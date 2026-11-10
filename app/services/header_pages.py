"""The Pages method: read the contents page out of the PDF, then extract.

The manual method needs someone to copy a contents page out of a PDF by hand.
This one does the copying: the parsing service is asked for the page range
typed into the toolbar, and the markdown it returns goes through the same
model call the paste would have.

One document at a time, on purpose. A call to the parsing service is a VLM
pass over several pages and the model call after it is not quick either, so
the browser walks the queue and asks for one document per request. That is
what keeps a batch of fifty from being one fifty-minute HTTP request.

Nothing here guesses. If the range is not valid, if the PDF cannot be read,
if the parsed pages turn out not to hold a contents page — the document is
reported as failed with the reason, and no headers are written.
"""

import re
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from app.doc_extract import DocExtractError, parse_pages
from app.models.document import Document, HasHeader
from app.services.header_bpa import (
    locate_toc,
    page_index_ranges_bpa,
    page_index_ranges_bpa2,
    parse_toc,
    parse_toc_bpa2,
)
from app.services.header_extraction import (
    ExtractedHeader,
    ExtractionError,
    extract_headers,
    save_headers,
)
from app.services.headers import find_marker, load_content_list
from app.storage import ObjectNotFound, read_object

# The page range typed into the toolbar: 1-based page numbers, the way a PDF
# viewer counts them, inclusive at both ends. The parser's start_page_id and
# end_page_id are 0-based, so each is sent one lower. The same range is used
# for every document in a queue.
DEFAULT_START_PAGE = 3
DEFAULT_END_PAGE = 3
# The most pages past the start one request may ask for: every page is a VLM
# pass, so a typo like 3..300 would hold the queue for an hour.
MAX_PAGE_SPAN = 10

# The ceiling for a range a strategy's locate hook derived instead. The cap
# above guards against toolbar typos; a located range is read off the
# document's own content list, and a real TOC can run long — the 분석종합
# volume's is ~48 pages — so it gets more room, not none.
MAX_LOCATED_SPAN = 60

# The whole PDF is uploaded to the parsing service, whichever pages it is
# asked for, so a source file has to fit in memory here first.
MAX_PDF_BYTES = 200 * 1024 * 1024


@dataclass(frozen=True)
class GroupStrategy:
    """How the Group method reads one group's contents pages.

    Documents of one group share a layout, so they share where the contents
    page sits. ``llm`` names the model every extraction of the group is sent
    to; None means the toolbar's pick stands.

    A group whose TOC is regular enough to parse without a model sets the
    hooks: ``locate`` reads the TOC's own page range off the document's
    content list (1-based viewer pages; None falls back to the static range),
    ``extract`` takes the parsed pages' markdown — whole, not cut by
    ``contents_section`` — and returns the ordered headings, and ``ranges``
    replaces ``page_index_ranges`` with the group's own page arithmetic. A
    strategy with its own ``extract`` also skips the contents-marker gate:
    its parser validates the pages far more strictly than a marker word, and
    this series renders the marker as a running header the pages-parse
    markdown drops. Any hook may be set alone; unset means the default step.
    """

    start_page: int
    end_page: int
    llm: str | None = None
    locate: Callable[[Any], tuple[int, int] | None] | None = None
    extract: Callable[[str], list[ExtractedHeader]] | None = None
    ranges: (
        Callable[[Sequence[ExtractedHeader], Any], list[tuple[int | None, int | None]]]
        | None
    ) = None


# What a document with no group — or a group with no entry below — gets under
# the Group method: the same defaults the Pages method starts from. This is
# the default way; everything extracted before groups existed went through it.
DEFAULT_STRATEGY = GroupStrategy(DEFAULT_START_PAGE, DEFAULT_END_PAGE)

