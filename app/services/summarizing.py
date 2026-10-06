"""Summaries by header: the Summarizing run.

The Summarizing page picks documents in the tree and writes a 3-4 sentence
Korean summary into ``headers.conclusion``, level by level:

1. every level-2 header is summarised from the ``header_contents`` rows of
   its subtree — its own blocks and those of its level-3 children;
2. every level-1 header is then summarised from its level-2 children's
   summaries. A level-1 header without level-2 children falls back to its
   own content rows, the same way a level-2 header is handled;
3. the whole document is then summarised from the level-1 summaries into
   ``documents.summary`` — the general summary the pop-up opens with. A
   document without level-1 summaries falls back to the level-2 ones.

A summary is a paragraph, optionally followed by a short ordered or
unordered Markdown list of key points; the document summary always carries
the list.

One call to the text model per header. When a unit does not fit the input
budget, it is split into parts at block boundaries, each part is summarised
on its own, and the final summary is made from the part summaries.

A header that already carries a conclusion is skipped unless the run is
forced, so an interrupted run picks up where it left off; each conclusion is
committed as soon as the model answers, for the same reason. One header's
failure is counted and the document's other headers still run.
"""

import logging
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.llm import LLMError, chat_text
from app.models.document import Document
from app.models.header import Header, HeaderContent
from app.services.html_table import (
    TableParseError,
    body_rows,
    build_grid,
    detect_header_rows,
    flatten_headers,
    parse_html_table,
    render_markdown_table,
)
from app.services.tokenizer import count_tokens as _real_count_tokens
from app.services.tree import Level

logger = logging.getLogger(__name__)

# Headers a summary is written for; deeper levels only feed their parents.
SUMMARY_LEVELS = (1, 2)

# Input budget per model call, counted with the stand-in tokenizer. The model
# (gemma, max_model_len 262144) takes far more, but the stand-in counts
# differ from gemma's own and the prompt and reply need room, so the budget
# stays well under the ceiling. A unit past it is split and merged.
MAX_INPUT_TOKENS = 100_000

# A short paragraph plus an optional list of key points; the ceiling only
# stops a runaway reply.
MAX_SUMMARY_TOKENS = 1024

SYSTEM_SECTION = """You are a summarisation engine for a Korean-language document indexing and RAG pipeline.

You are given one section of a Korean summary report of an IMF or OECD publication: its heading, and the text, tables and figure descriptions printed under it, in reading order.

Write a summary of the section in Korean: 3 to 4 sentences, one paragraph. When the section covers several distinct points, you may follow the paragraph with a short Markdown list — ordered (1.) or unordered (-) — of the key points, one line each.

- Name the subject, the entities (countries, groups, indicators), the period and the units where the section states them.
- Give the main findings: the clear trends, the largest and smallest values, turning points, differences between groups, and the policy conclusions the section draws.
- Name the important concepts and keywords in Korean so the summary is useful for semantic search; keep names printed in English as they are.
- Do not invent figures, trends or sources that the section does not support.
- Return ONLY the Korean summary (the paragraph and the optional list): no headings, no JSON, no commentary about the task."""

SYSTEM_MERGE = """You are a summarisation engine for a Korean-language document indexing and RAG pipeline.

One section of a Korean report was too long to read at once, so it was split into consecutive parts and each part was summarised on its own. You are given the section's heading and the part summaries, in order.

Combine them into one summary of the whole section in Korean: 3 to 4 sentences, one paragraph. When the section covers several distinct points, you may follow the paragraph with a short Markdown list — ordered (1.) or unordered (-) — of the key points, one line each.

- Keep what matters for the section as a whole; drop repetition between parts.
- Do not add anything the part summaries do not say.
- Return ONLY the Korean summary (the paragraph and the optional list): no headings, no JSON, no commentary about the task."""

SYSTEM_PARENT = """You are a summarisation engine for a Korean-language document indexing and RAG pipeline.

You are given the heading of one chapter of a Korean summary report of an IMF or OECD publication, and the summaries of its sections, in order.

Write a summary of the chapter in Korean: 3 to 4 sentences, one paragraph. When the chapter covers several distinct points, you may follow the paragraph with a short Markdown list — ordered (1.) or unordered (-) — of the key points, one line each.

- Base it only on the section summaries; do not add anything they do not say.
- Keep the chapter's main findings and policy conclusions; drop section detail that does not matter at chapter level.
- Name the important concepts and keywords in Korean so the summary is useful for semantic search; keep names printed in English as they are.
- Return ONLY the Korean summary (the paragraph and the optional list): no headings, no JSON, no commentary about the task."""


