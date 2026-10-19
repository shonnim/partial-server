"""Question answering over one document: the pipeline behind the client's chat.

A user's query about an opened document runs through these steps:

1. **Validate pages** — a page the query points at ("99페이지의 수치") is
   checked against the last page number printed in the document's content
   list; a query pointing past the document gets :data:`NOT_FOUND_MESSAGE`
   before any model call.
2. **Classify** — one model call decides what kind of request it is
   (``question`` / ``summary`` / ``analysis`` / ``unclear``) and rewrites it
   for retrieval without changing its meaning. An ``unclear`` query stops the
   pipeline: the caller returns :data:`NOT_FOUND_MESSAGE` and nothing else.
3. **Route a summary** — a ``summary`` query is routed by scope before any
   retrieval: one model call over the document's outline decides whether the
   user asks for the whole document or one section, and the stored summary —
   ``documents.summary`` for the document, the header's ``conclusion`` for a
   section — is answered from directly. Only a summary request with no stored
   summary to serve falls through to retrieval like any other query.
4. **Retrieve** — the rewritten query goes through the query embedding model,
   the document's points in Qdrant are searched, and the best hit's chunk row
   is loaded from Postgres.
5. **Verify** — one model call decides whether the retrieved chunk actually
   states what the query needs; when it does not, the caller returns
   :data:`NOT_FOUND_MESSAGE` instead of an answer forced from the wrong
   passage.
6. **Answer** — the query and the chunk's text go to the text model, under a
   system prompt chosen by the query's type.
7. **Sources** — the ``header_contents`` rows the chunk was built from are
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

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.llm import LLMError, chat_json, chat_text
from app.models.chunk import Chunk
from app.models.document import Document
from app.models.header import Header, HeaderContent
from app.schemas.retrieval import TOP_K
from app.services.headers import load_content_list
from app.services.retrieval import search_chunks
from app.services.summarizing import document_summaries

logger = logging.getLogger(__name__)

# The one reply an unanswerable query gets, verbatim.
NOT_FOUND_MESSAGE = "이 보고서에 있는 내용만 답변할 수 있어요"

QUERY_TYPES = ("question", "summary", "analysis", "unclear")

# Answers run to a short analytical report; the ceiling only stops a runaway reply.
MAX_ANSWER_TOKENS = 4096

# ---- step 1: page validation ------------------------------------------------------

# A page the query points at: "99페이지", "99 쪽", "page 99", "p.99".
_PAGE_REF = re.compile(
    r"(\d{1,4})\s*(?:페이지|쪽)|(?:page|p)\s*\.?\s*(\d{1,4})", re.IGNORECASE
)

# A run of digits not touching another digit, same as the headers service uses
# to read printed page numbers.
_NUMBER_TOKEN = re.compile(r"(?<!\d)\d+(?!\d)")


def requested_pages(query: str) -> list[int]:
    """The page numbers the query explicitly points at, deduplicated, in order."""
    pages: list[int] = []
    for korean, latin in _PAGE_REF.findall(query):
        page = int(korean or latin)
        if page not in pages:
            pages.append(page)
    return pages


def last_printed_page(blocks: Any) -> int | None:
    """The highest page number printed on the document's last numbered page.

    Read from the content list's ``page_number`` blocks, scanning from the end
    for the last one whose text shows a number. A two-up parse prints two
    numbers on one parse page; the larger one is where the document ends.
    """
    if not isinstance(blocks, list):
        return None
    for block in reversed(blocks):
        if not isinstance(block, dict) or block.get("type") != "page_number":
            continue
        text = block.get("text")
        if not isinstance(text, str):
            continue
        numbers = [int(m.group()) for m in _NUMBER_TOKEN.finditer(text)]
        if numbers:
            return max(numbers)
    return None


def query_pages_exist(
    db: Session,
    document: Document,
    query: str,
    *,
    load_blocks: Callable[[Session, Document], tuple[Any, str | None]] = load_content_list,
) -> bool:
    """Whether every page the query points at exists in the document.

    False only when the content list positively shows a smaller last printed
    page. A query naming no page, a document without a content list, and a
    content list without printed numbers all validate — the check refuses what
    is provably absent, never what it cannot see.
    """
    pages = requested_pages(query)
    if not pages:
        return True

    blocks, error = load_blocks(db, document)
    if error is not None:
        logger.warning("page check skipped for %s: %s", document.id, error)
        return True

    last = last_printed_page(blocks)
    if last is None:
        return True
    return max(pages) <= last


# ---- step 2: classification -------------------------------------------------------

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


# ---- step 3: summary routing ------------------------------------------------------

SUMMARY_SCOPES = ("document", "section")

SYSTEM_SUMMARY_SCOPE = """You route a summary request over one Korean fiscal or economic report. The user has the report open and asks for a summary.

