"""The oft-1 strategy: locating the TOC, its regex parser, the page arithmetic."""

import pytest

from app.services.header_extraction import ExtractedHeader, ExtractionError
from app.services.header_oft import (
    TOC_FIRST_PAGE,
    TOC_LAST_PAGE,
    amend_toc_oft,
    locate_toc_oft,
    page_index_ranges_oft,
    parse_toc_oft,
)
from app.services.header_pages import GROUP_STRATEGIES


def _text(page: int, text: str, heading: bool = False) -> dict:
    block = {"type": "text", "page_idx": page, "text": text}
    if heading:
        block["text_level"] = 1
    return block


# ---- locate ------------------------------------------------------------------


def test_locate_reads_the_marker_page_alone() -> None:
    # The page after the TOC is the summary page, so the range never runs on.
    blocks = [_text(0, "표지"), _text(2, "목 차"), _text(4, "본문")]
    assert locate_toc_oft(blocks) == (3, 3)


def test_locate_finds_the_split_title_with_its_interloper() -> None:
    # The group's usual TOC page: "목 차" as one block per syllable, the
    # running header read between them.
    blocks = [
        _text(2, "목", heading=True),
        _text(2, "재정동향", heading=True),
        _text(2, "차", heading=True),
    ]
    assert locate_toc_oft(blocks) == (3, 3)


def test_locate_rejects_a_marker_past_the_window() -> None:
    # A 표 목차 deeper in the document is not this group's contents page;
    # the static window stands.
    assert locate_toc_oft([_text(9, "목차")]) is None


def test_locate_without_a_marker_defers_to_the_static_range() -> None:
    assert locate_toc_oft([_text(0, "본문")]) is None
    assert locate_toc_oft(None) is None


# ---- the TOC parser ------------------------------------------------------------

# The parsed TOC page of a real report (2016년_재정동향_10월_1호), markdown
# dressing and all: the split title, the running header, and the entries in
# every spelling the page prints — full-width numerals, dot leaders, a
# letter-spaced title, a missing space after the dot.
REAL_TOC = """# 목

재정동향

# 차

I. 국제기구 2

Ⅱ. 호주....11

Ⅲ. 캐 나 다....12

IV. 프랑스 13

V. 독 일 18

VI.일본 19

VII. 영국 20

VIII. 미국 21
"""


def test_parse_toc_reads_the_real_page() -> None:
    headers = parse_toc_oft(REAL_TOC)

    # Only what the TOC prints: the summary section, when the document has
    # one, is amend_toc_oft's to add.
    assert [(h.title, h.level, h.start_page) for h in headers] == [
        ("I. 국제기구", 1, 2),
        ("II. 호주", 1, 11),
        ("III. 캐 나 다", 1, 12),
        ("IV. 프랑스", 1, 13),
        ("V. 독 일", 1, 18),
        ("VI. 일본", 1, 19),
        ("VII. 영국", 1, 20),
        ("VIII. 미국", 1, 21),
    ]


def test_parse_toc_ignores_everything_that_is_not_an_entry() -> None:
    # The split 목/차 syllables and the running header used to come back as
    # headings from the model; the pattern never matches them.
    titles = [h.title for h in parse_toc_oft(REAL_TOC)]
    assert "목" not in titles and "차" not in titles and "재정동향" not in titles


def test_parse_toc_refuses_a_page_with_no_entries() -> None:
    with pytest.raises(ExtractionError):
        parse_toc_oft("# 요 약\n\n○ 본문 문장")


# ---- the amend hook ------------------------------------------------------------


def test_amend_inserts_요약_when_the_page_after_the_toc_prints_it() -> None:
    # The usual volume: the letter-spaced "요 약" title parses split into
    # syllables on the page right after the TOC marker.
    blocks = [
        _text(2, "목 차"),
        _text(3, "요", heading=True),
        _text(3, "재정동향", heading=True),
        _text(3, "약", heading=True),
        _text(4, "I. 국제기구", heading=True),
    ]
    headers = [ExtractedHeader(title="I. 국제기구", level=1, start_page=2)]

    amended = amend_toc_oft(headers, blocks)
    assert [h.title for h in amended] == ["요약", "I. 국제기구"]
    assert amended[0] == ExtractedHeader(title="요약", level=1, start_page=None)


def test_amend_adds_nothing_when_the_document_has_no_summary_page() -> None:
    # The volume that used to grow a phantom 요약 entry: no summary after the
    # TOC, but the word 요약 appears on a body page (idx 37) — which title
    # matching then returned as the section's whole range (37–37).
    blocks = [
        _text(2, "목 차"),
        _text(3, "2016 재정동향"),  # a divider, not a summary
        _text(4, "I. 국제기구", heading=True),
        _text(37, "요약 및 시사점"),
    ]
    headers = [ExtractedHeader(title="I. 국제기구", level=1, start_page=2)]

    assert amend_toc_oft(headers, blocks) == headers


def test_amend_does_not_count_an_incidental_요약_inside_a_sentence() -> None:
    blocks = [_text(2, "목 차"), _text(3, "이 장은 주요 내용을 요약 정리한 것임")]
    headers = [ExtractedHeader(title="I. 개요", level=1, start_page=None)]
    assert amend_toc_oft(headers, blocks) == headers


def test_amend_leaves_junk_blocks_alone() -> None:
    headers = [ExtractedHeader(title="I. 개요", level=1, start_page=None)]
    assert amend_toc_oft(headers, None) == headers
    assert amend_toc_oft(headers, [_text(0, "본문")]) == headers


# ---- ranges ------------------------------------------------------------------

CONTENTS = _text(2, "목 차")


