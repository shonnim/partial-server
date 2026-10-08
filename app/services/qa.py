"""Question answering over one document: the pipeline behind the client's chat.

A user's query about an opened document runs through four steps:

1. **Classify** — one model call decides what kind of request it is
   (``question`` / ``summary`` / ``analysis`` / ``unclear``) and rewrites it
   for retrieval without changing its meaning. An ``unclear`` query stops the
   pipeline: the caller returns :data:`NOT_FOUND_MESSAGE` and nothing else.
2. **Retrieve** — the rewritten query goes through the query embedding model,
   the document's points in Qdrant are searched, and the best hit's chunk row
   is loaded from Postgres.
3. **Answer** — the query and the chunk's text go to the text model, under a
   system prompt chosen by the query's type.
4. **Sources** — the ``header_contents`` rows the chunk was built from are
   shown to the model next to the answer, and it names the ones the answer
   actually drew from; each comes back with its page index, the page number
   printed on that page (read from the content list), its text and its bbox.

Every step takes its model call (and the retrieval its search) as an
injectable callable, the way the Summarizer does, so the logic runs in tests
without a model. Failures raise — LLMError, EmbeddingError,
VectorStoreError — and the router turns them into an error event.
"""

import logging
import re
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.llm import LLMError, chat_json, chat_text
from app.models.chunk import Chunk
from app.models.document import Document
from app.models.header import HeaderContent
from app.schemas.retrieval import TOP_K
from app.services.headers import load_content_list
from app.services.retrieval import search_chunks

logger = logging.getLogger(__name__)

# The one reply an unanswerable query gets, verbatim.
NOT_FOUND_MESSAGE = "요청하신 내용을 문서에서 찾을 수 없습니다."

QUERY_TYPES = ("question", "summary", "analysis", "unclear")

# Answers are a few sentences; the ceiling only stops a runaway reply.
MAX_ANSWER_TOKENS = 1024

# ---- step 1: classification -------------------------------------------------------

SYSTEM_CLASSIFY = """You are the query gate of a question-answering service over one Korean fiscal or economic report. The user has the report open and asks about its content.

You are given the user's query. Decide what kind of request it is — exactly one of:

- "question": asks for specific facts, figures, dates, reasons or explanations that the report's body could state.
- "summary": asks to summarise, outline or shorten the report or a part of it.
- "analysis": asks for interpretation grounded in the report — comparisons, trends, causes, implications, an assessment.
- "unclear": cannot be understood, carries no request, or asks for nothing the report's body could answer (greetings, small talk, requests about unrelated topics or other documents).

Also produce "search_query": the query rewritten for semantic search over the report's text. Keep the query's language, keep every entity, figure and constraint it names, resolve fillers and conversational framing ("궁금한데요", "알려주세요") away, and NEVER change what is being asked — when the query is already a good search query, return it unchanged. For an "unclear" query, return the query as it is.

Return ONLY the JSON object."""

CLASSIFY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "query_type": {"type": "string", "enum": list(QUERY_TYPES)},
        "search_query": {"type": "string"},
    },
    "required": ["query_type", "search_query"],
    "additionalProperties": False,
}


@dataclass(frozen=True)
class Classification:
    """What the gate decided: the query's type and its retrieval form."""

    query_type: str
    search_query: str


def classify_query(
    query: str, *, complete_json: Callable[..., dict[str, Any]] = chat_json
) -> Classification:
    """One model call: the query's type, and the query rewritten for retrieval."""
    reply = complete_json(
        system=SYSTEM_CLASSIFY,
        user=f"USER QUERY:\n{query}",
        schema=CLASSIFY_SCHEMA,
        schema_name="query_classification",
        max_tokens=512,
    )
    query_type = reply.get("query_type")
    if query_type not in QUERY_TYPES:
        raise LLMError(f"the model returned an unknown query type: {query_type!r}")
    rewritten = reply.get("search_query")
    search_query = (
        rewritten.strip() if isinstance(rewritten, str) and rewritten.strip() else query
    )
    return Classification(query_type=query_type, search_query=search_query)


# ---- step 2: retrieval ------------------------------------------------------------


