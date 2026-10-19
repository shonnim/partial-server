r"""Filing a document's parsed blocks under its headers: the Section run.

A parse run leaves a content list — every block the parser read, in reading
order, each with a type, a bounding box and a page index. Header extraction
leaves the document's outline in ``headers`` with the page each heading
starts on. This module joins the two: it walks the content list from the top
and writes one ``header_contents`` row per block worth keeping, pointing at
the header the block sits under.

Which header a block sits under is decided by position, not by page alone.
Each header's *start* is the block that is its heading, located in the list
by title on the page ``start_page_idx`` names; a heading that cannot be
found on that page falls back to the first block of the page. A block belongs
to the most recent header whose start is at or before it. Blocks before the
first header's start — the cover, the contents page — belong to the document
and to no header, and are stored with ``header_id`` NULL.

A row holds the block's body in ``content``, and — for the block types the
parser prints one beside — the ``caption`` and the ``footnote`` that stood
with it on the page. The parser writes captions and footnotes as lists of
strings; a list is stored as one text with a newline between its parts, so a
caption split over two printed lines is kept as it was printed.

Every text stored here first comes off the parser's markdown escaping (see
``parser_text``): the backslashes it puts before ``*``, ``~`` and ``-`` are
its own, and a line opening ``\-`` is the bullet ∙ the page printed, not a
hyphen. Table bodies are the exception — they are HTML, never escaped, and
are stored exactly as they came.

What is stored, by block type:

* ``header``, ``footer``, ``aside_text``, ``page_number`` — page furniture,
  never stored.
* ``text``, ``page_footnote`` — stored when the text is not empty.
* ``list`` — the items joined as one content.
* ``table`` — the HTML body as the parser gave it, verbatim, in ``content``;
  ``table_caption`` and ``table_footnote`` in their own columns. Nothing is
  converted and no model is involved. A table the parser gave no body for
  keeps its caption and footnotes only.
* ``chart``, ``image`` — the crop is read from storage and summarised in
  Korean by the vision model (see ``image_description``), which is shown the
  caption, the footnote and the parser's own reading of the figure; the
  summary is the content, with ``chart_caption`` / ``chart_footnote`` or
  ``image_caption`` / ``image_footnote`` in their own columns.
* anything else with a text — stored under its own type.

A document is written all at once or not at all. Every figure is summarised
first, and only then are the previous rows replaced; a figure that cannot be
summarised fails the whole document with the reason, and the rows it had
before are left as they were.
"""

import logging
import time
import uuid
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.models.document import Document
from app.models.header import Header, HeaderContent
from app.services.header_extraction import _title_key, header_count, locate_heading
from app.services.headers import contents_page_index, load_content_list
from app.services.image_description import (
    FIGURE_TYPES,
    FigureDescriptionError,
    describe_figure_block,
)
from app.services.parser_text import unescape
from app.services.table_normalizer import join_fragments

logger = logging.getLogger(__name__)

# How many documents one click may queue. A document with a few dozen figures
# is minutes of model time, so a selection is capped rather than left to grow
# into an all-night job.
MAX_SECTION_BATCH = 50

# Block types that frame a page rather than carry its content.
SKIPPED_TYPES = frozenset({"header", "footer", "aside_text", "page_number"})

# Block types whose handling has not been decided yet. Left out, and counted,
# rather than stored in a shape that would have to be redone. Empty for now;
# kept so the next undecided type has a place to go.
DEFERRED_TYPES: frozenset[str] = frozenset()

# What goes between the items of a list block when they are stored as one
# content. Space, newline, space: each item on its own line, the bullets kept.
LIST_ITEM_JOIN = " \n "


@dataclass(frozen=True)
class SectionCandidate:
    """One selected document and whether the run will take it."""

    id: uuid.UUID
    filename: str
    header_count: int
    content_count: int
    #: Why the run leaves it out; None when it is eligible.
    reason: str | None = None

    @property
    def eligible(self) -> bool:
        return self.reason is None


