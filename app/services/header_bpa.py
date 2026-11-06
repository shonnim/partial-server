"""The bpa-1 group strategy: NABO "예산안 부처별 분석" contents pages.

The 국회예산정책처 *Budget Bill Analysis by Ministry* series has a contents
page regular enough to parse without a model. It runs several pages and is
divided into one block per agency under the committee's jurisdiction:

    [ 농림축산식품부 ]            agency, on a book-face page of its own
      I.  예산안 개요 / 3         three fixed roman sections per agency
          1. 현황 ·········· 3    fixed pair under I
          2. 신규사업 및 증액사업
      II. 예산안 특징 및 분석방향  no subsections
      III. 주요사업 분석
          1. … n.                 variable program analyses, titles differ

The page indexes follow arithmetic rather than text matching: every printed
page sits at ``idx = printed + c`` where ``c`` is the front-matter length,
derived from the content list's page_number blocks; an agency's book-face
page sits two pages before its Section I; and sections are contiguous, so
``end = next start − 1`` with the last entry running to the document's end.

``parse_toc`` and ``page_index_ranges_bpa`` plug into the Group method as the
``extract`` and ``ranges`` hooks of the group's ``GroupStrategy``.
"""

import re
from collections import Counter
from collections.abc import Sequence
from typing import Any

from app.services.header_extraction import (
    ExtractedHeader,
    ExtractionError,
    last_page_index,
    normalize_title,
)
from app.services.headers import contents_page_index, page_number_index

# How far below its first child an agency's book-face page sits: the agency
# name page plus the blank page behind it.
AGENCY_LEAD_PAGES = 2

# The printed number of the body's first page: every report in the series
# opens its first agency's Section I on printed page 3.
FIRST_BODY_PRINTED = 3

# Pages to read past the TOC's first page when the content list cannot say
# where the body starts; the widest TOC seen so far runs nine pages.
TOC_FALLBACK_SPAN = 9


def locate_toc(blocks: Any) -> tuple[int, int] | None:
    """The 1-based viewer page range holding the TOC, read off the content list.

    The TOC's first page carries the series' contents marker ("CONTENTS", a
    running header), and the body opens on printed page 3, whose block the
    page-number index knows — the TOC is everything in between, inclusive of
    the page before the body. None when the content list has no marker, which
    sends the caller back to the strategy's static range.
    """
    if not isinstance(blocks, list):
        return None
    marker = contents_page_index(blocks)
    if marker is None:
        return None

    start = marker + 1  # a viewer counts from 1, page_idx from 0
    body = page_number_index(blocks).get(FIRST_BODY_PRINTED)
    if body is not None and body > marker:
        return start, body  # viewer page of idx (body − 1): the TOC's last
    return start, start + TOC_FALLBACK_SPAN

# One line of the printed TOC, after markdown dressing is stripped.
#
# agency:  the name in square brackets, alone on its line.
# roman:   the three fixed sections; the page arrives as "/ 3", as dot
#          leaders, or not at all (the title alone fills the line).
# arabic:  a numbered entry; the page is required — dot leaders, a slash,
#          plain spacing, or glued straight onto the closing bracket
#          ("…필요(산림자원국)128") — which is what keeps a body line that
#          happens to start "1." from counting as an entry.
_AGENCY = re.compile(r"^\[\s*(?P<title>[^\[\]]+?)\s*\]$")
_ROMAN = re.compile(
    r"^(?P<num>I{1,3})\s*[.．]\s*(?P<title>.+?)"
    r"(?:\s*(?:/|[.·․‥…]+)\s*(?P<page>\d{1,4}))?\s*$"
)
_ARABIC = re.compile(
    r"^(?P<num>\d{1,2})\s*[.．]\s*(?P<title>.+?)"
    r"(?:\s*(?:/|[.·․‥…]+)\s*|\s+|(?<=[^\d\s]))(?P<page>\d{1,4})\s*$"
)

# What the parser wraps a TOC line in: heading hashes, emphasis, list bullets.
_LINE_DRESSING_LEAD = re.compile(r"^[\s#>*`\-–—]+")
_LINE_DRESSING_TAIL = re.compile(r"[\s*`]+$")

# The three fixed sections of every agency, keyed with whitespace squeezed
# out. The parse of a contents page sometimes renders the roman Ⅰ as the
# digit 1, so the fixed title — not the numeral — is what identifies a
# section line; the numeral is then rewritten to the canonical roman.
FIXED_SECTIONS = {
    "예산안개요": "I",
    "예산안특징및분석방향": "II",
    "주요사업분석": "III",
}

_SQUEEZE = re.compile(r"\s+")

# A line that *opens* an entry, matched or not: an agency bracket, a roman
# numeral, or a number with its dot. What does not open one is a wrapped
# title's continuation.
_ENTRY_START = re.compile(r"^(?:\[|(?:I{1,3}|\d{1,2})\s*[.．])")
_ARABIC_START = re.compile(r"^\d{1,2}\s*[.．]")

# A numbered line without the required page — a section line whose page the
# parse dropped, or the first half of a wrapped title.
_BARE_NUMBERED = re.compile(r"^(?:I{1,3}|\d{1,2})\s*[.．]\s*(?P<title>\S.*?)$")