You are given the user's query and the report's outline — its chapter and section headings, each opening with its id. Decide what the user asks to have summarised — exactly one of:

- "document": the whole report — the query names no particular part of it ("요약해줘", "이 보고서 내용 정리해줘").
- "section": one part of the report — the query names or clearly points at a chapter or section.

For a "section" request, also return "header_id": the id of the outline heading the query points at — the heading it names, or the heading whose chapter or section contains what it names. When the query asks about a part that matches no listed heading, return "header_id": null.

For a "document" request, return "header_id": null.

Return ONLY the JSON object."""

SUMMARY_SCOPE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "scope": {"type": "string", "enum": list(SUMMARY_SCOPES)},
        "header_id": {"type": ["integer", "null"]},
    },
    "required": ["scope", "header_id"],
    "additionalProperties": False,
}


@dataclass(frozen=True)
class SummaryScope:
    """What a summary request is about: the whole document, or one header."""

    scope: str
    header_id: int | None


def classify_summary_scope(
    query: str,
    outline: Sequence[tuple[int, int, str]],
    *,
    complete_json: Callable[..., dict[str, Any]] = chat_json,
) -> SummaryScope:
    """One model call: whether the summary request targets the document or a section.

    ``outline`` is the document's headings as (id, level, heading) rows, in
    outline order. A header id the outline does not contain is dropped to
    None, the way :func:`pick_source_ids` drops unknown ids.
    """
    listed = (
        "\n".join(
            f"[id {hid}] {'  ' * (level - 1)}{heading}" for hid, level, heading in outline
        )
        or "(the document has no outline)"
    )
    reply = complete_json(
        system=SYSTEM_SUMMARY_SCOPE,
        user=f"USER QUERY:\n{query}\n\nOUTLINE:\n{listed}",
        schema=SUMMARY_SCOPE_SCHEMA,
        schema_name="summary_scope",
        max_tokens=128,
    )
    scope = reply.get("scope")
    if scope not in SUMMARY_SCOPES:
        raise LLMError(f"the model returned an unknown summary scope: {scope!r}")
    header_id = reply.get("header_id")
    known = {hid for hid, _, _ in outline}
    return SummaryScope(
        scope=scope,
        header_id=header_id if isinstance(header_id, int) and header_id in known else None,
    )


@dataclass(frozen=True)
class StoredSummary:
    """The stored summary a summary request resolves to."""

    text: str
    # The section's heading; None when the whole document's summary answers.
    section: str | None


def find_stored_summary(
    db: Session,
    document: Document,
    query: str,
    *,
    complete_json: Callable[..., dict[str, Any]] = chat_json,
    load_headers: Callable[[Session, uuid.UUID], list[Header]] = document_summaries,
) -> StoredSummary | None:
    """The stored summary that answers a summary request, or None.

    The Summarizing run writes ``documents.summary`` for the whole document
    and ``headers.conclusion`` per level 1-2 header; one model call over the
    outline decides which one the query asks for. None — nothing is stored
    for what was asked, or the scope resolves to no stored header — sends the
    query down the ordinary retrieval path instead of refusing it.
    """
    headers = load_headers(db, document.id)
    doc_summary = (document.summary or "").strip()
    if not headers and not doc_summary:
        return None

    outline = [(h.id, h.level, h.header.strip()) for h in headers]
    decided = classify_summary_scope(query, outline, complete_json=complete_json)

    if decided.scope == "document":
        return StoredSummary(text=doc_summary, section=None) if doc_summary else None

    header = next((h for h in headers if h.id == decided.header_id), None)
    if header is None:
        return None
    conclusion = (header.conclusion or "").strip()
    if not conclusion:
        return None
    return StoredSummary(text=conclusion, section=header.header.strip())


SYSTEM_ANSWER_STORED_SUMMARY = """You are the answering engine of a question-answering service over one Korean fiscal or economic report.

You are given a user's request to summarise, and the stored summary of what they asked about — the whole report, or one section of it named by its heading. Write the reply to the user in Korean from the stored summary alone.