def _page_number(page: int, printed: int) -> dict:
    return {"type": "page_number", "page_idx": page, "text": f"• {printed}"}


def test_arithmetic_starts_survive_split_headings_and_missing_numbers() -> None:
    # The real failure: body headings parse split ("IV" and "프랑스" as two
    # blocks), so no title matches — and some printed numbers sit in footer
    # blocks the page-number index never sees. The majority vote (c = 2 from
    # printed 2→4 and 11→13) still places every entry.
    blocks = [
        CONTENTS,
        _text(3, "요 약", heading=True),
        _page_number(4, 2),
        _text(4, "I", heading=True),
        _text(4, "국제기구", heading=True),
        _page_number(13, 11),
        _text(13, "II", heading=True),
        _text(13, "호주", heading=True),
        _text(14, "III", heading=True),  # printed 12 never captured
        _text(14, "캐 나 다", heading=True),
        _text(15, "본문"),
    ]
    headers = amend_toc_oft(
        parse_toc_oft("I. 국제기구 2\nII. 호주 11\nIII. 캐나다 12"), blocks
    )

    assert page_index_ranges_oft(headers, blocks) == [
        (3, 3),  # 요약: the page after the TOC marker, ending before I.
        (4, 12),
        (13, 13),
        (14, 15),  # the last entry runs to the document's end
    ]


def test_sections_end_on_the_page_before_the_next_start() -> None:
    blocks = [
        CONTENTS,
        _text(4, "I. 개요", heading=True),
        _text(5, "본문"),
        _text(9, "II. 세입", heading=True),
        _text(13, "III. 세출", heading=True),
        _text(20, "본문"),
    ]
    headers = [
        ExtractedHeader(title="I. 개요", level=1, start_page=None),
        ExtractedHeader(title="II. 세입", level=1, start_page=None),
        ExtractedHeader(title="III. 세출", level=1, start_page=None),
    ]

    # No shared pages: each entry ends the page before the next, the last
    # runs to the document's end — no opens-its-page check needed.
    assert page_index_ranges_oft(headers, blocks) == [(4, 8), (9, 12), (13, 20)]


def test_a_child_ends_before_the_next_entry_at_any_shallower_level() -> None:
    blocks = [
        CONTENTS,
        _text(4, "I. 개요", heading=True),
        _text(5, "1. 총괄", heading=True),
        _text(9, "II. 세입", heading=True),
        _text(11, "본문"),
    ]
    headers = [
        ExtractedHeader(title="I. 개요", level=1, start_page=None),
        ExtractedHeader(title="1. 총괄", level=2, start_page=None),
        ExtractedHeader(title="II. 세입", level=1, start_page=None),
    ]

    assert page_index_ranges_oft(headers, blocks) == [(4, 8), (5, 8), (9, 11)]


def test_an_unknown_following_start_leaves_the_end_unknown() -> None:
    blocks = [
        CONTENTS,
        _text(4, "I. 개요", heading=True),
        _text(9, "본문"),  # "II. 세입" never matches anything
    ]
    headers = [
        ExtractedHeader(title="I. 개요", level=1, start_page=None),
        ExtractedHeader(title="II. 세입", level=1, start_page=None),
    ]

    assert page_index_ranges_oft(headers, blocks) == [(4, None), (None, 9)]


def test_two_starts_on_one_page_cannot_end_an_entry_before_it_begins() -> None:
    # Two headings matched onto one page contradict the group's layout; the
    # earlier entry keeps its own page rather than ending before it starts.
    blocks = [
        CONTENTS,
        _text(4, "I. 개요", heading=True),
        _text(4, "II. 세입", heading=True),
    ]
    headers = [
        ExtractedHeader(title="I. 개요", level=1, start_page=None),
        ExtractedHeader(title="II. 세입", level=1, start_page=None),
    ]

    assert page_index_ranges_oft(headers, blocks) == [(4, 4), (4, 4)]


def test_the_summary_entry_starts_on_the_page_after_the_toc() -> None:
    # The summary's own "요 약" heading parses split into syllables, so the
    # title matching can never find it; the group prints the summary on the
    # page right after the TOC. A 요약 printed deeper in the body must not
    # drag the entry there through title matching.
    blocks = [
        _text(2, "목 차"),
        _text(3, "요", heading=True),
        _text(3, "약", heading=True),
        _text(3, "○ 재정 수지는 흑자"),
        _text(5, "I. 개요", heading=True),
        _text(8, "요약", heading=True),  # a body heading reusing the word
        _text(9, "본문"),
    ]
    headers = [
        ExtractedHeader(title="요 약", level=1, start_page=None),
        ExtractedHeader(title="I. 개요", level=1, start_page=None),
    ]

    assert page_index_ranges_oft(headers, blocks) == [(3, 4), (5, 9)]


def test_junk_blocks_yield_unknown_ranges() -> None:
    headers = [ExtractedHeader(title="I. 개요", level=1, start_page=None)]
    assert page_index_ranges_oft(headers, None) == [(None, None)]
    assert page_index_ranges_oft([], []) == []


# ---- the registry entry ------------------------------------------------------


def test_oft1_strategy_wires_the_hooks_and_the_window() -> None:
    strategy = GROUP_STRATEGIES["oft-1"]
    assert (strategy.start_page, strategy.end_page) == (TOC_FIRST_PAGE, TOC_LAST_PAGE)
    assert strategy.locate is locate_toc_oft
    assert strategy.extract is parse_toc_oft  # deterministic, no model call
    assert strategy.amend is amend_toc_oft
    assert strategy.ranges is page_index_ranges_oft