# The strategy for each group id, the way chunking's CHUNKERS maps a group to
# the class that knows its layout. Add an entry here once a group's contents
# page location has been tuned on a few of its documents.
GROUP_STRATEGIES: dict[str, GroupStrategy] = {
    # NABO 예산안 부처별 분석: a multi-page TOC behind the cover, inner title
    # page and 발간사, parsed by pattern rather than by model — see
    # app/services/header_bpa.py for the layout. The static range is only the
    # fallback for a content list locate_toc cannot read.
    "bpa-1": GroupStrategy(
        start_page=3,
        end_page=12,
        locate=locate_toc,
        extract=parse_toc,
        ranges=page_index_ranges_bpa,
    ),
    # The series' other volumes — 거시·총량 분석, 성인지 예산서 분석,
    # 분석종합 — share one agency-less layout: parts and chapters (분석종합),
    # roman sections, numbered entries, hangul sub-entries (성인지). Same
    # locate as bpa-1: the CONTENTS marker opens the TOC and printed p.3
    # opens the body in every volume.
    "bpa-2": GroupStrategy(
        start_page=3,
        end_page=12,
        locate=locate_toc,
        extract=parse_toc_bpa2,
        ranges=page_index_ranges_bpa2,
    ),
}


def strategy_for(group_id: str | None) -> GroupStrategy:
    """The strategy a document's group prescribes, or the default."""
    if group_id is None:
        return DEFAULT_STRATEGY
    return GROUP_STRATEGIES.get(group_id, DEFAULT_STRATEGY)

# A markdown heading line, and a line that ends the way a contents entry does:
# with a page number, optionally after dot leaders.
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(?P<text>.+?)\s*#*\s*$")
_ENDS_WITH_PAGE = re.compile(r"(?:[.·…\s]+|/\s*)\d{1,4}\s*$")

# Fewer entry lines than this after the marker means the cut landed too early
# (a contents page whose entries the parser rendered as headings), and the
# uncut text is used instead.
_MIN_CONTENTS_LINES = 2


@dataclass(frozen=True)
class PagesResult:
    """What happened to one document under the Pages method."""

    id: uuid.UUID
    filename: str
    #: Rows written. None when the run failed before saving anything.
    saved: int | None = None
    #: The pages parsed, 1-based as typed (after trimming to the last page).
    start_page: int | None = None
    end_page: int | None = None
    parse_seconds: float | None = None
    llm_seconds: float | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


def range_error(start: int, end: int) -> str | None:
    """Why ``start``..``end`` cannot be sent to the parser, or None if it can."""
    if start < 1:
        return "start page must be 1 or more"
    if end < start:
        return "end page must not be before the start page"
    if end - start > MAX_PAGE_SPAN:
        return f"at most {MAX_PAGE_SPAN} pages past the start page"
    return None


def page_range(start: int, end: int, total: int | None) -> tuple[int, int] | None:
    """The 1-based inclusive range to parse, clamped to the document's length.

    ``total`` is the page count a parse recorded, when one did. An end that
    runs past the last page is trimmed rather than rejected: the contents page
    of a short document is still worth reading. A start past the last page
    leaves nothing to read, and gives None.
    """
    if total is None:
        return start, end
    if start > total:
        return None
    return start, min(end, total)


def contents_section(markdown: str) -> str:
    """The slice of ``markdown`` that is the table of contents, and nothing else.

    The pages sent to the parser run past the contents page on purpose — a
    long contents list spills onto a second page — so what comes back has the
    first chapter's opening behind it. Sent whole, the model reads that
    chapter heading as one more entry. Both ends are cut here:

    * everything before the marker line (cover, credits, preface) goes;
    * everything from the first markdown heading after the marker goes, unless
      that heading still ends in a page number, which makes it a contents
      entry the parser chose to render as a heading.

    Lists of tables and figures ("표 목차", "그림 목차") come after the real
    list and start with a heading of their own, so the same cut drops them.

    A cut that leaves almost nothing is not trusted: the text from the marker
    on is returned uncut, and the model's own instructions have to cope.
    """
    lines = markdown.splitlines()

    start = next((i for i, line in enumerate(lines) if find_marker(line)), None)
    if start is None:
        return markdown

    end = len(lines)
    for i in range(start + 1, len(lines)):
        match = _HEADING.match(lines[i])
        if match is None:
            continue
        text = match.group("text")
        if find_marker(text) or _ENDS_WITH_PAGE.search(text):
            continue
        end = i
        break

    entries = [line for line in lines[start + 1 : end] if line.strip()]
    if len(entries) < _MIN_CONTENTS_LINES:
        return "\n".join(lines[start:])
    return "\n".join(lines[start:end])


