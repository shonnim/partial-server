"""Turning a table of contents into rows in the ``headers`` table.

A contents page is a tree flattened onto paper: indentation and numbering carry
the nesting, and nothing else does. The model is asked for that nesting as a
``level`` per entry rather than as nested objects — a flat list in reading order
is far easier to constrain, to validate, and to argue with when it comes out
wrong. The tree is rebuilt here, from the levels, with an explicit stack.

Extraction is destructive by design: re-running it for a document replaces that
document's headers rather than appending a second copy, because the pasted text
is a correction of the previous attempt, not an addition to it.
"""

import enum
import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.llm import LLMError, chat_json
from app.models.document import Document, HasHeader
from app.models.header import Header
from app.services.headers import CheckOutcome, check_has_header

# The deepest nesting a contents page is allowed to express. Six is already
# more than any document in this corpus uses; the ceiling exists so a model
# that starts counting paragraphs cannot write a thousand-deep tree.
MAX_LEVEL = 6

# A pasted contents page is a page or two of text. The cap is what separates
# "the user pasted a table of contents" from "the user pasted the whole PDF".
MAX_INPUT_CHARS = 40_000


class ExtractionMethod(str, enum.Enum):
    """How the headers of a document are obtained.

    MANUAL is hand-fed: a person pastes the contents page. PAGES is automatic:
    the contents page is located in the content list, cut out of the PDF by the
    parsing service, and the resulting markdown takes the place of the paste.
    Both end in the same model call.
    """

    MANUAL = "manual"
    PAGES = "pages"

    @property
    def label(self) -> str:
        return _METHOD_LABELS[self]


_METHOD_LABELS = {
    ExtractionMethod.MANUAL: "Manual",
    ExtractionMethod.PAGES: "Pages",
}


