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
  back as headings — which is what the model call used to do. The summary is
  a real section the TOC never lists, so a 요약 entry is put in front.
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
    running header, page furniture) is ignored. A 요약 entry opens the list:
    the summary is a fixed section of the group that the TOC never lists, and
    ``page_index_ranges_oft`` knows which page it sits on.
    """
    text = markdown
    for char, plain in _ROMAN_CHARS.items():
        text = text.replace(char, plain)

    headers = [ExtractedHeader(title=SUMMARY_TITLE, level=1, start_page=None)]
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

    if len(headers) == 1:
        raise ExtractionError(
            "No roman-numbered entry (I. … 2) was found in the parsed page — "
            "the oft-1 TOC pattern did not match; check the page range."
        )
    return headers


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
    2. **Title matching** (``locate_starts``) for an entry the arithmetic
       cannot reach: no printed page, or a vote that never formed.
    3. **The summary page**: a 요약 entry still unplaced starts on the page
       after the TOC marker, where the group always prints its summary.

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

    if any(start is None for start in starts):
        located, _ = locate_starts(headers, blocks)
        for i, start in enumerate(located):
            if starts[i] is None:
                starts[i] = start

    marker = contents_page_index(blocks)
    if marker is not None:
        for i, entry in enumerate(headers):
            if starts[i] is None and _SQUEEZE.sub("", entry.title) == SUMMARY_TITLE:
                starts[i] = marker + 1

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
