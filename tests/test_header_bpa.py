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
    page_index_ranges_bpa2,
    parse_toc,
    parse_toc_bpa2,
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


# ---- bpa-2: the series' agency-less volumes -------------------------------------


def test_bpa2_two_level_volume_opens_at_the_roman_rank() -> None:
    # The 거시·총량 volume: roman sections and numbered entries, nothing else.
    headers = parse_toc_bpa2(
        "# 차 레\n# CONTENTS\n"
        "# I.「2016년도 예산안」개요 / 3\n"
        "1. 세입·세출 예산안 및 기금운용계획안 3\n"
        "2. 총수입·총지출 5\n"
        "# Ⅱ. 총량 분석 / 8\n"
        "1. 거시경제 전망 8\n"
        "# Ⅲ. 중점 분석 / 18\n"
        "5.「2016년도 예산안 편성 세부지침」준수 여부 54"
    )

    assert [(h.level, h.title, h.start_page) for h in headers] == [
        (1, "I. 「2016년도 예산안」개요", 3),
        (2, "1. 세입·세출 예산안 및 기금운용계획안", 3),
        (2, "2. 총수입·총지출", 5),
        (1, "II. 총량 분석", 8),
        (2, "1. 거시경제 전망", 8),
        (1, "III. 중점 분석", 18),
        (2, "5. 「2016년도 예산안 편성 세부지침」준수 여부", 54),
    ]


def test_bpa2_hangul_sub_entries_nest_under_their_item() -> None:
    # The 성인지 volume: 가./나. sub-entries, including a wrapped one.
    headers = parse_toc_bpa2(
        "# Ⅲ. 주요 부처별 분석 / 19\n"
        "1. 법무부 19\n"
        "가. 현황 19\n"
        "나. 분석 의견: 「교정교화」 사업 성과목표의 적정성 검토 필요\n"
        "(교정본부) 21\n"
        "2. 미래창조과학부 22"
    )

    assert [(h.level, h.start_page) for h in headers] == [
        (1, 19),
        (2, 19),
        (3, 19),
        (3, 21),
        (2, 22),
    ]
    assert headers[3].title.endswith("(교정본부)")


def test_bpa2_parts_and_chapters_deepen_the_tree() -> None:
    # The 분석종합 volume: part 1 holds roman sections directly; part 2 holds
    # chapters, whose roman sections are one level deeper.
    headers = parse_toc_bpa2(
        "# 제1부 거시·총량 분석 등\n"
        "# I.「2016년도 예산안」개요 / 3\n"
        "1. 세입·세출 예산안 및 기금운용계획안 3\n"
        "# IV. 성인지 예산서 분석 / 12\n"
        "# 제 2 부 부처별 분석\n"
        "# 제1장|국회운영위원회\n"
        "# I. 국회 / 17\n"
        "1. 사무실 환경 개선을 위한 카펫 바닥재 교체 필요 (관리국) 17\n"
        "# 제2장 | 법제사법위원회\n"
        "# I. 법무부 / 21\n"
        "# Ⅱ. 법제처 / 28\n"
        "1. 집행실적 고려 필요 (법령정보정책관실) 28"
    )

    assert [(h.level, h.title, h.start_page) for h in headers] == [
        (1, "제1부 거시·총량 분석 등", None),
        (2, "I. 「2016년도 예산안」개요", 3),
        (3, "1. 세입·세출 예산안 및 기금운용계획안", 3),
        (2, "IV. 성인지 예산서 분석", 12),
        (1, "제 2 부 부처별 분석", None),
        (2, "제1장|국회운영위원회", None),
        (3, "I. 국회", 17),
        (4, "1. 사무실 환경 개선을 위한 카펫 바닥재 교체 필요 (관리국)", 17),
        (2, "제2장 | 법제사법위원회", None),
        (3, "I. 법무부", 21),
        (3, "II. 법제처", 28),
        (4, "1. 집행실적 고려 필요 (법령정보정책관실)", 28),
    ]


def test_bpa2_entries_before_any_section_are_noise() -> None:
    headers = parse_toc_bpa2("1. 떠돌이 항목 3\nI. 개요 / 3\n1. 현황 3")

    assert [h.title for h in headers] == ["I. 개요", "1. 현황"]


def test_bpa2_a_trailing_divider_page_is_not_an_entry() -> None:
    # The located range ends on the page before printed p.3 — the first
    # part's divider page, whose text re-matches as a pageless part.
    headers = parse_toc_bpa2("# I. 개요 / 3\n1. 현황 3\n# 제 1 부")

    assert [h.title for h in headers] == ["I. 개요", "1. 현황"]


def test_bpa2_without_roman_sections_is_an_error() -> None:
    with pytest.raises(ExtractionError, match="No roman section"):
        parse_toc_bpa2("본문 첫 쪽\n예산 개요에 관한 문단.")


def test_bpa2_sections_without_entries_is_an_error() -> None:
    with pytest.raises(ExtractionError, match="no numbered entries"):
        parse_toc_bpa2("I. 개요 / 3\nⅡ. 총량 분석 / 8")


def test_bpa2_ranges_have_no_divider_lead() -> None:
    # A part or chapter starts where its first child starts — no book-face
    # offset — and ends contiguously before the next entry at its level.
    headers = parse_toc_bpa2(
        "# 제1부 총론\n# I. 개요 / 3\n1. 현황 3\n# 제 2 부 각론\n# I. 분석 / 10\n1. 항목 10"
    )
    blocks = [
        {"type": "page_number", "page_idx": 15, "text": "- 3 -"},
        {"type": "page_number", "page_idx": 22, "text": "- 10 -"},
        {"type": "text", "page_idx": 60, "text": "뒤표지"},
    ]

    assert page_index_ranges_bpa2(headers, blocks) == [
        (15, 21),  # 제1부: starts with I, no lead
        (15, 21),
        (15, 21),
        (22, 60),  # 제2부 runs to the end of the document
        (22, 60),
        (22, 60),
    ]


def test_bpa_2_is_registered_with_its_hooks() -> None:
    strategy = strategy_for("bpa-2")

    assert strategy.locate is locate_toc
    assert strategy.extract is parse_toc_bpa2
    assert strategy.ranges is page_index_ranges_bpa2
    assert strategy.llm is None