SYSTEM_DOCUMENT = """You are a summarisation engine for a Korean-language document indexing and RAG pipeline.

You are given the title of a Korean summary report of an IMF or OECD publication, and the summaries of its chapters, in order.

Write a general summary of the whole document in Korean: one opening paragraph of 3 to 5 sentences, followed by a short Markdown list — ordered (1.) or unordered (-) — of the document's key points, one line each.

- Base it only on the chapter summaries; do not add anything they do not say.
- Keep the document's main findings and policy conclusions; drop chapter detail that does not matter at document level.
- Name the important concepts and keywords in Korean so the summary is useful for semantic search; keep names printed in English as they are.
- Return ONLY the Korean summary (the paragraph and the list): no headings, no JSON, no commentary about the task."""


def _chat(system: str, user: str) -> str:
    return chat_text(system=system, user=user, max_tokens=MAX_SUMMARY_TOKENS)


# ---- building the text a header is summarised from ------------------------------


def block_text(row: HeaderContent) -> str | None:
    """One content row as the model should read it, or None for an empty one.

    A table's HTML is rendered to Markdown the way the chunker renders it; a
    table whose HTML cannot be read goes in as it is, which the model can
    still make some sense of. An image or chart row's content is the Korean
    description the Section run stored. Caption and footnote frame the body.
    """
    body = row.content or ""
    if row.type == "table" and body.strip():
        try:
            grid = build_grid(parse_html_table(body))
            header_rows = detect_header_rows(grid)
            header = flatten_headers(grid, header_rows) if header_rows else [""] * grid.width
            body = render_markdown_table(header, body_rows(grid, header_rows))
        except TableParseError:
            pass

    parts = [p.strip() for p in (row.caption, body, row.footnote) if p and p.strip()]
    return "\n".join(parts) or None


def subtree_blocks(
    header: Header,
    children: dict[int | None, list[Header]],
    contents: dict[int | None, list[HeaderContent]],
) -> list[str]:
    """The blocks under a header and its descendants, in outline order.

    A descendant's blocks open with its own heading, so the structure the
    page printed survives into the text the model reads.
    """
    blocks = [b for row in contents.get(header.id, []) if (b := block_text(row))]
    for child in children.get(header.id, []):
        child_blocks = subtree_blocks(child, children, contents)
        if child_blocks:
            blocks.append(child.header.strip())
            blocks.extend(child_blocks)
    return blocks


# ---- the summariser --------------------------------------------------------------


@dataclass(frozen=True)
class UnitSummary:
    """One header's summary and how it was made."""

    text: str
    # Model calls spent: 1 for a unit within budget, parts + 1 past it.
    calls: int