@dataclass(frozen=True)
class SectionQueue:
    candidates: Sequence[SectionCandidate]

    @property
    def eligible(self) -> list[SectionCandidate]:
        return [c for c in self.candidates if c.eligible]

    @property
    def skipped(self) -> list[SectionCandidate]:
        return [c for c in self.candidates if not c.eligible]


@dataclass(frozen=True)
class SectionResult:
    """What happened to one document under a Section run."""

    id: uuid.UUID
    filename: str
    #: Rows written. None when the run failed before saving anything.
    saved: int | None = None
    #: Of those, rows stored with no header: blocks before the first heading.
    unassigned: int = 0
    #: Tables converted from their HTML.
    tables: int = 0
    #: Tables the parser gave no body for, stored with caption and notes only.
    tables_without_body: int = 0
    charts: int = 0
    #: Images described by the vision model.
    images: int = 0
    #: Blocks left out, by the reason: furniture, deferred, empty.
    skipped: dict[str, int] = field(default_factory=dict)
    seconds: float | None = None
    llm_seconds: float = 0.0
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None

    @property
    def skipped_total(self) -> int:
        return sum(self.skipped.values())


def content_count(db: Session, doc_id: uuid.UUID) -> int:
    return (
        db.scalar(
            select(func.count())
            .select_from(HeaderContent)
            .where(HeaderContent.doc_id == doc_id)
        )
        or 0
    )


# ---- reading the rows back ---------------------------------------------------

#: How many blocks one page of the contents pop-up holds. A document runs to
#: hundreds of them, so the pop-up asks for the next page rather than
#: everything at once.
CONTENTS_PAGE = 50


@dataclass(frozen=True)
class StoredBlock:
    """One ``header_contents`` row with the heading it was filed under."""

    row: HeaderContent
    #: The header's text; None for a block stored before the first heading.
    header: str | None


@dataclass(frozen=True)
class ContentsPage:
    """One page of a document's stored blocks, in the order they were filed."""

    blocks: list[StoredBlock]
    #: Rows the document has in all, not just on this page.
    total: int
    offset: int
    #: Where the next page starts; None when this page is the last.
    next_offset: int | None

    @property
    def remaining(self) -> int:
        return max(self.total - (self.offset + len(self.blocks)), 0)


def document_contents(
    db: Session, doc_id: uuid.UUID, offset: int = 0, limit: int = CONTENTS_PAGE
) -> ContentsPage:
    """One page of what a Section run stored for a document, in reading order.

    ``order`` is what the run wrote, so it is what the page is read back by;
    ``id`` settles the order of anything written before that column existed.
    The heading each block sits under is looked up for the page in hand, not
    per row.
    """
    total = content_count(db, doc_id)
    rows = list(
        db.scalars(
            select(HeaderContent)
            .where(HeaderContent.doc_id == doc_id)
            .order_by(HeaderContent.order, HeaderContent.id)
            .offset(offset)
            .limit(limit)
        ).all()
    )

    header_ids = {row.header_id for row in rows if row.header_id is not None}
    titles: dict[int, str] = {}
    if header_ids:
        titles = {
            header.id: header.header
            for header in db.scalars(select(Header).where(Header.id.in_(header_ids))).all()
        }

    seen = offset + len(rows)
    return ContentsPage(
        blocks=[
            StoredBlock(row=row, header=titles.get(row.header_id) if row.header_id else None)
            for row in rows
        ],
        total=total,
        offset=offset,
        next_offset=seen if seen < total else None,
    )


def build_queue(db: Session, doc_ids: Sequence[uuid.UUID]) -> SectionQueue:
    """Decide, for each selected document, whether the run will take it.

    A document that already has content rows is left alone: the tree disables
    its box, but a ticked folder resolves to everything beneath it, and a run
    over a folder must not quietly redo what was done. A document with no
    headers has nothing to file blocks under.
    """
    if not doc_ids:
        return SectionQueue(candidates=())

    documents = db.scalars(
        select(Document)
        .where(Document.id.in_(list(doc_ids)))
        .order_by(Document.filename, Document.id)
    ).all()

    candidates = []
    for doc in documents:
        headers = header_count(db, doc.id)
        contents = content_count(db, doc.id)
        if contents:
            reason = f"already sectioned: {contents} block{'' if contents == 1 else 's'} stored"
        elif not headers:
            reason = "no headers stored — extract them on the Headers page first"
        else:
            reason = None
        candidates.append(
            SectionCandidate(
                id=doc.id,
                filename=doc.filename,
                header_count=headers,
                content_count=contents,
                reason=reason,
            )
        )
    return SectionQueue(candidates=candidates)


