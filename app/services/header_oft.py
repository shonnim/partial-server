"""The oft-1 group strategy: TOC location, a regex TOC parser, page arithmetic.

The group's reports share one front matter: cover, credits, the table of
contents on viewer page 3, the summary page right after it, then the body.
The layout facts this module encodes, tuned on the group's documents:

* **The TOC is one page inside the first four viewer pages** — usually the
  third, whose large letter-spaced "목 차" title the parse splits into one
  block per syllable, with the running header interleaved between them. The
  page right after it is the summary page, titled "요 약" the same way, so
  the TOC never spills onto it. ``locate_toc_oft`` reads the marker's page
  off the content list (the split-marker search catches the split title) and
  returns that page alone.
* **The TOC is one flat roman list**, regular enough to parse without a
  model::

      I. 국제기구 2
      Ⅱ. 호주....11
      Ⅲ. 캐 나 다....12
      VI.일본 19

  ``parse_toc_oft`` takes those lines and nothing else, so the split 목/차
  syllables and the running header on the same parsed page can never come
  back as headings — which is what the model call used to do. Most reports
  open with a summary section the TOC never lists; ``amend_toc_oft`` puts a
  요약 entry in front, but only after the content list confirms the page
  after the TOC actually prints the 요 약 title — some volumes have no
  summary, and those must not grow a phantom section.
* **Page indexes follow arithmetic, not text matching.** Body headings parse
  split too ("IV" and "프랑스" as two blocks), so locating them by title
  fails more often than not — but every printed page sits at ``idx =
  printed + c``, with ``c`` the front-matter length taken by majority vote
  (the same arithmetic as the bpa series). Title matching is only the
  fallback for an entry the vote cannot reach.
* **Every header opens at the top of a page.** No page holds the end of one
  section and the beginning of the next, so an entry always ends on the page
  before the next entry at its level or shallower, and the last runs to the
  document's end.

The hooks plug into the Group method as the ``locate``, ``extract`` and
``ranges`` of the group's ``GroupStrategy`` in ``header_pages.GROUP_STRATEGIES``.
"""

import re
from collections.abc import Sequence
from typing import Any

from app.services.header_bpa import _front_matter_offset, _lines
from app.services.header_extraction import (
    ExtractedHeader,
    ExtractionError,
    last_page_index,
    locate_starts,
    normalize_title,
)
from app.services.headers import contents_page_index, page_number_index

# The static fallback window, and the viewer page past which the group's TOC
# never sits. A marker found beyond it is not this group's contents page.
TOC_FIRST_PAGE = 1
TOC_LAST_PAGE = 4

# The group's summary section: a fixed page right after the TOC, which the
# TOC itself never lists. Its "요 약" heading is letter-spaced like the TOC
# title, so it often parses split and cannot be relied on for locating.
SUMMARY_TITLE = "요약"
_SQUEEZE = re.compile(r"\s+")
_SUMMARY_CHARS = frozenset(SUMMARY_TITLE)

# The single-character roman numerals the TOC prints alongside the ASCII
# ones; they must become letters before the entry pattern can see them.
_ROMAN_CHARS = {
    "Ⅰ": "I", "Ⅱ": "II", "Ⅲ": "III", "Ⅳ": "IV", "Ⅴ": "V",
    "Ⅵ": "VI", "Ⅶ": "VII", "Ⅷ": "VIII", "Ⅸ": "IX", "Ⅹ": "X",
}  # fmt: skip

# One TOC entry: a roman numeral with its dot, the title, and the required
# page — behind dot leaders, a slash, plain spacing, or glued straight onto
# the title ("일본19"). Requiring the page is what keeps the parsed page's
# other lines (the split 목/차 title, the running header) out.
_ENTRY = re.compile(
    r"^(?P<num>[IVX]{1,5})\s*[.．]\s*(?P<title>.+?)"
    r"(?:\s*(?:/|[.·․‥…]+)\s*|\s+|(?<=[^\d\s]))(?P<page>\d{1,4})\s*$"
)


def locate_toc_oft(blocks: Any) -> tuple[int, int] | None:
    """The 1-based viewer page holding the TOC, read off the content list.

    The marker's page is the whole range — ``contents_page_index`` finds the
    split "목 차" title the same way the has_header check does, and the page
    after the TOC is the summary page, so the TOC never runs on. None when
    the content list has no marker inside the window, which sends the caller
    back to the strategy's static 1..4.
    """
    if not isinstance(blocks, list):
        return None
    marker = contents_page_index(blocks)
    if marker is None:
        return None
    start = marker + 1  # a viewer counts from 1, page_idx from 0
    if start > TOC_LAST_PAGE:
        return None
    return start, start


