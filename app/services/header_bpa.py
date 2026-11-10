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

The bpa-2 strategy covers the other volumes of the same 예산안분석 series —
거시·총량 분석, 성인지 예산서 분석, 분석종합 — whose contents lists have no
agency brackets. Their lines stack differently per volume:

    제1부 거시·총량 분석 등          part        (분석종합 only)
      제1장|국회운영위원회           chapter     (분석종합 only)
        I. 국회 / 17                 roman section
          1. <item> 17               numbered entry
            가. 현황 19              hangul entry (성인지 only)

``parse_toc_bpa2`` assigns levels by rank — part, chapter, roman, arabic,
hangul — compressed so the shallowest kind a volume uses is level 1; the
page arithmetic is the same as bpa-1's, minus the agency book-face offset.
The 분석종합 TOC alone runs ~48 pages, which is why a located range is
allowed past the toolbar's span cap.
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

# ---- the bpa-2 line kinds ------------------------------------------------------
#
# Parts and chapters appear only in the 분석종합 volume, the hangul ordinals
# only in the 성인지 one. A part or chapter keeps its whole line as the
# title. The spacing varies per parse ("제1장|국회…", "제 2 부 부처별…"),
# and a body sentence running straight on ("제2부에서는…") has no boundary
# after 부/장, so one is required.
_PART = re.compile(r"^(?P<title>제\s*\d+\s*부(?:\s+\S.*)?)$")
_CHAPTER = re.compile(r"^(?P<title>제\s*\d+\s*장\s*(?:[|｜]\s*\S.*|\s+\S.*)?)$")

# The hangul ordinal sequence the series numbers sub-items with.
_HANGUL_ORDINALS = "가나다라마바사아자차카타파하"

# bpa-2's roman sections are agencies and run past III (IV, V, …), and their
# titles vary, so no fixed-title rescue applies.
_ROMAN2 = re.compile(
    r"^(?P<num>[IVX]{1,4})\s*[.．]\s*(?P<title>.+?)"
    r"(?:\s*(?:/|[.·․‥…]+)\s*(?P<page>\d{1,4}))?\s*$"
)
_HANGUL = re.compile(
    rf"^(?P<num>[{_HANGUL_ORDINALS}])\s*[.．]\s*(?P<title>.+?)"
    r"(?:\s*(?:/|[.·․‥…]+)\s*|\s+|(?<=[^\d\s]))(?P<page>\d{1,4})\s*$"
)

_ENTRY_START2 = re.compile(
    rf"^(?:\[|제\s*\d+\s*[부장]|(?:[IVX]{{1,4}}|\d{{1,2}}|[{_HANGUL_ORDINALS}])\s*[.．])"
)
_NUMBERED_START2 = re.compile(rf"^(?:\d{{1,2}}|[{_HANGUL_ORDINALS}])\s*[.．]")

# How the kinds nest. Levels are not fixed per kind: the shallowest kind a
# volume actually uses becomes level 1 (거시·총량 opens at the roman rank,
# 분석종합 at the part rank), which the rank stack below works out.
_RANK_PART, _RANK_CHAPTER, _RANK_ROMAN, _RANK_ARABIC, _RANK_HANGUL = range(1, 6)


def _lines(markdown: str) -> list[str]:
    """The TOC text as bare lines, markdown dressing stripped."""
    lines = []
    for raw in markdown.splitlines():
        line = _LINE_DRESSING_TAIL.sub("", _LINE_DRESSING_LEAD.sub("", raw))
        if line:
            lines.append(line)
    return lines


def _incomplete_bpa1(line: str) -> bool:
    """A bpa-1 numbered line that opened an entry but lacks its page."""
    return _ARABIC_START.match(line) is not None and _ARABIC.match(line) is None


def _incomplete_bpa2(line: str) -> bool:
    """The bpa-2 twin: arabic or hangul ordinal, page still missing."""
    return (
        _NUMBERED_START2.match(line) is not None
        and _ARABIC.match(line) is None
        and _HANGUL.match(line) is None
    )


