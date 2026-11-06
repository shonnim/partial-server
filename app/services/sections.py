r"""Filing a document's parsed blocks under its headers: the Section run.

A parse run leaves a content list — every block the parser read, in reading
order, each with a type, a bounding box and a page index. Header extraction
leaves the document's outline in ``headers`` with the page each heading
starts on. This module joins the two: it walks the content list from the top
and writes one ``header_contents`` row per block worth keeping, pointing at
the header the block sits under.

Which header a block sits under is decided by position, not by page alone.
Each header's *start* is the block that is its heading, located in the list
by title — sequentially, each heading only after the one before it, so a
title repeated across chapters resolves to its own occurrence. A located
heading on the page ``start_page_idx`` names is trusted outright; when the
two disagree, the first block of the recorded page stands in, unless the
located heading sits before that block — trusting the page there would file
the heading's own text under the header before it, which is how a level-2
title ends up stored as its level-1 parent's text. A block belongs to the
most recent header whose start is at or before it. Blocks before the first
header's start — the cover, the contents page — belong to the document and
to no header, and are stored with ``header_id`` NULL.

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
  ``image_caption`` / ``image_footnote`` in their own columns. On a
  re-section, a figure whose identity has not changed — same type, page,
  bounding box, caption and footnote — keeps the description the previous
  run stored instead of going back to the model; only a figure the previous
  rows do not cover is summarised anew.
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

from app.models.chunk import Chunk
from app.models.document import Document
from app.models.header import Header, HeaderContent
from app.services.header_extraction import header_count, locate_heading
from app.services.headers import contents_page_index, load_content_list
from app.services.image_description import (
    FIGURE_TYPES,
    FigureDescriptionError,
    describe_figure_block,
    figure_texts,
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
    #: Of the figures, those whose description was reused from the previous
    #: rows rather than asked of the model again.
    figures_reused: int = 0
    #: Blocks left out, by the reason: furniture, deferred, empty.
    skipped: dict[str, int] = field(default_factory=dict)
    #: Chunks of an earlier Chunking run removed before the rows were replaced.
    chunks_removed: int = 0
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


def build_queue(
    db: Session, doc_ids: Sequence[uuid.UUID], *, force: bool = False
) -> SectionQueue:
    """Decide, for each selected document, whether the run will take it.

    A document that already has content rows is left alone: the tree disables
    its box, but a ticked folder resolves to everything beneath it, and a run
    over a folder must not quietly redo what was done. ``force`` — the page's
    "force re-section" box — takes those documents back in: the run removes
    their chunks and replaces their rows. A document with no headers has
    nothing to file blocks under either way.
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
        if not headers:
            reason = "no headers stored — extract them on the Headers page first"
        elif contents and not force:
            reason = f"already sectioned: {contents} block{'' if contents == 1 else 's'} stored"
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


def _first_block_on(blocks: Sequence[Any], page: int, after: int = -1) -> int | None:
    """Index of the first block past ``after`` whose page is ``page`` or later."""
    for index, block in enumerate(blocks):
        if index <= after:
            continue
        block_page = _page_of(block)
        if block_page is not None and block_page >= page:
            return index
    return None


def header_starts(headers: Sequence[Header], blocks: Sequence[Any]) -> list[int | None]:
    """The block index each header starts at, in the same order as ``headers``.

    ``headers`` come in reading order, and each heading is looked for only
    past the one before it (``floor``) — so a title repeated across chapters
    resolves to its own occurrence, and the starts can never run backwards
    and steal an earlier header's blocks.

    The heading block is looked for by title, after the contents page. It is
    trusted when it sits on the page the header was recorded as starting on,
    and when there is no recorded page to check against. When the two
    disagree, the first block of the recorded page stands in — unless the
    located heading sits *before* that block: trusting the page there would
    file the heading's own text under the header before it (a level-2 title
    stored as its level-1 parent's text), so the located heading wins. A
    header with neither a locatable heading nor a recorded page has no start
    at all — its blocks then fall to the header before it, which is honest
    about what is known.
    """
    contents = contents_page_index(blocks)
    starts: list[int | None] = []
    floor = -1
    for header in headers:
        located = locate_heading(blocks, header.header, contents, after_index=floor)
        page = header.start_page_idx
        if page is None:
            start = located.index if located else None
        elif located is not None and located.page == page:
            start = located.index
        else:
            start = _first_block_on(blocks, page, after=floor)
            if located is not None and (start is None or located.index < start):
                start = located.index
        if start is not None:
            floor = start
        starts.append(start)
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
    figures_reused: int = 0
    llm_seconds: float = 0.0