HEADER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "headers": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "level": {"type": "integer", "minimum": 1, "maximum": MAX_LEVEL},
                    "start_page": {"type": ["integer", "null"]},
                },
                "required": ["title", "level", "start_page"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["headers"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """You convert a document's table of contents into structured data.

You are given the contents page of a report as plain text. Return every heading
it lists, in the order they appear, as a flat array.

Rules:

1. Copy each title exactly as written. Do not translate it, do not correct its
spelling, do not expand its abbreviations, and do not restyle its punctuation.
Korean stays Korean; English stays English.
2. Keep the numbering that is part of the title ("I.", "제1장", "1.2",
"Chapter 3") as part of the title.
3. Strip the dot leaders and the trailing page number from the title. Put the
page number in start_page as an integer. If a line has no page number,
start_page is null.
4. `level` is the depth of the heading, starting at 1 for the outermost
divisions. Read the depth from the numbering style and the indentation: a part
or unit (편, 부, Part) is above a chapter (장, Chapter), which is above a
section (절, I., 1.), which is above a subsection (가., 1.1, a.). Sibling
headings share a level. A level is never more than one deeper than the heading
above it.
5. A title that wraps onto the next line is one heading, not two. Join the
lines with a single space.
6. Return only the entries of the contents list. Skip the word that
introduces the contents page itself (목차, 목 차, 차례, Contents, Table of
Contents), page headers and footers, and lists of tables or figures (표 목차,
그림 목차, List of Tables, List of Figures).
7. The text may run past the end of the contents list into the first pages of
the body. The list ends where lines stop looking like entries — where a
chapter heading is followed by paragraphs rather than by more entries. Take
nothing from beyond that point. A heading that appears in the contents list
and again as the opening of the body is one entry, taken from the list, not
two.
8. Invent nothing. Every entry must correspond to a line in the contents list."""


_TITLE_NOISE = re.compile(r"[\s\W_]+", re.UNICODE)


def _title_key(title: str) -> str:
    """A title reduced to what survives copying: letters and digits, folded."""
    return _TITLE_NOISE.sub("", title).casefold()


@dataclass(frozen=True)
class ExtractedHeader:
    """One heading as the model returned it, before it is given a parent."""

    title: str
    level: int
    start_page: int | None


@dataclass(frozen=True)
class HeaderNode:
    """A saved header plus its children, for display."""

    id: int
    title: str
    level: int
    start_page: int | None
    children: list["HeaderNode"] = field(default_factory=list)


class ExtractionError(RuntimeError):
    """The pasted text could not be turned into headers."""


def build_user_prompt(contents: str) -> str:
    return f"Table of contents:\n\n{contents.strip()}"


def _is_body_repeat(
    title: str, start_page: int | None, seen: Sequence[ExtractedHeader]
) -> bool:
    """Whether ``title`` is a body heading echoing an entry already taken.

    The input can run past the contents list into the body, whose first
    headings repeat the list's entries — often with something appended, like
    the English title in brackets. Such an echo has no page number of its own
    and begins with a title that was already listed *with* one. A genuine
    repeated title inside a contents list (two chapters each opening with
    "1. 개요") keeps its page number and is untouched.
    """
    if start_page is not None:
        return False
    key = _title_key(title)
    if not key:
        return False
    return any(
        earlier.start_page is not None and key.startswith(_title_key(earlier.title))
        for earlier in seen
        if _title_key(earlier.title)
    )


def _clean(raw: Sequence[dict[str, Any]]) -> list[ExtractedHeader]:
    """Validate the model's array and normalise the levels.

    The schema guarantees the shape but not the sense of it: a model can still
    open at level 3 or jump two levels at once. Both are repaired here rather
    than rejected, because the titles are the expensive part and the depth is
    trivially recoverable — but a level is only ever pulled *up* to keep the
    tree connected, never pushed deeper than the model asked for.

    Body headings that echo an entry already taken are dropped on the way.
    """
    headers: list[ExtractedHeader] = []
    previous = 0

    for item in raw:
        title = str(item.get("title") or "").strip()
        if not title:
            continue

        page = item.get("start_page")
        start_page = (
            page
            if isinstance(page, int) and not isinstance(page, bool) and page >= 0
            else None
        )
        if _is_body_repeat(title, start_page, headers):
            continue

        try:
            level = int(item["level"])
        except (KeyError, TypeError, ValueError):
            level = previous or 1

        level = max(1, min(level, MAX_LEVEL))
        # No entry may be deeper than one below the one before it, which also
        # forces the first entry to be a root.
        level = min(level, previous + 1)

        headers.append(ExtractedHeader(title=title, level=level, start_page=start_page))
        previous = level

    return headers


def extract_headers(contents: str) -> list[ExtractedHeader]:
    """Ask the model to turn a pasted contents page into ordered headings."""
    text = contents.strip()
    if not text:
        raise ExtractionError("No text was pasted.")
    if len(text) > MAX_INPUT_CHARS:
        raise ExtractionError(
            f"That is {len(text):,} characters. Paste the contents page only "
            f"(at most {MAX_INPUT_CHARS:,})."
        )

    try:
        reply = chat_json(
            system=SYSTEM_PROMPT,
            user=build_user_prompt(text),
            schema=HEADER_SCHEMA,
            schema_name="headers",
        )
    except LLMError as exc:
        raise ExtractionError(str(exc)) from exc

    raw = reply.get("headers")
    if not isinstance(raw, list):
        raise ExtractionError("The model's reply had no headers array.")

    headers = _clean([item for item in raw if isinstance(item, dict)])
    if not headers:
        raise ExtractionError("No headings were found in that text.")

    return headers


def save_headers(
    db: Session, doc_id: uuid.UUID, headers: Sequence[ExtractedHeader]
) -> int:
    """Replace a document's headers with ``headers``, nesting them by level.

    Returns how many rows were written. The parent of an entry is the closest
    preceding entry at a shallower level, which is what the level stack tracks.
    """
    db.execute(delete(Header).where(Header.doc_id == doc_id))

    # stack[i] holds the id of the most recent header at level i + 1.
    stack: list[int] = []

    for entry in headers:
        parent_id = stack[entry.level - 2] if entry.level > 1 else None

        row = Header(
            doc_id=doc_id,
            header=entry.title,
            parent_id=parent_id,
            start_page=entry.start_page,
        )
        db.add(row)
        # The id is needed as the parent of whatever comes next, so each row is
        # flushed as it is added rather than in one batch at the end.
        db.flush()

        del stack[entry.level - 1 :]
        stack.append(row.id)

    db.commit()
    return len(headers)


def document_headers(db: Session, doc_id: uuid.UUID) -> list[HeaderNode]:
    """A document's headers as a tree, in reading order.

    One query and one pass: rows come back in insertion order, which is reading
    order, so a parent is always seen before its children.
    """
    rows = db.scalars(
        select(Header).where(Header.doc_id == doc_id).order_by(Header.id)
    ).all()

    nodes: dict[int, HeaderNode] = {}
    roots: list[HeaderNode] = []

    for row in rows:
        parent = nodes.get(row.parent_id) if row.parent_id else None
        node = HeaderNode(
            id=row.id,
            title=row.header,
            level=(parent.level + 1) if parent else 1,
            start_page=row.start_page,
        )
        nodes[row.id] = node
        (parent.children if parent else roots).append(node)

    return roots


def header_count(db: Session, doc_id: uuid.UUID) -> int:
    return (
        db.scalar(
            select(func.count()).select_from(Header).where(Header.doc_id == doc_id)
        )
        or 0
    )


# ---- the gate in front of extraction ---------------------------------------


@dataclass(frozen=True)
class Candidate:
    """One document considered for extraction."""

    id: uuid.UUID
    filename: str
    header_count: int


@dataclass(frozen=True)
class GateReport:
    """Which of the selected documents extraction may proceed on."""

    method: ExtractionMethod
    eligible: list[Candidate]
    skipped: list[Candidate]
    #: (filename, reason) for documents whose has_header could not be settled.
    failed: list[tuple[str, str]]
    #: How many were UNCHECKED when the button was pressed and got a verdict.
    checked_now: int
    #: Document id → new has_header value, for repainting the rows on the page.
    updates: dict[str, str]


def prepare_for_extraction(
    db: Session, doc_ids: Sequence[uuid.UUID], method: ExtractionMethod
) -> GateReport:
    """Resolve has_header for the selection, then split it by verdict.

    An UNCHECKED document is checked first — that is the whole point of running
    this before extraction. NO is skipped. A document whose check could not be
    run is reported as failed rather than quietly treated as either.
    """
    report = check_has_header(db, doc_ids)

    failed = [(c.filename, c.error or "could not be checked") for c in report.failures]
    settled = {c.id for c in report.checks if c.outcome is not CheckOutcome.FAILED}

    documents = db.scalars(
        select(Document)
        .where(Document.id.in_([d for d in doc_ids if d in settled]))
        .order_by(Document.filename, Document.id)
    ).all()

    eligible: list[Candidate] = []
    skipped: list[Candidate] = []
    for document in documents:
        candidate = Candidate(
            id=document.id,
            filename=document.filename,
            header_count=header_count(db, document.id),
        )
        target = eligible if document.has_header is HasHeader.YES else skipped
        target.append(candidate)

    return GateReport(
        method=method,
        eligible=eligible,
        skipped=skipped,
        failed=failed,
        checked_now=report.decided,
        updates=report.updates,
    )