- Deliver the summary's content as the answer: keep every key figure, turning point and conclusion it states.
- Rewrite, do not transcribe: the stored summary is written in the clipped 개조식 style; the reply is polite Korean prose ("~습니다").
- Shape the reply to the request — when the user asks for a particular length or focus the stored summary covers, follow it.
- Quote figures, units and periods exactly as the summary prints them; add nothing the summary does not say.
- Format the reply as Markdown with the key figures and conclusions bolded with **...**; keep the summary's list of key points when it carries one.
- Return ONLY the Korean reply: no headings about the task, no JSON, no commentary."""


def generate_summary_answer(
    query: str,
    summary: str,
    *,
    section: str | None = None,
    complete: Callable[..., str] = chat_text,
) -> str:
    """The reply to a summary request, written from the stored summary."""
    heading = f"SECTION HEADING:\n{section}\n\n" if section else ""
    user = f"USER QUERY:\n{query}\n\n{heading}STORED SUMMARY:\n{summary}"
    return complete(
        system=SYSTEM_ANSWER_STORED_SUMMARY, user=user, max_tokens=MAX_ANSWER_TOKENS
    ).strip()


# ---- step 4: retrieval ------------------------------------------------------------


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


# ---- step 5: chunk verification ----------------------------------------------------

SYSTEM_VERIFY = """You decide whether a retrieved passage can ground an answer.

You are given a user's request about one Korean fiscal or economic report and the one passage retrieved from the report's body for it. Decide whether the passage itself states the information the request needs — the facts or figures a question asks for, or the content a summary or analysis request is about.

- Judge only against the passage's own words: being on the same topic, or surrounding the likely answer, is not enough.
- "answerable" is true when the passage states at least the core of what is asked, even if not every detail; false when it merely touches the topic or does not bear on the request at all.

Return ONLY the JSON object."""

VERIFY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"answerable": {"type": "boolean"}},
    "required": ["answerable"],
    "additionalProperties": False,
}


def chunk_answers(
    query: str,
    chunk_text: str,
    *,
    complete_json: Callable[..., dict[str, Any]] = chat_json,
) -> bool:
    """One model call: does the retrieved passage state what the query needs?"""
    reply = complete_json(
        system=SYSTEM_VERIFY,
        user=f"USER QUERY:\n{query}\n\nRETRIEVED PASSAGE:\n{chunk_text}",
        schema=VERIFY_SCHEMA,
        schema_name="chunk_verification",
        max_tokens=128,
    )
    answerable = reply.get("answerable")
    if not isinstance(answerable, bool):
        raise LLMError("the model's verification reply held no answerable boolean")
    return answerable


# ---- step 6: the answer -----------------------------------------------------------

SYSTEM_ANSWER_QUESTION = """You are the answering engine of a question-answering service over one Korean fiscal or economic report.

You are given a user's question about the report and one passage retrieved from the report's body. Answer the question in Korean, using only what the passage states, at the depth of a small analytical report rather than a one-line reply.

- Structure the answer 두괄식 (conclusion first), as 서론-본론-결론:
  1. Open with the direct answer to the question — the fact or figure itself — in the first sentence or two.
  2. Then develop it in depth: the passage's supporting evidence, surrounding figures, conditions, reasons and context that bear on the question, each tied back to what was asked.
  3. Close with a short concluding paragraph that restates the core of the answer and what it means within the passage's scope.
- Stay on the question: develop everything in the passage that supports or qualifies the answer, but leave out passage content unrelated to what was asked.
- Depth comes from the passage, not from padding: when the passage states little, a shorter faithful answer beats a stretched one.
- Quote figures, units and periods exactly as the passage prints them; never invent or extrapolate numbers.
- Write polite Korean prose ("~습니다"), formatted as Markdown — descriptive headings or bold lead-ins for the parts (never the literal words 서론/본론/결론), lists where they aid reading — with the key figures bolded with **...**.
- If the passage does not state what is asked, say so plainly in Korean instead of guessing.
- Return ONLY the Korean answer: no headings about the task, no JSON, no commentary."""

SYSTEM_ANSWER_SUMMARY = """You are the answering engine of a question-answering service over one Korean fiscal or economic report.

You are given a user's request to summarise and one passage retrieved from the report's body. Write the summary the user asked for in Korean, using only what the passage states, at the depth of a small analytical report rather than a few lines.

- Structure the summary 두괄식 (conclusion first), as 서론-본론-결론:
  1. Open with the passage's central conclusion on what the request asks about, in the first sentence or two.
  2. Then lay out the substance in depth: the key figures, developments, turning points and reasons the passage gives, grouped under descriptive headings or as lists where that aids reading.
  3. Close with a short concluding paragraph that draws the points together.