# ---- where each header starts -----------------------------------------------


def _page_of(block: Any) -> int | None:
    if not isinstance(block, dict):
        return None
    page = block.get("page_idx")
    if isinstance(page, int) and not isinstance(page, bool) and page >= 0:
        return page
    return None


def _first_block_on(blocks: Sequence[Any], page: int) -> int | None:
    """Index of the first block whose page is ``page`` or later."""
    for index, block in enumerate(blocks):
        block_page = _page_of(block)
        if block_page is not None and block_page >= page:
            return index
    return None


def header_starts(headers: Sequence[Header], blocks: Sequence[Any]) -> list[int | None]:
    """The block index each header starts at, in the same order as ``headers``.

    The heading block is looked for by title, after the contents page. It is
    trusted when it sits on the page the header was recorded as starting on;
    otherwise the first block of that page stands in. A header with no
    recorded start page takes the located heading if there is one, and has no
    start at all if there is none — its blocks then fall to the header before
    it, which is honest about what is known.
    """
    contents = contents_page_index(blocks)
    starts: list[int | None] = []
    for header in headers:
        located = locate_heading(blocks, _title_key(header.header), contents)
        page = header.start_page_idx
        if page is None:
            starts.append(located.index if located else None)
        elif located is not None and located.page == page:
            starts.append(located.index)
        else:
            starts.append(_first_block_on(blocks, page))
    return starts


def assign_headers(
    headers: Sequence[Header], blocks: Sequence[Any]
) -> list[int | None]:
    """The header id each block belongs to, by block index; None before the first.

    Headers are ordered by where they start, so a block takes the latest
    header that has started by the time the walk reaches it.
    """
    starts = header_starts(headers, blocks)
    events = sorted(
        ((start, order, header.id) for order, (header, start) in enumerate(zip(headers, starts, strict=True)) if start is not None),
    )

    assigned: list[int | None] = []
    current: int | None = None
    pending = 0
    for index in range(len(blocks)):
        while pending < len(events) and events[pending][0] <= index:
            current = events[pending][2]
            pending += 1
        assigned.append(current)
    return assigned


# ---- one block, one row -----------------------------------------------------


def _bbox(block: dict[str, Any]) -> tuple[int | None, int | None, int | None, int | None]:
    raw = block.get("bbox")
    if not isinstance(raw, list) or len(raw) != 4:
        return (None, None, None, None)
    values = []
    for v in raw:
        values.append(v if isinstance(v, int) and not isinstance(v, bool) else None)
    return (values[0], values[1], values[2], values[3])


def _text_of(block: dict[str, Any]) -> str | None:
    text = block.get("text")
    if isinstance(text, str) and text.strip():
        return unescape(text)
    return None


def _list_text(block: dict[str, Any]) -> str | None:
    items = block.get("list_items")
    if not isinstance(items, list):
        return None
    kept = [unescape(item) for item in items if isinstance(item, str) and item.strip()]
    return LIST_ITEM_JOIN.join(kept) if kept else None


@dataclass
class _Tally:
    """Running counts while a document's blocks are turned into rows."""

    skipped: Counter = field(default_factory=Counter)
    tables: int = 0
    tables_without_body: int = 0
    charts: int = 0
    images: int = 0
    llm_seconds: float = 0.0


