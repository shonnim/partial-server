"""The Pages method: read the contents page out of the PDF, then extract.

The manual method needs someone to copy a contents page out of a PDF by hand.
This one does the copying: the content list a parse run left behind says which
page the contents are on, the parsing service is asked for just that page and
the few after it, and the markdown it returns goes through the same model call
the paste would have.

One document at a time, on purpose. A call to the parsing service is a VLM
pass over several pages and the model call after it is not quick either, so
the browser walks the queue and asks for one document per request. That is
what keeps a batch of fifty from being one fifty-minute HTTP request.

Nothing here guesses. If the content list has no marker, if the PDF cannot
be read, if the parsed pages turn out not to hold a contents page — the
document is reported as failed with the reason, and no headers are written.
"""

import re
import time
import uuid
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.doc_extract import DocExtractError, parse_pages
from app.models.document import Document, HasHeader
from app.services.header_extraction import (
    ExtractionError,
    extract_headers,
    save_headers,
)
from app.services.headers import contents_page_index, find_marker, load_content_list
from app.storage import ObjectNotFound, read_object

# How many pages after the contents page are sent along with it. The end page
# is ``start + pages`` (inclusive), which is what the request asks for.
DEFAULT_PAGES = 3
MIN_PAGES = 1
MAX_PAGES = 10

# The whole PDF is uploaded to the parsing service, whichever pages it is
# asked for, so a source file has to fit in memory here first.
MAX_PDF_BYTES = 200 * 1024 * 1024

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
    start_page: int | None = None
    end_page: int | None = None
    parse_seconds: float | None = None
    llm_seconds: float | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


def page_range(start: int, pages: int, total: int | None) -> tuple[int, int]:
    """The 0-based inclusive range to parse, clamped to the document's length.

    ``total`` is the page count a parse recorded, when one did. A range that
    runs past the end is trimmed rather than rejected: the contents page of a
    short document is still worth reading.
    """
    end = start + pages
    if total is not None and total > start:
        end = min(end, total - 1)
    return start, max(start, end)


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


def run_pages(db: Session, document: Document, pages: int) -> PagesResult:
    """Extract and store the headers of one document from its PDF pages."""

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

    if not MIN_PAGES <= pages <= MAX_PAGES:
        return failed(f"pages must be between {MIN_PAGES} and {MAX_PAGES}")

    if document.has_header is not HasHeader.YES:
        return failed("not marked as having a contents page — run Has Header first")

    blocks, error = load_content_list(db, document)
    if error is not None:
        return failed(error)

    start = contents_page_index(blocks)
    if start is None:
        return failed("no contents marker with a page index in the content list")

    start, end = page_range(start, pages, document.pages)

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
        markdown = parse_pages(pdf, document.filename, start_page=start, end_page=end)
    except DocExtractError as exc:
        return failed(str(exc), start_page=start, end_page=end)
    parse_seconds = time.monotonic() - began

    # The content list's page index is not guaranteed to be the PDF's: a run
    # over a two-up layout numbers spreads, not pages. Body text sent to the
    # model would come back as invented headings, so it is refused here.
    if find_marker(markdown) is None:
        return failed(
            f"pages {start}–{end} parsed, but no contents marker was in them; "
            "the content list's page_idx may not match the PDF's pages",
            start_page=start,
            end_page=end,
            parse_seconds=parse_seconds,
        )

    began = time.monotonic()
    try:
        extracted = extract_headers(contents_section(markdown))
    except ExtractionError as exc:
        return failed(
            str(exc), start_page=start, end_page=end, parse_seconds=parse_seconds
        )
    llm_seconds = time.monotonic() - began

    saved = save_headers(db, document.id, extracted)

    return PagesResult(
        id=document.id,
        filename=document.filename,
        saved=saved,
        start_page=start,
        end_page=end,
        parse_seconds=parse_seconds,
        llm_seconds=llm_seconds,
    )
