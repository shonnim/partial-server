"""The bpa-1 strategy: parsing a NABO 부처별 분석 TOC and placing its pages.

The parse is regex over the printed contents list — agencies in square
brackets, three roman sections per agency, numbered entries below — and the
page placement is arithmetic: printed page + front-matter offset, agency
book-face two pages before its Section I, contiguous sections.
"""

import pytest

from app.services.header_bpa import (
    TOC_FALLBACK_SPAN,
    locate_toc,
    page_index_ranges_bpa,
    parse_toc,
)
from app.services.header_extraction import ExtractedHeader, ExtractionError
from app.services.header_pages import strategy_for

# A cut-down 2016-report TOC, with the dressings a parse leaves behind:
# heading hashes, stray emphasis, the marker line, and page furniture.
TOC = """# 차 례

[ 농림축산식품부 ]

I. 예산안 개요 / 3
 1. 현황 ·········· 3
 2. 신규사업 및 증액사업 ······ 7
Ⅱ. 예산안 특징 및 분석방향 / 11
**III. 주요사업 분석 / 15**
 1. 농업정책자금 이차보전 사업의 집행관리 필요(농업정책관) ···· 15
 2. [분야별 분석] 수리시설 개보수 사업 절감 필요(농업기반과): 10억원 감액 가능 … 20

- 2 -

[ 해양수산부 ]
I. 예산안 개요 / 51
 1. 현황  51
 2. 신규사업 및 증액사업 / 55
II. 예산안 특징 및 분석방향 / 58
III. 주요사업 분석 / 60
 1. 항만 유지보수 사업 집행관리 필요(항만국) ····· 60
"""


def test_parse_toc_reads_the_three_levels() -> None:
    headers = parse_toc(TOC)

    assert [(h.level, h.title, h.start_page) for h in headers] == [
        (1, "농림축산식품부", None),
        (2, "I. 예산안 개요", 3),
        (3, "1. 현황", 3),
        (3, "2. 신규사업 및 증액사업", 7),
        (2, "II. 예산안 특징 및 분석방향", 11),
        (2, "III. 주요사업 분석", 15),
        (3, "1. 농업정책자금 이차보전 사업의 집행관리 필요(농업정책관)", 15),
        (
            3,
            "2. [분야별 분석] 수리시설 개보수 사업 절감 필요(농업기반과): 10억원 감액 가능",
            20,
        ),
        (1, "해양수산부", None),
        (2, "I. 예산안 개요", 51),
        (3, "1. 현황", 51),
        (3, "2. 신규사업 및 증액사업", 55),
        (2, "II. 예산안 특징 및 분석방향", 58),
        (2, "III. 주요사업 분석", 60),
        (3, "1. 항만 유지보수 사업 집행관리 필요(항만국)", 60),
    ]


def test_entries_before_the_first_agency_are_noise() -> None:
    headers = parse_toc("I. 예산안 개요 / 3\n[ 산림청 ]\nI. 예산안 개요 / 5\n 1. 현황 · 5")

    assert [h.title for h in headers] == ["산림청", "I. 예산안 개요", "1. 현황"]


def test_a_numbered_line_without_a_page_is_not_an_entry() -> None:
    # A pageless numbered line followed by a new entry is a fragment, not a
    # heading — it absorbs nothing and is dropped.
    headers = parse_toc("[ 산림청 ]\nI. 개요 / 3\n 1. 사방사업의 성과관리\n 2. 현황 · 5")

    assert [h.title for h in headers] == ["산림청", "I. 개요", "2. 현황"]


def test_a_digit_rendered_section_is_recognised_by_its_fixed_title() -> None:
    # The parse renders the roman Ⅰ as the digit 1; the fixed title says what
    # the line is, and the entries beneath it are kept with it.
    headers = parse_toc(
        "[해양수산부]\n"
        "1. 예산안 개요 / 51\n"
        "1. 현 황 51\n"
        "2. 신규사업 및 증액사업 55\n"
        "II.예산안 특징 및 분석방향/58"
    )

    assert [(h.level, h.title, h.start_page) for h in headers] == [
        (1, "해양수산부", None),
        (2, "I. 예산안 개요", 51),
        (3, "1. 현 황", 51),
        (3, "2. 신규사업 및 증액사업", 55),
        (2, "II. 예산안 특징 및 분석방향", 58),
    ]


def test_a_pageless_section_line_still_counts() -> None:
    headers = parse_toc("[농촌진흥청]\n1. 예산안 개요\n1. 현황 97")

    assert [(h.level, h.title, h.start_page) for h in headers] == [
        (1, "농촌진흥청", None),
        (2, "I. 예산안 개요", None),
        (3, "1. 현황", 97),
    ]


def test_a_page_glued_to_the_bracket_still_counts() -> None:
    headers = parse_toc(
        "[산림청]\n"
        "III. 주요사업 분석 / 123\n"
        "4.국산 목재펠릿 보급 활성화를 위한 산림탄소상쇄제도와의 연계 필요(산림자원국)128"
    )

    assert headers[-1].title.endswith("연계 필요(산림자원국)")
    assert headers[-1].start_page == 128


def test_a_wrapped_title_is_rejoined_with_its_page() -> None:
    headers = parse_toc(
        "[농림축산식품부]\n"
        "III. 주요사업 분석 / 13\n"
        "11. 「할랄인증 활성화 지원」사업의 집행 가능성 개선을 위한 방안 마련 필요\n"
        "(식품산업정책실 식품산업정책관) …… 34"
    )

    assert headers[-1].level == 3
    assert headers[-1].start_page == 34
    assert headers[-1].title.endswith("(식품산업정책실 식품산업정책관)")