def _figure_key(
    kind: str,
    page_idx: int | None,
    bbox: tuple[int | None, int | None, int | None, int | None],
    caption: str | None,
    footnote: str | None,
) -> tuple:
    """A figure's identity across runs, for reusing its stored description.

    Everything in it comes from the parse output, not the model: a figure the
    parser printed the same way is the same figure, whatever the crop's file
    name happens to be this run.
    """
    return (kind, page_idx, *bbox, caption, footnote)


def _stored_figure_descriptions(db: Session, doc_id: uuid.UUID) -> dict[tuple, str]:
    """The figure descriptions an earlier run stored, keyed by `_figure_key`.

    A re-section reads these before replacing the rows: a description the
    vision model already wrote is reused, not asked for again. Empty for a
    document sectioned for the first time.
    """
    rows = db.scalars(
        select(HeaderContent).where(
            HeaderContent.doc_id == doc_id,
            HeaderContent.type.in_(FIGURE_TYPES),
        )
    ).all()
    return {
        _figure_key(
            row.type,
            row.page_idx,
            (row.bbox_0, row.bbox_1, row.bbox_2, row.bbox_3),
            row.caption,
            row.footnote,
        ): row.content
        for row in rows
        if row.content and row.content.strip()
    }


def _row_for(
    db: Session,
    document: Document,
    block: Any,
    tally: _Tally,
    reuse: dict[tuple, str] | None = None,
) -> HeaderContent | None:
    """The row a block becomes, or None when it is not stored.

    ``reuse`` is the previous rows' figure descriptions by ``_figure_key``; a
    figure found there keeps its description without a model call. Raises
    FigureDescriptionError for a chart or image the vision model could not
    summarise — the caller decides what that means for the document.
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
        # its summary is the content — unless the previous rows already hold
        # this figure's description, which is then reused as it stands.
        caption, footnote, _reading = figure_texts(block)
        stored = (reuse or {}).get(
            _figure_key(kind, row.page_idx, (x0, y0, x1, y1), caption, footnote)
        )
        if stored is not None:
            row.content = stored
            row.caption = caption
            row.footnote = footnote
            tally.figures_reused += 1
        else:
            began = time.monotonic()
            described = describe_figure_block(db, document, block)
            tally.llm_seconds += time.monotonic() - began
            row.content = described.text
            row.caption = described.caption
            row.footnote = described.footnote
        if kind == "chart":
            tally.charts += 1
        else:
            tally.images += 1
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
    """File one document's blocks under its headers, replacing earlier rows.

    A re-run also removes the document's chunks: they were built from the
    rows being replaced, so they cannot outlive them.
    """
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

    # What the previous run's vision model wrote, read before the rows are
    # replaced: a figure that has not changed keeps its description for free.
    reuse = _stored_figure_descriptions(db, document.id)

    tally = _Tally()
    rows: list[HeaderContent] = []
    for index, block in enumerate(blocks):
        page = _page_of(block)
        where = f"page_idx {page}" if page is not None else f"block {index}"
        try:
            row = _row_for(db, document, block, tally, reuse)
        except FigureDescriptionError as exc:
            return failed(f"{block.get('type')} at {where} could not be summarised: {exc}")
        if row is None:
            continue
        row.doc_id = document.id
        row.header_id = assigned[index]
        row.order = len(rows) + 1
        rows.append(row)

    # Everything that could fail has; the replacement is one transaction.
    # The document's chunks go first: they were cut from the rows being
    # replaced (head_content_ids), so a re-section must not leave them
    # pointing at rows that no longer exist. Their vectors are reconciled by
    # the embedding stage, which deletes points no current chunk owns.
    chunks_removed = db.execute(
        delete(Chunk).where(Chunk.doc_id == document.id)
    ).rowcount
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
        figures_reused=tally.figures_reused,
        skipped=dict(tally.skipped),
        chunks_removed=chunks_removed or 0,
        seconds=time.monotonic() - began,
        llm_seconds=tally.llm_seconds,
    )