- Summarise ONLY what the request asks about; drop the passage's other content, even when notable.
- Depth comes from the passage, not from padding: when the passage states little, a shorter faithful summary beats a stretched one.
- Bold the most important parts — the key figures, turning points and conclusions — with **...**.
- Quote figures, units and periods exactly as the passage prints them; do not add anything the passage does not say.
- Write polite Korean prose ("~습니다"), formatted as Markdown; never use the literal words 서론/본론/결론 as headings.
- Return ONLY the Korean summary: no headings about the task, no JSON, no commentary."""

SYSTEM_ANSWER_ANALYSIS = """You are the answering engine of a question-answering service over one Korean fiscal or economic report.

You are given a user's request for analysis and one passage retrieved from the report's body. Write the analysis in Korean, grounded only in what the passage states, at the depth of a small analytical report rather than a few sentences.

- Structure the analysis 두괄식 (conclusion first), as 서론-본론-결론:
  1. Open with the analytical conclusion — the finding on the comparison, trend, cause or implication the user names — in the first sentence or two.
  2. Then build the case in depth: the passage's facts and figures that support the finding, how they relate, and the conditions or counterpoints the passage itself raises, under descriptive headings or lists where that aids reading.
  3. Close with a short concluding paragraph on what the finding means within the passage's scope.
- Address ONLY what the user asks to have analysed — the comparison, trend, cause or implication they name — and nothing else the passage happens to cover.
- Depth comes from the passage, not from padding: when the passage states little, a shorter faithful analysis beats a stretched one.
- Build the analysis from the passage's own facts and figures, quoted exactly as printed; keep what the passage states clearly apart from what you infer from it, and infer nothing the passage cannot support.
- Write polite Korean prose ("~습니다"), formatted as Markdown, with the key figures and conclusions bolded with **...**; never use the literal words 서론/본론/결론 as headings.
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


# ---- step 7: the sources ----------------------------------------------------------

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


# ---- suggested questions ----------------------------------------------------------

# How many 추천질문 ride along with an answer.
SUGGESTED_COUNT = 3

SYSTEM_SUGGEST = """You write the suggested questions (추천질문) a document viewer shows under an answer.

You are given a few passages from one Korean fiscal or economic report. For each passage, write one question in Korean that a reader of the report would plausibly ask and that the passage itself answers.

- One question per passage, in the passages' order.
- Write short, natural, specific questions in polite Korean ("~나요?", "~인가요?"), each a single sentence.
- Ask about the passage's main fact, figure or conclusion; never about anything the passage does not state.
- Do not mention the passages, page numbers, or this task in the questions.

Return ONLY the JSON object."""

SUGGEST_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "questions": {
            "type": "array",
            "items": {"type": "string"},
            "minItems": 1,
            "maxItems": SUGGESTED_COUNT,
        }
    },
    "required": ["questions"],
    "additionalProperties": False,
}


def suggest_questions(
    db: Session,
    doc_id: uuid.UUID,
    *,
    complete_json: Callable[..., dict[str, Any]] = chat_json,
) -> list[str]:
    """Up to :data:`SUGGESTED_COUNT` questions the document can answer.

    One model call over that many of the document's chunks, drawn at random
    so the box changes between asks. The suggestions only ride along with an
    answer, so a failure costs the box and nothing else: it is logged and an
    empty list returned, never raised.
    """
    chunks = db.scalars(
        select(Chunk)
        .where(Chunk.doc_id == doc_id)
        .order_by(func.random())
        .limit(SUGGESTED_COUNT)
    ).all()
    texts = [text for c in chunks if (text := (c.text or c.emb_text).strip())]
    if not texts:
        return []

    listed = "\n\n".join(f"[passage {i}]\n{text}" for i, text in enumerate(texts, 1))
    try:
        reply = complete_json(
            system=SYSTEM_SUGGEST,
            user=f"PASSAGES:\n{listed}",
            schema=SUGGEST_SCHEMA,
            schema_name="suggested_questions",
            max_tokens=512,
        )
    except LLMError as exc:
        logger.warning("suggested questions for %s failed: %s", doc_id, exc)
        return []

    questions: list[str] = []
    for value in reply.get("questions") or []:
        if isinstance(value, str) and value.strip() and value.strip() not in questions:
            questions.append(value.strip())
    return questions[:SUGGESTED_COUNT]