def retrieve_chunk(
    db: Session,
    *,
    doc_id: uuid.UUID,
    query: str,
    search: Callable[..., list] = search_chunks,
) -> Chunk | None:
    """The chunk row behind the document's best match for ``query``, or None.

    The top :data:`TOP_K` hits come back from Qdrant and the first one is
    taken; its chunk id and the document's id resolve to the ``chunks`` row.
    None means the document has no embedded chunks, or the winning point's
    row is gone (a re-chunk raced the question).
    """
    hits = search(query, doc_id=str(doc_id), limit=TOP_K)
    if not hits:
        return None
    best = hits[0]
    return db.scalar(
        select(Chunk).where(Chunk.id == best.chunk_id, Chunk.doc_id == doc_id)
    )


# ---- step 3: the answer -----------------------------------------------------------

SYSTEM_ANSWER_QUESTION = """You are the answering engine of a question-answering service over one Korean fiscal or economic report.

You are given a user's question about the report and one passage retrieved from the report's body. Answer the question in Korean, using only what the passage states.

- Answer ONLY what the question asks: the fact or figure itself, and the direct reason or detail when the question asks for one. Do not add background, surrounding context, or other content of the passage the question did not ask about.
- Keep it short: 1 to 3 sentences.
- Quote figures, units and periods exactly as the passage prints them; never invent or extrapolate numbers.
- Write polite Korean prose ("~습니다"), formatted as Markdown, with the key figures bolded with **...**.
- If the passage does not state what is asked, say so plainly in Korean instead of guessing.
- Return ONLY the Korean answer: no headings about the task, no JSON, no commentary."""

SYSTEM_ANSWER_SUMMARY = """You are the answering engine of a question-answering service over one Korean fiscal or economic report.

You are given a user's request to summarise and one passage retrieved from the report's body. Write the summary the user asked for in Korean, using only what the passage states.

- Summarise ONLY what the request asks about; drop the passage's other content, even when notable.
- Keep it short: a paragraph of 2 to 3 sentences, optionally followed by a list of at most 3 key points when the request covers several distinct points, one line each.
- Bold the most important parts — the key figures, turning points and conclusions — with **...**.
- Quote figures, units and periods exactly as the passage prints them; do not add anything the passage does not say.
- Return ONLY the Korean summary: no headings about the task, no JSON, no commentary."""

SYSTEM_ANSWER_ANALYSIS = """You are the answering engine of a question-answering service over one Korean fiscal or economic report.

You are given a user's request for analysis and one passage retrieved from the report's body. Write the analysis in Korean, grounded only in what the passage states.

- Address ONLY what the user asks to have analysed — the comparison, trend, cause or implication they name — and nothing else the passage happens to cover.
- Keep it short: 2 to 4 sentences.
- Build the analysis from the passage's own facts and figures, quoted exactly as printed; keep what the passage states clearly apart from what you infer from it, and infer nothing the passage cannot support.
- Write polite Korean prose ("~습니다"), formatted as Markdown, with the key figures and conclusions bolded with **...**.
- If the passage does not bear on what is asked, say so plainly in Korean instead of speculating.
- Return ONLY the Korean analysis: no headings about the task, no JSON, no commentary."""

# One system prompt per answerable query type.
ANSWER_SYSTEMS = {
    "question": SYSTEM_ANSWER_QUESTION,
    "summary": SYSTEM_ANSWER_SUMMARY,
    "analysis": SYSTEM_ANSWER_ANALYSIS,
}


def generate_answer(
    query: str,
    chunk_text: str,
    query_type: str,
    *,
    complete: Callable[..., str] = chat_text,
) -> str:
    """The answer to ``query`` from ``chunk_text``, under the type's prompt."""
    system = ANSWER_SYSTEMS.get(query_type)
    if system is None:
        raise LLMError(f"no answer prompt for query type {query_type!r}")
    user = f"USER QUERY:\n{query}\n\nSOURCE PASSAGE:\n{chunk_text}"
    return complete(system=system, user=user, max_tokens=MAX_ANSWER_TOKENS).strip()


# ---- step 4: the sources ----------------------------------------------------------

SYSTEM_SOURCES = """You decide which source blocks an answer was actually drawn from.

You are given an answer written for a user about one Korean fiscal or economic report, and the candidate blocks the answer could have come from, each opening with its id. The candidates are consecutive blocks of one passage, so most of them merely surround the real source: being on the same topic as the answer, or continuing the same section on the next page, does not make a block a source.

Judge every block on its own, against the answer's actual words:

- Include a block ONLY when a specific statement or figure of the answer is stated in that block itself.
- Never include a block because it precedes or follows a real source, repeats its topic, or would give helpful context.
- Most answers are drawn from exactly one block. Include a second only when the answer states something the first block does not contain, and no block twice.
- Return ids only from the given candidates, best match first.
- When no block states what the answer states, return an empty list.

Return ONLY the JSON object."""