def _row_for(
    db: Session, document: Document, block: Any, tally: _Tally
) -> HeaderContent | None:
    """The row a block becomes, or None when it is not stored.

    Raises FigureDescriptionError for a chart or image the vision model could
    not summarise — the caller decides what that means for the document.
    """
    if not isinstance(block, dict):
        tally.skipped["not a block"] += 1
        return None

    kind = block.get("type")
    if not isinstance(kind, str) or not kind:
        tally.skipped["no type"] += 1
        return None
    if kind in SKIPPED_TYPES:
        tally.skipped["page furniture"] += 1
        return None
    if kind in DEFERRED_TYPES:
        tally.skipped[kind] += 1
        return None

    x0, y0, x1, y1 = _bbox(block)
    row = HeaderContent(
        type=kind,
        page_idx=_page_of(block),
        bbox_0=x0,
        bbox_1=y0,
        bbox_2=x1,
        bbox_3=y1,
    )

    if kind == "list":
        row.content = _list_text(block)
        if row.content is None:
            tally.skipped["empty list"] += 1
            return None
        return row

    if kind == "table":
        row.caption = join_fragments(block.get("table_caption"))
        row.footnote = join_fragments(block.get("table_footnote"))
        body = block.get("table_body")
        if not isinstance(body, str) or not body.strip():
            # No body: the caption and the notes are all the parser gave.
            tally.tables_without_body += 1
            return row
        # The HTML is stored as it came. Reading it is a later concern.
        row.content = body
        tally.tables += 1
        return row

    if kind in FIGURE_TYPES:
        # A chart and an image go the same way: the crop, the caption, the
        # footnote and the parser's reading all go to the vision model, and
        # its summary is the content.
        began = time.monotonic()
        described = describe_figure_block(db, document, block)
        tally.llm_seconds += time.monotonic() - began
        if kind == "chart":
            tally.charts += 1
        else:
            tally.images += 1
        row.content = described.text
        row.caption = described.caption
        row.footnote = described.footnote
        return row

    # text, page_footnote, and any other block that carries a text.
    row.content = _text_of(block)
    if row.content is None:
        tally.skipped[f"empty {kind}"] += 1
        return None
    return row


# ---- the run ----------------------------------------------------------------


def document_headers_in_order(db: Session, doc_id: uuid.UUID) -> list[Header]:
    """A document's headers in reading order — the order they were written."""
    return list(
        db.scalars(select(Header).where(Header.doc_id == doc_id).order_by(Header.id)).all()
    )


def run_sections(db: Session, document: Document) -> SectionResult:
    """File one document's blocks under its headers, replacing earlier rows."""
    began = time.monotonic()

    def failed(reason: str) -> SectionResult:
        return SectionResult(
            id=document.id,
            filename=document.filename,
            seconds=time.monotonic() - began,
            error=reason,
        )

    headers = document_headers_in_order(db, document.id)
    if not headers:
        return failed("no headers stored for this document")

    blocks, error = load_content_list(db, document)
    if error is not None:
        return failed(error)
    if not isinstance(blocks, list):
        return failed("the content list is not a JSON array of blocks")

    assigned = assign_headers(headers, blocks)

    tally = _Tally()
    rows: list[HeaderContent] = []
    for index, block in enumerate(blocks):
        page = _page_of(block)
        where = f"page_idx {page}" if page is not None else f"block {index}"
        try:
            row = _row_for(db, document, block, tally)
        except FigureDescriptionError as exc:
            return failed(f"{block.get('type')} at {where} could not be summarised: {exc}")
        if row is None:
            continue
        row.doc_id = document.id
        row.header_id = assigned[index]
        row.order = len(rows) + 1
        rows.append(row)

    # Everything that could fail has; the replacement is one transaction.
    db.execute(delete(HeaderContent).where(HeaderContent.doc_id == document.id))
    db.add_all(rows)
    db.commit()

    return SectionResult(
        id=document.id,
        filename=document.filename,
        saved=len(rows),
        unassigned=sum(1 for r in rows if r.header_id is None),
        tables=tally.tables,
        tables_without_body=tally.tables_without_body,
        charts=tally.charts,
        images=tally.images,
        skipped=dict(tally.skipped),
        seconds=time.monotonic() - began,
        llm_seconds=tally.llm_seconds,
    )
