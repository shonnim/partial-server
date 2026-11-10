"""Turning a table of contents into rows in the ``headers`` table.

A contents page is a tree flattened onto paper: indentation and numbering carry
the nesting, and nothing else does. The model is asked for that nesting as a
``level`` per entry rather than as nested objects — a flat list in reading order
is far easier to constrain, to validate, and to argue with when it comes out
wrong. The tree is rebuilt here, from the levels, with an explicit stack.

Re-running extraction corrects the previous attempt rather than appending a
second copy: entries are matched to the stored rows in reading order, matched
rows are updated in place — keeping their ids, and with them the section
contents already hanging off them — extra entries are inserted, and stored rows
with no entry left to match are deleted.
"""

import enum
import json
import re
import unicodedata
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, NamedTuple

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.llm import LLMError, chat_json
from app.models.document import Document, HasHeader
from app.models.header import Header
from app.services.headers import (
    CheckOutcome,
    check_has_header,
    contents_page_index,
    page_number_index,
)

# The deepest nesting a contents page is allowed to express. Six is already
# more than any document in this corpus uses; the ceiling exists so a model
# that starts counting paragraphs cannot write a thousand-deep tree.
MAX_LEVEL = 6

# A pasted contents page is a page or two of text. The cap is what separates
# "the user pasted a table of contents" from "the user pasted the whole PDF".
MAX_INPUT_CHARS = 40_000

# The models the page's picker offers, in the order the box lists them — the
# same pair the Summarizing page offers. The first is the default: the
# self-hosted text model everything else uses.
HEADER_LLMS: dict[str, str] = {
    "gemma": "Gemma 4 (local)",
    "solar": "Solar Pro 4 (Upstage)",
}
DEFAULT_LLM = "gemma"


def resolve_llm(llm: str | None) -> str:
    """The picker's value, or the default for anything it does not offer."""
    return llm if llm in HEADER_LLMS else DEFAULT_LLM


class ExtractionMethod(str, enum.Enum):
    """How the headers of a document are obtained.

    MANUAL is hand-fed: a person pastes the contents page. PAGES is automatic:
    the contents page is located in the content list, cut out of the PDF by the
    parsing service, and the resulting markdown takes the place of the paste.
    GROUP is PAGES again, but each document's page range (and model, when the
    strategy names one) comes from its group id's strategy instead of the
    toolbar — documents of one group share a layout, so they share a contents
    page location too. All three end in the same model call.
    """

    MANUAL = "manual"
    PAGES = "pages"
    GROUP = "group"

    @property
    def label(self) -> str:
        return _METHOD_LABELS[self]