SOURCES_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"source_ids": {"type": "array", "items": {"type": "integer"}}},
    "required": ["source_ids"],
    "additionalProperties": False,
}


def pick_source_ids(
    answer: str,
    candidates: Sequence[tuple[int, str]],
    *,
    complete_json: Callable[..., dict[str, Any]] = chat_json,
) -> list[int]:
    """The candidate ids the model names as the answer's sources, in its order.

    Ids the candidates do not contain are dropped — the model is constrained
    to integers, not to these integers — and so are repeats.
    """
    listed = "\n\n".join(f"[id {cid}]\n{content}" for cid, content in candidates)
    reply = complete_json(
        system=SYSTEM_SOURCES,
        user=f"ANSWER:\n{answer}\n\nCANDIDATE BLOCKS:\n{listed}",
        schema=SOURCES_SCHEMA,
        schema_name="answer_sources",
        max_tokens=256,
    )
    returned = reply.get("source_ids")
    if not isinstance(returned, list):
        raise LLMError("the model's source reply held no source_ids list")

    known = {cid for cid, _ in candidates}
    picked: list[int] = []
    for value in returned:
        if isinstance(value, int) and value in known and value not in picked:
            picked.append(value)
    return picked


# A run of digits not touching another digit, same as the headers service uses
# to read printed page numbers.
_NUMBER_TOKEN = re.compile(r"(?<!\d)\d+(?!\d)")


def printed_page(blocks: Any, page_idx: int | None) -> int | None:
    """The page number printed on parse page ``page_idx``, or None.

    Read from the content list's ``page_number`` blocks. A two-up parse
    prints two numbers on one parse page; the first one stands for the page.
    """
    if page_idx is None or not isinstance(blocks, list):
        return None
    for block in blocks:
        if not isinstance(block, dict) or block.get("type") != "page_number":
            continue
        if block.get("page_idx") != page_idx:
            continue
        text = block.get("text")
        if not isinstance(text, str):
            continue
        match = _NUMBER_TOKEN.search(text)
        if match:
            return int(match.group())
    return None


@dataclass(frozen=True)
class QASource:
    """One block the answer was drawn from, located on the page."""

    page_idx: int | None
    # The page number printed on that page, when the content list shows one.
    page: int | None
    text: str
    bbox: tuple[int | None, int | None, int | None, int | None]


def identify_sources(
    db: Session,
    document: Document,
    *,
    answer: str,
    chunk: Chunk,
    complete_json: Callable[..., dict[str, Any]] = chat_json,
    load_blocks: Callable[[Session, Document], tuple[Any, str | None]] = load_content_list,
) -> list[QASource]:
    """The ``header_contents`` rows behind ``answer``, located on their pages.

    The chunk's ``head_content_ids`` bound the candidates; the model picks
    among them. A missing content list costs only the printed page numbers —
    the sources still come back, with ``page`` None and the reason logged.
    """
    if not chunk.head_content_ids:
        return []

    rows = db.scalars(
        select(HeaderContent)
        .where(
            HeaderContent.doc_id == document.id,
            HeaderContent.id.in_(list(chunk.head_content_ids)),
        )
        .order_by(HeaderContent.id)
    ).all()
    candidates = [(row.id, row.content) for row in rows if row.content and row.content.strip()]
    if not candidates:
        return []

    picked = pick_source_ids(answer, candidates, complete_json=complete_json)
    if not picked:
        return []

    blocks, error = load_blocks(db, document)
    if error is not None:
        logger.warning(
            "no printed page numbers for the sources of %s: %s", document.id, error
        )
        blocks = None

    by_id = {row.id: row for row in rows}
    return [
        QASource(
            page_idx=row.page_idx,
            page=printed_page(blocks, row.page_idx),
            text=row.content or "",
            bbox=(row.bbox_0, row.bbox_1, row.bbox_2, row.bbox_3),
        )
        for cid in picked
        if (row := by_id[cid])
    ]