def test_no_agency_blocks_is_an_error() -> None:
    with pytest.raises(ExtractionError, match=r"No \[agency\] block"):
        parse_toc("본문 첫 쪽\n예산 개요에 관한 문단.")


def test_agencies_without_sections_is_an_error() -> None:
    with pytest.raises(ExtractionError, match="no roman sections"):
        parse_toc("[ 산림청 ]\n[ 농촌진흥청 ]")


# ---- page placement -----------------------------------------------------------


def _page_number(printed: int, idx: int) -> dict:
    return {"type": "page_number", "page_idx": idx, "text": f"- {printed} -"}


def _blocks(pairs: dict[int, int], last: int) -> list[dict]:
    blocks = [_page_number(printed, idx) for printed, idx in pairs.items()]
    blocks.append({"type": "text", "page_idx": last, "text": "뒤표지"})
    return blocks


# The worked example's shape: offset c = 12, two agencies, back cover at 150.
HEADERS = [
    ExtractedHeader("농림축산식품부", 1, None),
    ExtractedHeader("I. 예산안 개요", 2, 3),
    ExtractedHeader("1. 현황", 3, 3),
    ExtractedHeader("2. 신규사업 및 증액사업", 3, 7),
    ExtractedHeader("II. 예산안 특징 및 분석방향", 2, 11),
    ExtractedHeader("III. 주요사업 분석", 2, 15),
    ExtractedHeader("1. 집행관리 필요(농업정책관)", 3, 15),
    ExtractedHeader("2. 절감 필요(농업기반과)", 3, 20),
    ExtractedHeader("해양수산부", 1, None),
    ExtractedHeader("I. 예산안 개요", 2, 51),
    ExtractedHeader("1. 현황", 3, 51),
    ExtractedHeader("2. 신규사업 및 증액사업", 3, 55),
    ExtractedHeader("II. 예산안 특징 및 분석방향", 2, 58),
    ExtractedHeader("III. 주요사업 분석", 2, 60),
    ExtractedHeader("1. 집행관리 필요(항만국)", 3, 60),
]

PRINTED = {3: 15, 7: 19, 11: 23, 15: 27, 20: 32, 51: 63, 55: 67, 58: 70, 60: 72}


def test_the_worked_example_places_every_entry() -> None:
    ranges = page_index_ranges_bpa(HEADERS, _blocks(PRINTED, last=150))

    assert ranges == [
        (13, 60),  # agency: Section I start − 2, up to the next agency
        (15, 22),  # I = 1.현황 start … 2.신규사업 end
        (15, 18),
        (19, 22),
        (23, 26),  # II, no subsections
        (27, 60),  # III runs to the next agency's book-face page
        (27, 31),
        (32, 60),
        (61, 150),  # last agency runs to the end of the document
        (63, 69),
        (63, 66),
        (67, 69),
        (70, 71),
        (72, 150),  # last section: to the end, back cover included
        (72, 150),
    ]


def test_a_front_matter_restart_is_outvoted() -> None:
    # The front matter also printed a "3", earlier in the list, so the index
    # maps 3 to the wrong block; the body's consistent offset must win and
    # place the p.3 entries by the majority's arithmetic.
    poisoned = {3: 2, **{k: v for k, v in PRINTED.items() if k != 3}}
    ranges = page_index_ranges_bpa(HEADERS, _blocks(poisoned, last=150))

    assert ranges[1] == (15, 22)
    assert ranges[2] == (15, 18)


def test_no_content_list_means_no_indexes() -> None:
    assert page_index_ranges_bpa(HEADERS, None) == [(None, None)] * len(HEADERS)


# ---- locating the TOC pages ----------------------------------------------------


def test_locate_toc_reads_marker_and_body_start() -> None:
    # The series marks its TOC with a CONTENTS running header; the body opens
    # on printed page 3. The 2016 report: marker at idx 7, p.3 at idx 15, so
    # the TOC is viewer pages 8–15 (the last TOC page is idx 14, viewer 15).
    blocks = [
        {"type": "header", "page_idx": 7, "text": "CONTENTS"},
        _page_number(3, 15),
        {"type": "text", "page_idx": 150, "text": "뒤표지"},
    ]

    assert locate_toc(blocks) == (8, 15)


def test_locate_toc_without_a_body_page_uses_the_fallback_span() -> None:
    blocks = [{"type": "header", "page_idx": 7, "text": "CONTENTS"}]

    assert locate_toc(blocks) == (8, 8 + TOC_FALLBACK_SPAN)


def test_locate_toc_ignores_a_body_page_before_the_marker() -> None:
    # A front-matter page that also printed a "3" sits before the marker and
    # cannot be where the body starts.
    blocks = [
        _page_number(3, 2),
        {"type": "header", "page_idx": 7, "text": "CONTENTS"},
    ]

    assert locate_toc(blocks) == (8, 8 + TOC_FALLBACK_SPAN)


def test_locate_toc_without_a_marker_defers_to_the_static_range() -> None:
    assert locate_toc([{"type": "text", "page_idx": 0, "text": "본문"}]) is None
    assert locate_toc(None) is None


def test_bpa_1_is_registered_with_its_hooks() -> None:
    strategy = strategy_for("bpa-1")

    assert strategy.locate is locate_toc
    assert strategy.extract is parse_toc
    assert strategy.ranges is page_index_ranges_bpa
    assert strategy.llm is None  # no model call either way