_METHOD_LABELS = {
    ExtractionMethod.MANUAL: "Manual",
    ExtractionMethod.PAGES: "Pages",
    ExtractionMethod.GROUP: "Group",
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

# A parenthesised aside inside a title — usually the English term beside the
# Korean one: "공공 재정(Public Finances) 신뢰 강화". The contents page and
# the body heading routinely disagree on whether it is there.
_PAREN_ASIDE = re.compile(r"\([^()]*\)|（[^（）]*）")


def _title_key(title: str) -> str:
    """A title reduced to what survives copying: letters and digits, folded.

    NFKC first, so a roman numeral typed as one character ("Ⅰ.") and the same
    numeral typed as a letter ("I.") reduce to the same key.
    """
    return _TITLE_NOISE.sub("", unicodedata.normalize("NFKC", title)).casefold()


def _bare_title_key(title: str) -> str:
    """``_title_key`` with parenthesised asides dropped first.

    "공공 채무(Public Debt) 대응 전략" and "공공 채무 대응 전략" reduce to
    the same bare key, whichever of the contents page and the body heading
    carries the parenthesis — an aside *inserted* mid-title defeats the plain
    startswith match, which only forgives additions at the end.
    """
    return _title_key(_PAREN_ASIDE.sub(" ", title))


# ---- normalising a title before it is stored ---------------------------------

# The roman-numeral characters this corpus actually uses; the other numerals
# arrive as plain letters already.
_ROMAN_CHARS = {"Ⅱ": "II", "Ⅲ": "III"}

# What a trailing run may be made of before it counts as dot leaders: the
# dot-like characters parsers render leaders as, plus the spaces between them.
_DOT_CHARS = ".·․‥…"

# A title opening with its ordinal — a roman numeral or a short number — and
# the run of punctuation a design put between the ordinal and the text:
# "I. 서론", "I -- 서론", "2、개요". The separator must contain at least one
# character, so "Internet" cannot read as "I" + "nternet".
_LEADING_ORDINAL = re.compile(
    r"^(?P<num>[IVX]+|\d{1,2})"
    r"(?P<sep>[\s.．·ㆍ:：,、\-–—―]+)"
    r"(?P<rest>\S.*)$"
)


def normalize_title(title: str) -> str:
    """One extracted title, restyled the way the corpus numbers its headings.

    Three repairs, in order: the single-character roman numerals Ⅱ and Ⅲ
    become the letters II and III; dot leaders left dangling after the text
    ("1. 개요....") come off; and a title opening with an ordinal is forced
    into the "I. 서론" / "2. 개요" pattern whatever design sat between the
    ordinal and the text ("I -- 서론").

    Two shapes are left alone: hierarchical numbering ("1.2 예산"), whose dot
    is part of the ordinal rather than a separator, and a lone roman letter
    followed by an English word ("I think"), which is a sentence, not an
    ordinal.
    """
    text = title.strip()
    for char, plain in _ROMAN_CHARS.items():
        text = text.replace(char, plain)

    # Trailing dot leaders: strip the run only when it holds an ellipsis or at
    # least two dots — a sentence's single full stop is not a leader.
    stripped = text.rstrip(_DOT_CHARS + " \t")
    removed = text[len(stripped) :]
    if "…" in removed or sum(removed.count(c) for c in _DOT_CHARS) >= 2:
        text = stripped

    match = _LEADING_ORDINAL.match(text)
    if match is None:
        return text
    num, sep, rest = match.group("num", "sep", "rest")

    hierarchical = (
        num.isdigit() and sep.strip() in {".", "．"} and " " not in sep and rest[0].isdigit()
    )
    english_word = len(num) == 1 and not sep.strip() and rest[0].isascii() and rest[0].isalpha()
    if hierarchical or english_word:
        return text
    return f"{num}. {rest}"


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
    start_page_idx: int | None
    end_page_idx: int | None
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

    Body headings that echo an entry already taken are dropped on the way,
    and every title goes through ``normalize_title`` before it is kept.
    """
    headers: list[ExtractedHeader] = []
    previous = 0

    for item in raw:
        title = normalize_title(str(item.get("title") or ""))
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


def extract_headers(contents: str, llm: str | None = None) -> list[ExtractedHeader]:
    """Ask the model to turn a pasted contents page into ordered headings.

    ``llm`` is the page picker's value: the default is the configured text
    model; "solar" sends the same call to the Upstage Solar chat endpoint,
    which answers the same OpenAI-compatible shape and authenticates with the
    embedding key.
    """
    text = contents.strip()
    if not text:
        raise ExtractionError("No text was pasted.")
    if len(text) > MAX_INPUT_CHARS:
        raise ExtractionError(
            f"That is {len(text):,} characters. Paste the contents page only "
            f"(at most {MAX_INPUT_CHARS:,})."
        )

    endpoint: dict[str, str] = {}
    if resolve_llm(llm) == "solar":
        settings = get_settings()
        endpoint = {
            "base_url": settings.solar_base_url,
            "api_key": settings.upstage_api_key,
            "model": settings.solar_model,
        }

    try:
        reply = chat_json(
            system=SYSTEM_PROMPT,
            user=build_user_prompt(text),
            schema=HEADER_SCHEMA,
            schema_name="headers",
            **endpoint,
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


# Block types that frame a page rather than carry its body: a running header,
# a footer, the printed page number, a footnote. They are skipped when asking
# what precedes a heading on the page.
_PAGE_FURNITURE = frozenset({"header", "footer", "page_number", "page_footnote"})


@dataclass(frozen=True)
class LocatedHeading:
    """Where a header's heading block sits in the content list."""

    #: Position of the block in the content list.
    index: int
    #: The block's ``page_idx``.
    page: int


def _page_of(block: Any) -> int | None:
    page = block.get("page_idx")
    if isinstance(page, int) and not isinstance(page, bool) and page >= 0:
        return page
    return None


def locate_heading(
    blocks: Sequence[Any], title: str, after_page: int | None, *, after_index: int = -1
) -> LocatedHeading | None:
    """The body block that is the heading for ``title``, or None.

    A text block matches when its ``_title_key`` starts with the title's —
    body headings often append what the contents list left out, like the
    English title in brackets — or, failing that, when the two *bare* keys
    match the same way: an aside inserted mid-title ("공공 채무(Public Debt)
    대응 전략" against the list's "공공 채무 대응 전략") breaks the plain
    prefix, whichever side carries it. Blocks up to and including
    ``after_page`` are skipped: that is where the contents page lists the
    same title, and the listing is not the heading. Blocks at or before
    ``after_index`` are skipped too — a caller walking headers in reading
    order passes the previous heading's index, so a title repeated across
    chapters resolves to its own occurrence rather than the first.

    A block carrying ``text_level`` is what the parser called a heading and is
    preferred. When no matching block carries it, the first matching body
    block is taken instead — a heading the parser rendered as plain text.
    """
    key = _title_key(title)
    if not key:
        return None
    bare = _bare_title_key(title)

    fallback: LocatedHeading | None = None
    for index, block in enumerate(blocks):
        if index <= after_index:
            continue
        if not isinstance(block, dict) or block.get("type") != "text":
            continue
        page = _page_of(block)
        if page is None or (after_page is not None and page <= after_page):
            continue
        text = block.get("text")
        if not isinstance(text, str):
            continue
        if not _title_key(text).startswith(key) and not (
            bare and _bare_title_key(text).startswith(bare)
        ):
            continue
        if "text_level" in block:
            return LocatedHeading(index=index, page=page)
        if fallback is None:
            fallback = LocatedHeading(index=index, page=page)
    return fallback


def starts_page(blocks: Sequence[Any], heading: LocatedHeading) -> bool:
    """Whether the heading is the first body block on its page.

    The block before it — ignoring page furniture — is on an earlier page when
    the heading opens the page; on the same page when text of the previous
    section (or a chart of it) comes first.
    """
    for index in range(heading.index - 1, -1, -1):
        block = blocks[index]
        if not isinstance(block, dict) or block.get("type") in _PAGE_FURNITURE:
            continue
        return _page_of(block) != heading.page
    return True


def last_page_index(blocks: Sequence[Any]) -> int | None:
    known = [p for p in (_page_of(b) for b in blocks if isinstance(b, dict)) if p is not None]
    return max(known) if known else None


def locate_starts(
    headers: Sequence[ExtractedHeader], blocks: list[Any]
) -> tuple[list[int | None], list[LocatedHeading | None]]:
    """Each entry's start ``page_idx``, and the heading block it was read from.

    The start is the page of the body block that is the entry's heading (see
    ``locate_heading``). The entry's printed ``start_page`` is a cross-check,
    not a requirement: when the page that number was printed on disagrees, the
    start is left unknown rather than trusted, but a number the content list
    never captured has nothing to disagree with and the located heading stands
    on its own. A heading that cannot be located at all falls back to the page
    its printed number sits on — the content list knows the page even when the
    heading's text does not match. The located entry is None then.
    """
    contents = contents_page_index(blocks)
    printed = page_number_index(blocks)

    located = [locate_heading(blocks, h.title, contents) for h in headers]

    starts: list[int | None] = []
    for entry, heading in zip(headers, located, strict=True):
        promised = printed.get(entry.start_page) if entry.start_page is not None else None
        if heading is not None:
            agrees = promised is None or promised == heading.page
            starts.append(heading.page if agrees else None)
        elif promised is not None and (contents is None or promised > contents):
            # The > contents guard keeps a duplicated printed number — front
            # matter restarting at 1 — from dragging a body section before
            # the contents page.
            starts.append(promised)
        else:
            starts.append(None)
    return starts, located


def page_index_ranges(
    headers: Sequence[ExtractedHeader], blocks: Any
) -> list[tuple[int | None, int | None]]:
    """``(start_page_idx, end_page_idx)`` for each entry, in the same order.

    ``blocks`` is a document's content list. Both ends are read from it:

    * **start** comes from ``locate_starts``: the located heading block,
      cross-checked against the printed page number.
    * **end** looks at the next entry at the same level or shallower — the
      next section for a section, the next chapter for a chapter's last
      section. If that heading opens its page the entry ends on the page
      before; if it shares the page with what came before, the entry runs onto
      that page too — as it also does when the following start is known only
      from its printed number, where opening the page cannot be checked. The
      last entry at its level runs to the last page.
    """
    if not isinstance(blocks, list):
        return [(None, None)] * len(headers)

    last = last_page_index(blocks)
    starts, located = locate_starts(headers, blocks)

    ranges: list[tuple[int | None, int | None]] = []
    for i, entry in enumerate(headers):
        following = next(
            (j for j in range(i + 1, len(headers)) if headers[j].level <= entry.level),
            None,
        )
        heading = located[following] if following is not None else None
        if following is None:
            end = last
        elif starts[following] is None:
            end = None
        elif heading is None or heading.page != starts[following]:
            end = starts[following]
        elif starts_page(blocks, heading):
            end = starts[following] - 1
        else:
            end = starts[following]
        ranges.append((starts[i], end))
    return ranges


def save_headers(
    db: Session,
    doc_id: uuid.UUID,
    headers: Sequence[ExtractedHeader],
    blocks: Any = None,
    *,
    ranges: Sequence[tuple[int | None, int | None]] | None = None,
) -> int:
    """Write ``headers`` as the document's outline, nesting them by level.

    Returns how many rows the document now has. The parent of an entry is the
    closest preceding entry at a shallower level, which is what the level stack
    tracks.

    Stored rows are not thrown away wholesale: entries are matched to them in
    reading order (rows come back in id order, which is reading order), each
    matched row is updated in place so its id — and the section contents keyed
    on it — survives the redo, entries past the stored rows are inserted, and
    stored rows past the entries are deleted, taking their contents with them.

    ``blocks`` is the document's content list, used to fill ``start_page_idx``
    and ``end_page_idx``; without it both stay NULL. A group strategy that
    computed the indexes its own way passes them as ``ranges``, one pair per
    entry, and the content-list matching is skipped.
    """
    existing = db.scalars(
        select(Header).where(Header.doc_id == doc_id).order_by(Header.id)
    ).all()

    if ranges is None:
        ranges = page_index_ranges(headers, blocks)

    # stack[i] holds the id of the most recent header at level i + 1.
    stack: list[int] = []

    for position, (entry, (start_idx, end_idx)) in enumerate(
        zip(headers, ranges, strict=True)
    ):
        parent_id = stack[entry.level - 2] if entry.level > 1 else None

        if position < len(existing):
            row = existing[position]
            row.header = entry.title
            row.parent_id = parent_id
            row.level = entry.level
            row.start_page = entry.start_page
            row.start_page_idx = start_idx
            row.end_page_idx = end_idx
        else:
            row = Header(
                doc_id=doc_id,
                header=entry.title,
                parent_id=parent_id,
                level=entry.level,
                start_page=entry.start_page,
                start_page_idx=start_idx,
                end_page_idx=end_idx,
            )
            db.add(row)
            # The id is needed as the parent of whatever comes next, so each
            # row is flushed as it is added rather than in one batch at the end.
            db.flush()

        del stack[entry.level - 1 :]
        stack.append(row.id)

    # A stored row can only parent rows behind it in reading order, so the
    # leftover tail never parents a row that was kept; the cascade takes the
    # tail's own descendants and contents with it.
    leftover = [row.id for row in existing[len(headers) :]]
    if leftover:
        db.execute(delete(Header).where(Header.id.in_(leftover)))

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
            level=row.level,
            start_page=row.start_page,
            start_page_idx=row.start_page_idx,
            end_page_idx=row.end_page_idx,
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


# ---- hand-editing the stored page indexes -----------------------------------


def _edited_index(value: Any) -> int | None:
    """One box of the Edit form: a whole page index, or None for an empty box."""
    if value is None or value == "":
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("A page index must be a whole number.")
    if value < 0:
        raise ValueError("A page index cannot be negative.")
    return value


class IndexEdit(NamedTuple):
    """One row of the Edit form: the printed page and both page indexes."""

    page: int | None
    start: int | None
    end: int | None
    #: False when the payload carried no "page" key at all — a browser still
    #: running the serialiser from before the page box existed. Saving such a
    #: payload must leave start_page alone rather than clear it.
    touch_page: bool = True


def parse_index_edits(raw: str) -> dict[int, IndexEdit]:
    """The Edit form's payload: header id → (start_page, start/end page_idx).

    ``raw`` is the JSON the outline pop-up serialises, shaped like
    ``{"12": {"page": 5, "start": 3, "end": 7}}`` with null for a box left
    empty. Anything that is not a whole page number — a negative number, a
    fraction, an end index before its start — raises ValueError with a message
    fit for the dialog.
    """
    unreadable = ValueError("The edited indexes could not be read.")

    try:
        data = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError as exc:
        raise unreadable from exc
    if not isinstance(data, dict):
        raise unreadable

    edits: dict[int, IndexEdit] = {}
    for key, value in data.items():
        try:
            header_id = int(key)
        except (TypeError, ValueError):
            raise unreadable from None
        if not isinstance(value, dict):
            raise unreadable

        page = _edited_index(value.get("page"))
        start = _edited_index(value.get("start"))
        end = _edited_index(value.get("end"))
        if start is not None and end is not None and end < start:
            raise ValueError("An end index cannot be before its start.")
        edits[header_id] = IndexEdit(page, start, end, touch_page="page" in value)

    return edits


def update_page_indexes(
    db: Session,
    doc_id: uuid.UUID,
    edits: dict[int, IndexEdit],
) -> int:
    """Overwrite the edited headers' page fields; returns how many changed.

    Scoped to one document, so an id belonging to another document's outline
    cannot be reached. An id that no longer exists is skipped rather than an
    error — the outline on screen can be older than the table behind it.
    """
    if not edits:
        return 0

    rows = db.scalars(
        select(Header).where(Header.doc_id == doc_id, Header.id.in_(edits))
    ).all()

    changed = 0
    for row in rows:
        edit = edits.get(row.id)
        if edit is None:
            continue
        page = edit.page if edit.touch_page else row.start_page
        if (row.start_page, row.start_page_idx, row.end_page_idx) == (
            page,
            edit.start,
            edit.end,
        ):
            continue
        row.start_page, row.start_page_idx, row.end_page_idx = (
            page,
            edit.start,
            edit.end,
        )
        changed += 1

    db.commit()
    return changed


# ---- the gate in front of extraction ---------------------------------------


@dataclass(frozen=True)
class Candidate:
    """One document considered for extraction."""

    id: uuid.UUID
    filename: str
    header_count: int
    #: The Grouping page's label, which is what picks the strategy under the
    #: Group method; None for a document never grouped.
    group_id: str | None = None


@dataclass(frozen=True)
class GateReport:
    """Which of the selected documents extraction may proceed on."""

    method: ExtractionMethod
    eligible: list[Candidate]
    skipped: list[Candidate]
    #: Documents left alone because their headers are already stored and the
    #: Force toggle was off. A folder path can resolve to them even though
    #: their rows on the page are disabled — rows still behind "load more"
    #: have no box to disable — so the gate has to hold them back itself.
    stored: list[Candidate]
    #: (filename, reason) for documents whose has_header could not be settled.
    failed: list[tuple[str, str]]
    #: How many were UNCHECKED when the button was pressed and got a verdict.
    checked_now: int
    #: Document id → new has_header value, for repainting the rows on the page.
    updates: dict[str, str]


def prepare_for_extraction(
    db: Session,
    doc_ids: Sequence[uuid.UUID],
    method: ExtractionMethod,
    *,
    force: bool = False,
) -> GateReport:
    """Resolve has_header for the selection, then split it by verdict.

    An UNCHECKED document is checked first — that is the whole point of running
    this before extraction. NO is skipped. A document whose check could not be
    run is reported as failed rather than quietly treated as either. A document
    whose headers are already stored is held back too — re-doing one is a
    deliberate act — unless ``force`` says that is exactly what is meant.
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
    stored: list[Candidate] = []
    for document in documents:
        candidate = Candidate(
            id=document.id,
            filename=document.filename,
            header_count=header_count(db, document.id),
            group_id=document.group_id,
        )
        if document.has_header is not HasHeader.YES:
            skipped.append(candidate)
        elif candidate.header_count and not force:
            stored.append(candidate)
        else:
            eligible.append(candidate)

    return GateReport(
        method=method,
        eligible=eligible,
        skipped=skipped,
        stored=stored,
        failed=failed,
        checked_now=report.decided,
        updates=report.updates,
    )