def run_pages(
    db: Session,
    document: Document,
    start_page: int,
    end_page: int,
    llm: str | None = None,
    strategy: GroupStrategy | None = None,
) -> PagesResult:
    """Extract and store the headers of one document from its PDF pages.

    ``start_page`` and ``end_page`` are 1-based and inclusive; the parser gets
    them one lower, as its 0-based ``start_page_id`` and ``end_page_id``.
    ``llm`` is the page picker's value, handed on to ``extract_headers``.
    ``strategy`` carries a group's hooks, when the Group method found any: a
    custom ``extract`` gets the markdown whole and replaces the model call,
    and a custom ``ranges`` replaces the content-list matching with the
    group's own page arithmetic.
    """

    def failed(
        reason: str,
        *,
        start_page: int | None = None,
        end_page: int | None = None,
        parse_seconds: float | None = None,
    ) -> PagesResult:
        return PagesResult(
            id=document.id,
            filename=document.filename,
            start_page=start_page,
            end_page=end_page,
            parse_seconds=parse_seconds,
            error=reason,
        )

    error = range_error(start_page, end_page)
    if error is not None:
        return failed(error)

    if document.has_header is not HasHeader.YES:
        return failed("not marked as having a contents page — run Has Header first")

    # The content list fills each header's page indexes when it is saved — and
    # a strategy with a locate hook reads the TOC's own page range off it,
    # replacing the static range it was called with.
    blocks, error = load_content_list(db, document)
    if error is not None:
        return failed(error)

    if strategy is not None and strategy.locate is not None:
        located = strategy.locate(blocks)
        if located is not None:
            start_page, end_page = located
            end_page = min(end_page, start_page + MAX_LOCATED_SPAN)

    clamped = page_range(start_page, end_page, document.pages)
    if clamped is None:
        return failed(
            f"start page {start_page} is past the last page "
            f"(the document has {document.pages})"
        )
    start, end = clamped

    try:
        pdf, oversized = read_object(
            document.bucket, document.s3_key, max_bytes=MAX_PDF_BYTES
        )
    except ObjectNotFound as exc:
        return failed(f"PDF missing from storage: {exc}")
    if oversized:
        return failed(f"PDF is larger than {MAX_PDF_BYTES // (1024 * 1024)} MB")

    began = time.monotonic()
    try:
        markdown = parse_pages(
            pdf, document.filename, start_page=start - 1, end_page=end - 1
        )
    except DocExtractError as exc:
        return failed(str(exc), start_page=start, end_page=end)
    parse_seconds = time.monotonic() - began

    # A range that misses the contents page parses to body text, and body text
    # sent to the model would come back as invented headings, so it is refused
    # here. A strategy with its own extract is exempt: its parser refuses
    # non-TOC text by pattern, which is stricter than a marker word — and the
    # bpa series renders its marker as a running header that the pages-parse
    # markdown drops, so the word is absent even on the right pages.
    if (strategy is None or strategy.extract is None) and find_marker(markdown) is None:
        return failed(
            f"pages {start}–{end} parsed, but no contents marker was in them; "
            "check the page range",
            start_page=start,
            end_page=end,
            parse_seconds=parse_seconds,
        )

    began = time.monotonic()
    try:
        if strategy is not None and strategy.extract is not None:
            extracted = strategy.extract(markdown)
        else:
            extracted = extract_headers(contents_section(markdown), llm=llm)
    except ExtractionError as exc:
        return failed(
            str(exc), start_page=start, end_page=end, parse_seconds=parse_seconds
        )
    llm_seconds = time.monotonic() - began

    ranges = (
        strategy.ranges(extracted, blocks)
        if strategy is not None and strategy.ranges is not None
        else None
    )
    saved = save_headers(db, document.id, extracted, blocks, ranges=ranges)

    return PagesResult(
        id=document.id,
        filename=document.filename,
        saved=saved,
        start_page=start,
        end_page=end,
        parse_seconds=parse_seconds,
        llm_seconds=llm_seconds,
    )


def run_group(db: Session, document: Document, llm: str | None = None) -> PagesResult:
    """The Group method: ``run_pages`` with the document's own group strategy.

    The page range — and any extract/ranges hook — comes from the strategy,
    never from the toolbar. ``llm`` is the toolbar's pick and stands only when
    the strategy does not name one; a strategy with its own ``extract`` makes
    no model call at all.
    """
    strategy = strategy_for(document.group_id)
    return run_pages(
        db,
        document,
        strategy.start_page,
        strategy.end_page,
        llm=strategy.llm or llm,
        strategy=strategy,
    )