def parse_toc_oft(markdown: str) -> list[ExtractedHeader]:
    """The parsed TOC page of an oft-1 report, as ordered headings.

    Deterministic — no model call. Every roman-numbered line carrying a page
    is an entry, all at level 1; every other line (the split 목/차 title, the
    running header, page furniture) is ignored. Only what the TOC prints
    comes back: the group's unlisted summary section is ``amend_toc_oft``'s
    to add, once the content list has confirmed the document has one.
    """
    text = markdown
    for char, plain in _ROMAN_CHARS.items():
        text = text.replace(char, plain)

    headers: list[ExtractedHeader] = []
    for line in _lines(text):
        match = _ENTRY.match(line)
        if match is None:
            continue
        title = normalize_title(f"{match.group('num')}. {match.group('title')}")
        if not title:
            continue
        headers.append(
            ExtractedHeader(title=title, level=1, start_page=int(match.group("page")))
        )

    if not headers:
        raise ExtractionError(
            "No roman-numbered entry (I. … 2) was found in the parsed page — "
            "the oft-1 TOC pattern did not match; check the page range."
        )
    return headers


def _has_summary_page(blocks: list[Any], page: int) -> bool:
    """Whether content-list page ``page`` prints the group's 요 약 title.

    The title is letter-spaced like the TOC's, so it arrives either whole
    ("요 약") or as one single-character block per syllable with the running
    header interleaved — the same shapes the contents marker takes. An
    incidental 요약 inside a longer sentence does not count; only a block
    that is the title, or syllables that join into it.
    """
    chars: list[str] = []
    for block in blocks:
        if not isinstance(block, dict) or block.get("page_idx") != page:
            continue
        text = block.get("text")
        if not isinstance(text, str):
            continue
        squeezed = _SQUEEZE.sub("", text)
        if squeezed == SUMMARY_TITLE:
            return True
        if len(squeezed) == 1 and squeezed in _SUMMARY_CHARS:
            chars.append(squeezed)
    return SUMMARY_TITLE in "".join(chars)


def amend_toc_oft(
    headers: Sequence[ExtractedHeader], blocks: Any
) -> list[ExtractedHeader]:
    """Insert the 요약 entry in front — for a document that prints a summary.

    Most of the group's reports open with a 요 약 section on the page right
    after the TOC, which the TOC itself never lists. Whether *this* document
    is one of them is read off the content list, not assumed: without the
    title on that page the headers stand as parsed, so a volume with no
    summary does not grow a phantom section (whose range used to be whatever
    body page the word 요약 happened to appear on).
    """
    if not isinstance(blocks, list):
        return list(headers)
    marker = contents_page_index(blocks)
    if marker is None or not _has_summary_page(blocks, marker + 1):
        return list(headers)
    return [ExtractedHeader(title=SUMMARY_TITLE, level=1, start_page=None), *headers]


def page_index_ranges_oft(
    headers: Sequence[ExtractedHeader], blocks: Any
) -> list[tuple[int | None, int | None]]:
    """``(start_page_idx, end_page_idx)`` per entry, by the group's page layout.

    Starts, in order of trust:

    1. **Arithmetic**: ``idx = printed + c``, with ``c`` the front-matter
       offset taken by majority vote over the entries whose printed number
       the content list captured. The group's body headings parse split
       ("IV" and "프랑스" as two blocks), so this — not title matching — is
       the primary source.
    2. **The summary page**: a 요약 entry starts on the page after the TOC
       marker, where the group prints its summary. Never by title matching —
       its letter-spaced heading parses split, so a generic match lands on
       whatever body page happens to carry the word 요약.
    3. **Title matching** (``locate_starts``) for any other entry the
       arithmetic cannot reach: no printed page, or a vote that never formed.

    Ends need no opens-its-page check — every header starts at the top of a
    page, so no page is shared: an entry ends on the page before the next
    entry at its level or shallower, and the last entry at its level runs to
    the document's end.
    """
    if not isinstance(blocks, list) or not headers:
        return [(None, None)] * len(headers)

    printed = page_number_index(blocks)
    last = last_page_index(blocks)
    c = _front_matter_offset(headers, printed)

    starts: list[int | None] = [None] * len(headers)
    if c is not None:
        for i, entry in enumerate(headers):
            if entry.start_page is None:
                continue
            idx = entry.start_page + c
            if idx >= 0 and (last is None or idx <= last):
                starts[i] = idx

    summary = [_SQUEEZE.sub("", entry.title) == SUMMARY_TITLE for entry in headers]
    marker = contents_page_index(blocks)
    if marker is not None:
        for i in range(len(headers)):
            if starts[i] is None and summary[i]:
                starts[i] = marker + 1

    if any(
        start is None for start, is_summary in zip(starts, summary) if not is_summary
    ):
        located, _ = locate_starts(headers, blocks)
        for i, start in enumerate(located):
            if starts[i] is None and not summary[i]:
                starts[i] = start

    ranges: list[tuple[int | None, int | None]] = []
    for i, entry in enumerate(headers):
        following = next(
            (j for j in range(i + 1, len(headers)) if headers[j].level <= entry.level),
            None,
        )
        if following is None:
            end = last
        elif starts[following] is None:
            end = None
        else:
            end = starts[following] - 1
            if starts[i] is not None and end < starts[i]:
                # Two starts on one page contradict the group's layout;
                # never let an entry end before it begins.
                end = starts[i]
        ranges.append((starts[i], end))
    return ranges