class Summarizer:
    """Summaries for one header's worth of text, split and merged as needed.

    ``complete`` is the model call — (system, user) to reply text — and is
    injectable so the logic can be exercised without a model; so is the
    token counter, the same way the chunker takes one.
    """

    def __init__(
        self,
        complete: Callable[[str, str], str] = _chat,
        count_tokens: Callable[[str], int] = _real_count_tokens,
    ) -> None:
        self.complete = complete
        self.count_tokens = count_tokens

    def summarize_unit(self, title: str, blocks: Sequence[str]) -> UnitSummary:
        """The summary of one header's blocks: one call, or parts then a merge."""
        content = "\n".join(blocks)
        user = f"SECTION HEADING:\n{title}\n\nSECTION CONTENT:\n{content}"
        if self.count_tokens(user) <= MAX_INPUT_TOKENS:
            return UnitSummary(text=self._clean(self.complete(SYSTEM_SECTION, user)), calls=1)

        parts = self._split(blocks)
        summaries = []
        for part in parts:
            part_user = f"SECTION HEADING:\n{title}\n\nSECTION CONTENT:\n{part}"
            summaries.append(self._clean(self.complete(SYSTEM_SECTION, part_user)))

        numbered = "\n".join(f"{i}. {s}" for i, s in enumerate(summaries, start=1))
        merge_user = f"SECTION HEADING:\n{title}\n\nPART SUMMARIES:\n{numbered}"
        return UnitSummary(
            text=self._clean(self.complete(SYSTEM_MERGE, merge_user)),
            calls=len(parts) + 1,
        )

    def summarize_parent(self, title: str, children: Sequence[tuple[str, str]]) -> str:
        """The summary of a level-1 header from its children's summaries."""
        listed = "\n".join(f"- {child}: {summary}" for child, summary in children)
        user = f"CHAPTER HEADING:\n{title}\n\nSECTION SUMMARIES:\n{listed}"
        return self._clean(self.complete(SYSTEM_PARENT, user))

    def summarize_overall(self, title: str, chapters: Sequence[tuple[str, str]]) -> str:
        """The general summary of the whole document from its chapter summaries."""
        listed = "\n".join(
            f"{i}. {chapter}: {summary}"
            for i, (chapter, summary) in enumerate(chapters, start=1)
        )
        user = f"DOCUMENT TITLE:\n{title}\n\nCHAPTER SUMMARIES:\n{listed}"
        return self._clean(self.complete(SYSTEM_DOCUMENT, user))

    @staticmethod
    def _clean(reply: str) -> str:
        return reply.strip()

    def _split(self, blocks: Sequence[str]) -> list[str]:
        """Blocks packed greedily into parts within the input budget.

        A single block past the whole budget is sliced by lines; a single
        line past it — nothing a parser produces, but guarded anyway — is
        sliced by a character window so the pack always terminates.
        """
        units: list[str] = []
        for block in blocks:
            if self.count_tokens(block) <= MAX_INPUT_TOKENS:
                units.append(block)
                continue
            for line in block.split("\n"):
                if self.count_tokens(line) <= MAX_INPUT_TOKENS:
                    units.append(line)
                    continue
                window = MAX_INPUT_TOKENS * 2  # ≥ 2 characters per token
                units.extend(
                    line[start : start + window] for start in range(0, len(line), window)
                )

        parts: list[str] = []
        current: list[str] = []
        current_tokens = 0
        for unit in units:
            tokens = self.count_tokens(unit) + 1
            if current and current_tokens + tokens > MAX_INPUT_TOKENS:
                parts.append("\n".join(current))
                current, current_tokens = [], 0
            current.append(unit)
            current_tokens += tokens
        if current:
            parts.append("\n".join(current))
        return parts


# ---- summarising one document -----------------------------------------------------


@dataclass
class SummarizeResult:
    """What one document's run did, in the shape the queue row reports."""

    id: uuid.UUID
    filename: str
    ok: bool = True
    # Conclusions written, headers skipped for already having one, failures.
    summarized: int = 0
    skipped: int = 0
    failed: int = 0
    calls: int = 0
    # The general summary: written this run / present after the run.
    doc_summarized: bool = False
    has_summary: bool = False
    # The document's standing after the run, for the tree badge.
    total: int = 0
    done_total: int = 0
    seconds: float = 0.0
    error: str | None = None
    errors: list[str] = field(default_factory=list)

    @property
    def done(self) -> bool:
        return self.total > 0 and self.done_total == self.total and self.has_summary


def _load_outline(
    db: Session, doc_id: uuid.UUID
) -> tuple[list[Header], dict[int | None, list[Header]], dict[int | None, list[HeaderContent]]]:
    headers = list(
        db.scalars(select(Header).where(Header.doc_id == doc_id).order_by(Header.id)).all()
    )
    children: dict[int | None, list[Header]] = {}
    for header in headers:
        children.setdefault(header.parent_id, []).append(header)

    contents: dict[int | None, list[HeaderContent]] = {}
    for row in db.scalars(
        select(HeaderContent)
        .where(HeaderContent.doc_id == doc_id)
        .order_by(HeaderContent.id)
    ).all():
        contents.setdefault(row.header_id, []).append(row)
    return headers, children, contents