def _lines(markdown: str) -> list[str]:
    """The TOC text as bare lines, markdown dressing stripped."""
    lines = []
    for raw in markdown.splitlines():
        line = _LINE_DRESSING_TAIL.sub("", _LINE_DRESSING_LEAD.sub("", raw))
        if line:
            lines.append(line)
    return lines


def _dewrap(lines: list[str]) -> list[str]:
    """Rejoin numbered entries whose titles wrapped onto the next line.

    A long title carries its dot leaders and page on the continuation line,
    so the numbered line above it fails the page-required rule. A numbered
    line still missing its page absorbs following lines that do not open an
    entry of their own — at most two, so a stray pageless number cannot
    swallow half a page of text.
    """
    merged: list[str] = []
    absorbed = 0
    for line in lines:
        wraps = (
            bool(merged)
            and absorbed < 2
            and _ARABIC_START.match(merged[-1]) is not None
            and _ARABIC.match(merged[-1]) is None
            and _ENTRY_START.match(line) is None
        )
        if wraps:
            merged[-1] = f"{merged[-1]} {line}"
            absorbed += 1
        else:
            merged.append(line)
            absorbed = 0
    return merged


def _fixed_section(title: str) -> str | None:
    """The canonical roman numeral when ``title`` is a fixed section's."""
    return FIXED_SECTIONS.get(_SQUEEZE.sub("", title))


def parse_toc(markdown: str) -> list[ExtractedHeader]:
    """The parsed pages of a bpa-1 contents list, as ordered headings.

    Deterministic — no model call. Agencies are level 1, roman sections level
    2, numbered entries level 3. A numbered line carrying one of the three
    fixed section titles is a section whatever its numeral — the parse
    renders the roman Ⅰ as the digit 1 — and a title that wrapped onto the
    next line is rejoined first. Lines matching nothing (page furniture, the
    차례 marker) are ignored, as is anything before the first agency block
    and a numbered entry arriving before its section.
    """
    # The single-character numerals must become letters before the roman
    # pattern can see them; normalize_title repeats this per title for the
    # occurrences mid-title.
    text = markdown.replace("Ⅰ", "I").replace("Ⅱ", "II").replace("Ⅲ", "III")

    headers: list[ExtractedHeader] = []
    previous = 0

    for line in _dewrap(_lines(text)):
        if match := _AGENCY.match(line):
            title, level, page = match.group("title"), 1, None
        elif match := (
            _ROMAN.match(line) or _ARABIC.match(line) or _BARE_NUMBERED.match(line)
        ):
            groups = match.groupdict()
            body, page = groups["title"], groups.get("page")
            fixed = _fixed_section(body)
            if fixed is not None:
                if previous == 0:
                    continue  # a section before any agency block
                title, level = f"{fixed}. {body}", 2
            elif (num := groups.get("num")) and not num.isdigit():
                if previous == 0:
                    continue
                title, level = f"{num}. {body}", 2
            elif num is not None and page is not None:
                if previous < 2:
                    continue  # a numbered entry before any roman section
                title, level = f"{num}. {body}", 3
            else:
                continue  # a pageless numbered line that is no fixed section
        else:
            continue

        title = normalize_title(title)
        if not title:
            continue
        headers.append(
            ExtractedHeader(
                title=title,
                level=level,
                start_page=int(page) if page is not None else None,
            )
        )
        previous = level

    if not headers:
        raise ExtractionError(
            "No [agency] block was found in the parsed pages — "
            "the bpa-1 TOC pattern did not match; check the page range."
        )
    if all(h.level == 1 for h in headers):
        raise ExtractionError(
            "Agency blocks were found but no roman sections under them — "
            "the bpa-1 TOC pattern did not match the entry lines."
        )
    return headers


def _front_matter_offset(
    headers: Sequence[ExtractedHeader], printed: dict[int, int]
) -> int | None:
    """``c`` in ``idx = printed + c``: the front-matter length of the report.

    Derived per entry whose printed number the content list captured, and
    taken by majority: a duplicated printed number — front matter restarting
    at 1 — maps to the wrong block and casts a stray vote, but the body's
    consistent offset outvotes it.
    """
    votes = Counter(
        printed[h.start_page] - h.start_page
        for h in headers
        if h.start_page is not None and h.start_page in printed
    )
    if not votes:
        return None
    return votes.most_common(1)[0][0]


def page_index_ranges_bpa(
    headers: Sequence[ExtractedHeader], blocks: Any
) -> list[tuple[int | None, int | None]]:
    """``(start_page_idx, end_page_idx)`` per entry, by the series' arithmetic.

    Starts: ``printed + c`` for every entry with a printed page; an entry
    without one takes its first child's start — except an agency, whose
    book-face page sits ``AGENCY_LEAD_PAGES`` before its Section I.

    Ends: sections are contiguous, so an entry ends one page before the next
    entry at its level or shallower starts; the entry before an agency ends
    one page before the agency's book-face page, and the last entry at its
    level runs to the document's last page.
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

    # Backwards, so a roman section resolves from its entries before the
    # agency above it asks for the roman section's start.
    for i in range(len(headers) - 1, -1, -1):
        if starts[i] is not None:
            continue
        child = i + 1
        if child == len(headers) or headers[child].level != headers[i].level + 1:
            continue
        if starts[child] is None:
            continue
        lead = AGENCY_LEAD_PAGES if headers[i].level == 1 else 0
        starts[i] = max(0, starts[child] - lead)

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
        ranges.append((starts[i], end))
    return ranges