def _dewrap(
    lines: list[str],
    *,
    incomplete=_incomplete_bpa1,
    entry_start: re.Pattern[str] = _ENTRY_START,
) -> list[str]:
    """Rejoin numbered entries whose titles wrapped onto the next line.

    A long title carries its dot leaders and page on the continuation line,
    so the numbered line above it fails the page-required rule. A line
    ``incomplete`` says is still missing its page absorbs following lines
    that do not open an entry of their own (``entry_start``) — at most two,
    so a stray pageless number cannot swallow half a page of text.
    """
    merged: list[str] = []
    absorbed = 0
    for line in lines:
        wraps = (
            bool(merged)
            and absorbed < 2
            and incomplete(merged[-1])
            and entry_start.match(line) is None
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


def parse_toc_bpa2(markdown: str) -> list[ExtractedHeader]:
    """The parsed pages of a bpa-2 contents list, as ordered headings.

    Deterministic — no model call. Lines are one of five kinds — part,
    chapter, roman section, numbered entry, hangul sub-entry — and the level
    of each heading is its depth among the kinds the volume actually uses:
    a rank stack pops everything at or below the arriving kind, so 거시·총량
    opens at level 1 with its roman sections while 분석종합 opens with its
    parts. An entry arriving before any section, and any line matching
    nothing, is ignored.
    """
    text = (
        markdown.replace("Ⅰ", "I")
        .replace("Ⅱ", "II")
        .replace("Ⅲ", "III")
        .replace("Ⅳ", "IV")
        .replace("Ⅴ", "V")
    )

    headers: list[ExtractedHeader] = []
    ranks: list[int] = []
    stack: list[int] = []
    seen: set[int] = set()

    lines = _dewrap(
        _lines(text), incomplete=_incomplete_bpa2, entry_start=_ENTRY_START2
    )
    for line in lines:
        page: str | None = None
        if match := _PART.match(line):
            rank, title = _RANK_PART, match.group("title")
        elif match := _CHAPTER.match(line):
            rank, title = _RANK_CHAPTER, match.group("title")
        elif match := _ROMAN2.match(line):
            rank = _RANK_ROMAN
            title = f"{match.group('num')}. {match.group('title')}"
            page = match.group("page")
        elif match := _ARABIC.match(line):
            rank = _RANK_ARABIC
            title = f"{match.group('num')}. {match.group('title')}"
            page = match.group("page")
        elif match := _HANGUL.match(line):
            rank = _RANK_HANGUL
            title = f"{match.group('num')}. {match.group('title')}"
            page = match.group("page")
        else:
            continue

        if rank >= _RANK_ARABIC and not stack:
            continue  # an entry before any part, chapter or section

        title = normalize_title(title)
        if not title:
            continue

        while stack and stack[-1] >= rank:
            stack.pop()
        headers.append(
            ExtractedHeader(
                title=title,
                level=len(stack) + 1,
                start_page=int(page) if page is not None else None,
            )
        )
        ranks.append(rank)
        stack.append(rank)
        seen.add(rank)

    # The located range runs up to the page before printed p.3, which in the
    # 분석종합 volume is the first part's own divider page — its "제 1 부"
    # re-matches as a part. A pageless part, chapter or section with nothing
    # beneath it at the very end is that divider, not an entry, and left in
    # place it would hold the real last entries' ends open.
    while (
        headers
        and headers[-1].start_page is None
        and ranks[-1] <= _RANK_ROMAN
    ):
        headers.pop()
        ranks.pop()

    if _RANK_ROMAN not in seen:
        raise ExtractionError(
            "No roman section (I., II., …) was found in the parsed pages — "
            "the bpa-2 TOC pattern did not match; check the page range."
        )
    if _RANK_ARABIC not in seen:
        raise ExtractionError(
            "Roman sections were found but no numbered entries under them — "
            "the bpa-2 TOC pattern did not match the entry lines."
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


def _arithmetic_ranges(
    headers: Sequence[ExtractedHeader],
    blocks: Any,
    *,
    level_leads: dict[int, int],
) -> list[tuple[int | None, int | None]]:
    """``(start_page_idx, end_page_idx)`` per entry, by the series' arithmetic.

    Starts: ``printed + c`` for every entry with a printed page; an entry
    without one takes its first child's start, minus its level's entry in
    ``level_leads`` — the pages a divider sits on before the content begins.

    Ends: sections are contiguous, so an entry ends one page before the next
    entry at its level or shallower starts, and the last entry at its level
    runs to the document's last page.
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

    # Backwards, so a section resolves from its entries before the level
    # above it asks for the section's start.
    for i in range(len(headers) - 1, -1, -1):
        if starts[i] is not None:
            continue
        child = i + 1
        if child == len(headers) or headers[child].level != headers[i].level + 1:
            continue
        if starts[child] is None:
            continue
        starts[i] = max(0, starts[child] - level_leads.get(headers[i].level, 0))

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
                # Two entries opening on the same page: the earlier one ends
                # on the page it starts on, not the page before it.
                end = starts[i]
        ranges.append((starts[i], end))
    return ranges


def page_index_ranges_bpa(
    headers: Sequence[ExtractedHeader], blocks: Any
) -> list[tuple[int | None, int | None]]:
    """bpa-1's arithmetic: an agency's book-face page sits two before its I."""
    return _arithmetic_ranges(headers, blocks, level_leads={1: AGENCY_LEAD_PAGES})


def page_index_ranges_bpa2(
    headers: Sequence[ExtractedHeader], blocks: Any
) -> list[tuple[int | None, int | None]]:
    """bpa-2's arithmetic: the same, with no divider lead at any level."""
    return _arithmetic_ranges(headers, blocks, level_leads={})