def summarize_document(
    db: Session,
    doc: Document,
    *,
    force: bool = False,
    summarizer: Summarizer | None = None,
) -> SummarizeResult:
    """Write a conclusion for every level-2 then level-1 header of ``doc``,
    then the document's general summary into ``documents.summary``.

    Each conclusion is committed as soon as the model answers, so a failure
    or a stop loses nothing already done. Returns what happened, never
    raises: the caller is a queue row on the page.
    """
    summarizer = summarizer or Summarizer()
    result = SummarizeResult(id=doc.id, filename=doc.filename)
    started = time.monotonic()

    headers, children, contents = _load_outline(db, doc.id)
    level2 = [h for h in headers if h.level == 2]
    level1 = [h for h in headers if h.level == 1]
    result.total = len(level1) + len(level2)

    def write(header: Header, make: Callable[[], tuple[str, int]]) -> None:
        if header.conclusion and not force:
            result.skipped += 1
            return
        try:
            summary, calls = make()
            header.conclusion = summary
            db.commit()
            result.summarized += 1
            result.calls += calls
        except LLMError as exc:
            db.rollback()
            result.failed += 1
            result.errors.append(f"'{header.header.strip()}': {exc}")
            logger.warning("summarizing header %s of %s failed: %s", header.id, doc.id, exc)

    def from_contents(header: Header) -> tuple[str, int]:
        blocks = subtree_blocks(header, children, contents)
        if not blocks:
            raise LLMError("no content rows under this header")
        unit = summarizer.summarize_unit(header.header.strip(), blocks)
        return unit.text, unit.calls

    for header in level2:
        write(header, lambda h=header: from_contents(h))

    for header in level1:

        def make(h: Header = header) -> tuple[str, int]:
            summarised = [
                (child.header.strip(), child.conclusion.strip())
                for child in children.get(h.id, [])
                if child.level == 2 and child.conclusion and child.conclusion.strip()
            ]
            if summarised:
                return summarizer.summarize_parent(h.header.strip(), summarised), 1
            # No level-2 children (or none summarised): the header's own
            # contents are the next best source, handled like a section.
            return from_contents(h)

        write(header, make)

    # The general summary of the whole document, from the level-1 summaries
    # just written — or the level-2 ones when the outline has no level-1 rows.
    result.has_summary = bool(doc.summary and doc.summary.strip())
    if not result.has_summary or force:
        tops = [h for h in level1 if h.conclusion and h.conclusion.strip()] or [
            h for h in level2 if h.conclusion and h.conclusion.strip()
        ]
        if tops:
            try:
                doc.summary = summarizer.summarize_overall(
                    doc.filename,
                    [(h.header.strip(), h.conclusion.strip()) for h in tops],
                )
                db.commit()
                result.doc_summarized = True
                result.has_summary = True
                result.calls += 1
            except LLMError as exc:
                db.rollback()
                result.failed += 1
                result.errors.append(f"document summary: {exc}")
                logger.warning("document summary of %s failed: %s", doc.id, exc)
        else:
            result.failed += 1
            result.errors.append("document summary: no header summaries to build it from")

    result.done_total = sum(
        1 for h in [*level1, *level2] if h.conclusion and h.conclusion.strip()
    )
    result.seconds = time.monotonic() - started
    if result.errors:
        result.error = result.errors[0]
    result.ok = result.failed == 0
    return result


# ---- the queue --------------------------------------------------------------------


@dataclass(frozen=True)
class QueueCandidate:
    id: uuid.UUID
    filename: str
    # Level 1-2 headers in the document, which is the work the row stands for.
    headers: int


@dataclass(frozen=True)
class QueueSkip:
    filename: str
    reason: str


@dataclass(frozen=True)
class SummarizeQueue:
    eligible: list[QueueCandidate]
    skipped: list[QueueSkip]


def build_queue(
    db: Session, doc_ids: Sequence[uuid.UUID], *, force: bool = False
) -> SummarizeQueue:
    """Split a selection into documents worth queueing and ones to skip.

    Only a document with level 1-2 headers and content rows can be
    summarised, and one whose summaries are all written has nothing left to
    do unless the run is forced; both are reported with the reason rather
    than failing the click.
    """
    stats = file_stats(db, doc_ids)
    eligible: list[QueueCandidate] = []
    skipped: list[QueueSkip] = []
    for doc_id in doc_ids:
        doc = db.get(Document, doc_id)
        if doc is None:
            skipped.append(QueueSkip(str(doc_id), "document not found"))
            continue
        doc_stats = stats.get(doc.id)
        if doc_stats is None or not doc_stats.total:
            skipped.append(
                QueueSkip(doc.filename, "no level 1-2 headers stored (run Headers first)")
            )
            continue
        if not doc_stats.with_contents:
            skipped.append(
                QueueSkip(doc.filename, "no content rows stored (run Section first)")
            )
            continue
        if doc_stats.is_done and not force:
            skipped.append(QueueSkip(doc.filename, "already summarized"))
            continue
        eligible.append(QueueCandidate(doc.id, doc.filename, doc_stats.total))
    return SummarizeQueue(eligible=eligible, skipped=skipped)


# ---- tree decoration --------------------------------------------------------------


@dataclass(frozen=True)
class DocSummaryStats:
    """One document's standing, for the badge on its tree row."""

    total: int = 0
    done: int = 0
    with_contents: bool = False
    # Whether the general summary in ``documents.summary`` is written.
    has_summary: bool = False

    @property
    def is_done(self) -> bool:
        return self.total > 0 and self.done == self.total and self.has_summary


def file_stats(db: Session, doc_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, DocSummaryStats]:
    """Per-document header counts for the file rows of one tree level."""
    if not doc_ids:
        return {}
    rows = db.execute(
        text("""
            SELECT h.doc_id,
                   count(*) AS total,
                   count(*) FILTER (
                       WHERE h.conclusion IS NOT NULL AND btrim(h.conclusion) <> ''
                   ) AS done,
                   EXISTS (
                       SELECT 1 FROM header_contents hc WHERE hc.doc_id = h.doc_id
                   ) AS with_contents,
                   bool_or(d.summary IS NOT NULL AND btrim(d.summary) <> '') AS has_summary
            FROM headers h
            JOIN documents d ON d.id = h.doc_id
            WHERE h.doc_id = ANY(cast(:ids AS uuid[])) AND h.level IN (1, 2)
            GROUP BY h.doc_id
        """),
        {"ids": [str(i) for i in doc_ids]},
    ).all()
    return {
        r.doc_id: DocSummaryStats(r.total, r.done, r.with_contents, r.has_summary)
        for r in rows
    }


@dataclass(frozen=True)
class FolderSummaryStats:
    """How many of a folder's documents are fully summarized."""

    done: int = 0


def folder_stats(db: Session, path: str) -> dict[str, FolderSummaryStats]:
    """Per-subfolder counts for one tree level, keyed by subfolder name."""
    child_depth = len(path.split("/")) + 1 if path else 1
    rows = db.execute(
        text("""
            WITH scoped AS (
                SELECT id, substring(s3_key from '^(.*)/[^/]*$') AS folder,
                       (summary IS NOT NULL AND btrim(summary) <> '') AS has_summary
                FROM documents
                WHERE :path = '' OR starts_with(s3_key, :path || '/')
            ),
            per_doc AS (
                SELECT s.folder,
                       count(*) AS total,
                       count(*) FILTER (
                           WHERE h.conclusion IS NOT NULL AND btrim(h.conclusion) <> ''
                       ) AS done,
                       bool_or(s.has_summary) AS has_summary
                FROM scoped s
                JOIN headers h ON h.doc_id = s.id AND h.level IN (1, 2)
                WHERE s.folder IS NOT NULL
                GROUP BY s.id, s.folder
            )
            SELECT split_part(folder, '/', :child_depth) AS name,
                   count(*) FILTER (WHERE done = total AND has_summary) AS done
            FROM per_doc
            WHERE split_part(folder, '/', :child_depth) <> ''
            GROUP BY 1
        """),
        {"path": path, "child_depth": child_depth},
    ).all()
    return {r.name: FolderSummaryStats(r.done) for r in rows}


def decorate_level(db: Session, level: Level) -> dict:
    """The template context for one tree level: the level plus its stats."""
    return {
        "level": level,
        "sstats": file_stats(db, [f.id for f in level.files]),
        "fstats": folder_stats(db, level.path) if level.folders else {},
    }


def document_summaries(db: Session, doc_id: uuid.UUID) -> list[Header]:
    """The document's level 1-2 headers in outline order, for the pop-up.

    Outline order is level-1 headers by id, each followed by its level-2
    children — the order the report prints them.
    """
    headers, children, _ = _load_outline(db, doc_id)
    ordered: list[Header] = []
    for header in headers:
        if header.level == 1:
            ordered.append(header)
            ordered.extend(c for c in children.get(header.id, []) if c.level == 2)
    # Level-2 headers whose parent is not a level-1 row still belong.
    seen = {h.id for h in ordered}
    ordered.extend(h for h in headers if h.level == 2 and h.id not in seen)
    return ordered
