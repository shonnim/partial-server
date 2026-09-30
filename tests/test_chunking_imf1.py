"""The imf-1 chunking strategy: header_contents rows in, chunks out.

Tokens are counted by a stand-in — one token per character — so the budget
tests are exact and no tokenizer has to load.
"""

import pytest

from app.models.header import Header, HeaderContent
from app.services.chunking_imf1 import (
    MAX_TOKENS,
    MIN_TOKENS,
    Footnote,
    ImfHierarchicalChunker,
    detect_level,
    inline_footnote,
    parse_footnotes,
)

# One token per character: the numbers in the budget tests are then
# character counts, which can be read off the text.
chunker = ImfHierarchicalChunker(count_tokens=len)

PART = Header(id=1, doc_id=None, header="I. 채무의 현명한 관리(Debt : Use it Wisely)", level=1)
CHAPTER = Header(id=2, doc_id=None, header="1. 세계 채무 현황", level=2, parent_id=1)
CHAPTER_2 = Header(id=3, doc_id=None, header="2.채무 수준의 중요성", level=2, parent_id=1)
SECTION = Header(id=4, doc_id=None, header="가. 선진국", level=3, parent_id=2)
HEADERS = {h.id: h for h in (PART, CHAPTER, CHAPTER_2, SECTION)}

BREADCRUMB = "[I. 채무의 현명한 관리(Debt : Use it Wisely) > 1. 세계 채무 현황]"


def row(
    id: int,
    content: str | None,
    *,
    header_id: int | None = 2,
    type: str = "text",
    page_idx: int = 3,
    caption: str | None = None,
    footnote: str | None = None,
) -> HeaderContent:
    return HeaderContent(
        id=id,
        header_id=header_id,
        type=type,
        content=content,
        caption=caption,
        footnote=footnote,
        page_idx=page_idx,
    )


# ---- level detection ---------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "level"),
    [
        ("I. 채무의 현명한 관리(Debt : Use it Wisely)", 1),
        ("II. 재정 전망", 1),
        ("IV.결론", 1),
        ("Ⅲ. 전각 로마 숫자", 1),
        ("1. 세계 채무 현황", 2),
        ("2.채무 수준의 중요성", 2),
        ("12. 소결", 2),
        ("가. 선진국", 3),
        ("나.신흥국", 3),
        ("라. 저소득국", 3),
    ],
)
def test_detects_the_three_outline_levels(text: str, level: int) -> None:
    assert detect_level(text) == level


@pytest.mark.parametrize(
    "text",
    [
        "1.5% 증가",
        "GDP 대비 1.5%",
        "2016. 10. 5. 발표",  # a date, not a chapter: the digit after the dot
        "□ 세계 전체의 비금융부문 채무",
        "○ 선진국 및 주요 신흥시장국",
        "- 금융의 세계화",
        "(1) 항목",
        "I.V. 혼합",
        "Item. 영문",
        "",
        None,
    ],
)
def test_does_not_mistake_numbers_or_bullets_for_headings(text: str | None) -> None:
    assert detect_level(text) is None


# ---- footnotes ---------------------------------------------------------------


def test_parses_one_footnote() -> None:
    assert parse_footnotes("1) 일반정부, 가계, 비금융기업") == [
        Footnote("1", "일반정부, 가계, 비금융기업")
    ]


def test_parses_several_footnotes_in_one_row() -> None:
    assert parse_footnotes("1) 일반정부, 가계, 비금융기업 2) 국제통화기금 기준\n3) 추정치") == [
        Footnote("1", "일반정부, 가계, 비금융기업"),
        Footnote("2", "국제통화기금 기준"),
        Footnote("3", "추정치"),
    ]


def test_footnote_row_without_a_marker_is_kept_unmarked() -> None:
    assert parse_footnotes("주: 추정치임") == [Footnote("", "주: 추정치임")]


def test_inlines_the_footnote_at_its_citation() -> None:
    text = "□ 세계 전체의 비금융부문1) 채무는 사상 최대치"
    assert inline_footnote(text, Footnote("1", "일반정부, 가계, 비금융기업")) == (
        "□ 세계 전체의 비금융부문(일반정부, 가계, 비금융기업) 채무는 사상 최대치"
    )


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # superscript digits, as the parser reads a raised marker
        ("○ (아시아 선진국¹)) 중국과 밀접한", "○ (아시아 선진국(싱가폴, 홍콩)) 중국과 밀접한"),
        # glued inside a parenthesis
        ("정보기술협정(ITA1))은 정보기술", "정보기술협정(ITA(싱가폴, 홍콩))은 정보기술"),
        # LaTeX, the paren outside the braces
        ("여력이 있음 $^{1}$ )", "여력이 있음 (싱가폴, 홍콩)"),
        # LaTeX, the paren inside the braces
        ("정책패키지 $^{1)}$ 의 일부", "정책패키지 (싱가폴, 홍콩) 의 일부"),
        # LaTeX with the paren lost altogether
        ("역자산 $^{1}$ (negative equity)", "역자산 (싱가폴, 홍콩) (negative equity)"),
    ],
)
def test_inline_reads_every_way_the_parser_prints_a_citation(text: str, expected: str) -> None:
    assert inline_footnote(text, Footnote("1", "싱가폴, 홍콩")) == expected


def test_inline_ignores_enumerations_and_other_numbers() -> None:
    note = Footnote("1", "각주")
    assert inline_footnote("(1) 첫째 항목", note) is None
    assert inline_footnote("1) 첫째 항목", note) is None  # starts the text: an enumeration
    assert inline_footnote("항목 1) 첫째", note) is None  # after a space
    assert inline_footnote("2011) 기준", note) is None  # part of a longer number


def test_footnote_is_inlined_and_its_id_joins_the_chunk() -> None:
    rows = [
        row(10, "I. 채무의 현명한 관리(Debt : Use it Wisely)", header_id=1),
        row(11, "1. 세계 채무 현황"),
        row(12, "□ 세계 전체의 비금융부문1) 채무는 사상 최대치"),
        row(13, "○ 선진국의 민간채무"),
        row(14, "1) 일반정부, 가계, 비금융기업", type="page_footnote"),
        row(15, "∙ 다음 페이지의 내용", page_idx=4),
    ]
    result = chunker.chunk(rows, HEADERS)

    assert result.warnings == []
    assert len(result.chunks) == 1
    chunk = result.chunks[0]
    assert chunk.text == (
        f"{BREADCRUMB}\n"
        "□ 세계 전체의 비금융부문(일반정부, 가계, 비금융기업) 채무는 사상 최대치\n"
        "○ 선진국의 민간채무\n"
        "∙ 다음 페이지의 내용"
    )
    assert chunk.head_content_ids == [12, 14, 13, 15]
    assert chunk.chunk_type == "text"
    assert chunk.section_path == BREADCRUMB
    assert chunk.token_count == len(chunk.text)


def test_several_footnotes_in_one_row_land_on_their_own_citations() -> None:
    rows = [
        row(11, "1. 세계 채무 현황"),
        row(12, "□ 비금융부문1) 채무와 공공부문2) 채무"),
        row(13, "○ 다른 문장"),
        row(14, "1) 일반정부, 가계, 비금융기업 2) 중앙정부", type="page_footnote"),
    ]
    result = chunker.chunk(rows, HEADERS)

    assert result.chunks[0].text.endswith(
        "□ 비금융부문(일반정부, 가계, 비금융기업) 채무와 공공부문(중앙정부) 채무\n○ 다른 문장"
    )
    # The footnote row is listed once, however many notes it supplied.
    assert result.chunks[0].head_content_ids == [12, 14, 13]


def test_only_the_first_citation_on_the_page_is_replaced() -> None:
    rows = [
        row(11, "1. 세계 채무 현황"),
        row(12, "□ 첫 인용1) 그리고 둘째 인용1)"),
        row(13, "○ 셋째 인용1)"),
        row(14, "1) 각주", type="page_footnote"),
    ]
    result = chunker.chunk(rows, HEADERS)

    assert result.chunks[0].text.endswith("□ 첫 인용(각주) 그리고 둘째 인용1)\n○ 셋째 인용1)")


def test_footnote_cited_on_another_page_is_appended_with_a_warning() -> None:
    rows = [
        row(11, "1. 세계 채무 현황"),
        row(12, "□ 인용 없음", page_idx=3),
        row(13, "○ 다른 페이지의 인용1)", page_idx=4),
        row(14, "1) 각주 내용", type="page_footnote", page_idx=3),
    ]
    result = chunker.chunk(rows, HEADERS)

    assert len(result.warnings) == 1
    assert "not cited" in result.warnings[0]
    assert result.chunks[0].text.endswith("□ 인용 없음 (1) 각주 내용)\n○ 다른 페이지의 인용1)")
    assert result.chunks[0].head_content_ids == [12, 14, 13]


def test_footnote_rows_never_become_content_on_their_own() -> None:
    rows = [
        row(11, "1. 세계 채무 현황"),
        row(12, "□ 본문"),
        row(14, "1) 각주", type="page_footnote", page_idx=9),
    ]
    result = chunker.chunk(rows, HEADERS)

    assert len(result.chunks) == 1
    assert "각주" in result.chunks[0].text  # appended to the nearest text row
    assert not any(c.text.strip().endswith("1) 각주") for c in result.chunks)


# ---- filtering -----------------------------------------------------------------


def test_rows_repeating_their_header_are_dropped_but_still_move_the_path() -> None:
    rows = [
        row(1, "표지", header_id=None, page_idx=0),
        row(2, "목차", header_id=None, page_idx=2),
        row(10, "I. 채무의 현명한 관리(Debt : Use it Wisely)", header_id=1),
        row(11, "1. 세계 채무 현황"),
        row(12, "□ 첫 장의 본문"),
        row(20, "2.  채무 수준의  중요성", header_id=3),  # whitespace differs from the header
        row(21, "□ 둘째 장의 본문", header_id=3),
    ]
    result = chunker.chunk(rows, HEADERS)

    assert [c.text for c in result.chunks] == [
        f"{BREADCRUMB}\n□ 첫 장의 본문",
        "[I. 채무의 현명한 관리(Debt : Use it Wisely) > 2.채무 수준의 중요성]\n□ 둘째 장의 본문",
    ]
    assert [c.head_content_ids for c in result.chunks] == [[12], [21]]


def test_level_three_section_extends_the_breadcrumb_and_bounds_the_chunk() -> None:
    rows = [
        row(11, "1. 세계 채무 현황"),
        row(12, "□ 장 머리의 본문"),
        row(30, "가. 선진국", header_id=4),
        row(31, "□ 절의 본문", header_id=4),
    ]
    result = chunker.chunk(rows, HEADERS)

    assert [c.text for c in result.chunks] == [
        f"{BREADCRUMB}\n□ 장 머리의 본문",
        f"{BREADCRUMB[:-1]} > 가. 선진국]\n□ 절의 본문",
    ]


def test_heading_printed_as_plain_text_moves_the_path() -> None:
    # The outline only knows the part; the chapters show up as text rows.
    rows = [
        row(10, "I. 채무의 현명한 관리(Debt : Use it Wisely)", header_id=1),
        row(11, "1. 세계 채무 현황", header_id=1),
        row(12, "□ 첫 장의 본문", header_id=1),
        row(13, "2. 채무 수준의 중요성", header_id=1),
        row(14, "□ 둘째 장의 본문", header_id=1),
    ]
    result = chunker.chunk(rows, {1: PART})

    assert [c.text for c in result.chunks] == [
        f"{BREADCRUMB}\n□ 첫 장의 본문",
        "[I. 채무의 현명한 관리(Debt : Use it Wisely) > 2. 채무 수준의 중요성]\n□ 둘째 장의 본문",
    ]
    assert result.warnings == []


def test_content_under_a_part_with_no_chapter_is_warned_about() -> None:
    rows = [
        row(10, "I. 채무의 현명한 관리(Debt : Use it Wisely)", header_id=1),
        row(12, "□ 장 없이 바로 나오는 본문", header_id=1),
    ]
    result = chunker.chunk(rows, {1: PART})

    assert result.chunks[0].text == (
        "[I. 채무의 현명한 관리(Debt : Use it Wisely)]\n□ 장 없이 바로 나오는 본문"
    )
    assert len(result.warnings) == 1
    assert "no chapter" in result.warnings[0]


def test_header_without_a_marker_falls_back_to_its_outline_level() -> None:
    summary = Header(id=9, doc_id=None, header="요약", level=1)
    rows = [row(50, "□ 요약의 본문", header_id=9)]
    result = chunker.chunk(rows, {9: summary})

    assert result.chunks[0].text == "[요약]\n□ 요약의 본문"
    assert any("matches no" in w for w in result.warnings)


# ---- the token budget -----------------------------------------------------------


def bullet(prefix: str, n: int, width: int) -> str:
    """A body line of exactly ``width`` characters, so its token cost is known."""
    head = f"{prefix} 문장 {n} "
    return head + "가" * (width - len(head))


def test_text_is_cut_at_row_boundaries_within_the_budget() -> None:
    # Six 200-character rows under a 41-character breadcrumb + newline: each
    # row costs 201 with its newline, so three fit (42 + 603 = 645 ≤ 800)
    # and the fourth would not.
    rows = [row(11, "1. 세계 채무 현황")] + [
        row(20 + i, bullet("○", i, 200)) for i in range(6)
    ]
    result = chunker.chunk(rows, HEADERS)

    assert [c.head_content_ids for c in result.chunks] == [[20, 21, 22], [23, 24, 25]]
    for chunk in result.chunks:
        assert MIN_TOKENS <= chunk.token_count <= MAX_TOKENS
        assert chunk.text.startswith(BREADCRUMB + "\n")


def test_a_box_bullet_is_preferred_as_the_cut_point() -> None:
    # □ with two ○ children, twice over. The first block is 3 × 150 = 450
    # past the breadcrumb; the second □ arrives once the target is passed
    # and everything would still fit under 800, but the cut is made at the
    # □ so the block keeps its children.
    rows = [
        row(11, "1. 세계 채무 현황"),
        row(20, bullet("□", 1, 150)),
        row(21, bullet("○", 2, 150)),
        row(22, bullet("○", 3, 150)),
        row(23, bullet("○", 4, 150)),
        row(24, bullet("□", 5, 150)),
        row(25, bullet("○", 6, 150)),
        row(26, bullet("○", 7, 150)),
        row(27, bullet("○", 8, 150)),
    ]
    result = chunker.chunk(rows, HEADERS)

    assert [c.head_content_ids for c in result.chunks] == [[20, 21, 22, 23], [24, 25, 26, 27]]
    assert result.chunks[1].text.split("\n")[1].startswith("□")


def test_an_overflowing_group_is_cut_back_to_its_last_box_bullet() -> None:
    # □ + 3 children = 4 × 120 = 480 past the breadcrumb (over the minimum),
    # then a second □ and its children. The overflow comes mid-way through
    # the second block, and the cut goes back to that □.
    rows = [
        row(11, "1. 세계 채무 현황"),
        row(20, bullet("□", 1, 120)),
        row(21, bullet("○", 2, 120)),
        row(22, bullet("○", 3, 120)),
        row(23, bullet("○", 4, 120)),
        row(24, bullet("□", 5, 100)),
        row(25, bullet("○", 6, 100)),
        row(26, bullet("○", 7, 100)),
        row(27, bullet("○", 8, 100)),
    ]
    result = chunker.chunk(rows, HEADERS)

    assert [c.head_content_ids for c in result.chunks] == [[20, 21, 22, 23], [24, 25, 26, 27]]


def test_a_short_tail_is_merged_back_when_it_fits() -> None:
    # 4 × 150 pass the target (646), so the □ that follows opens a new
    # chunk — but that leaves a 100-character tail under the minimum, and
    # 646 + 101 still fits under 800, so the tail is folded back in.
    rows = [row(11, "1. 세계 채무 현황")] + [
        row(20 + i, bullet("○", i, 150)) for i in range(4)
    ] + [row(30, bullet("□", 9, 100))]
    result = chunker.chunk(rows, HEADERS)

    assert len(result.chunks) == 1
    assert result.chunks[0].head_content_ids == [20, 21, 22, 23, 30]
    assert result.chunks[0].token_count <= MAX_TOKENS


def test_a_short_tail_stays_apart_when_merging_would_overflow() -> None:
    rows = [row(11, "1. 세계 채무 현황")] + [
        row(20 + i, bullet("○", i, 200)) for i in range(3)
    ] + [row(30, bullet("○", 9, 300))]
    result = chunker.chunk(rows, HEADERS)

    assert [c.head_content_ids for c in result.chunks] == [[20, 21, 22], [30]]


def test_a_short_section_is_one_short_chunk() -> None:
    rows = [row(11, "1. 세계 채무 현황"), row(20, "□ 한 줄뿐인 장")]
    result = chunker.chunk(rows, HEADERS)

    assert len(result.chunks) == 1
    assert result.chunks[0].token_count < MIN_TOKENS


def test_a_single_row_over_the_budget_is_split_by_lines() -> None:
    lines = [bullet("-", i, 300) for i in range(4)]  # 1200 characters in one list row
    rows = [row(11, "1. 세계 채무 현황"), row(20, "\n".join(lines), type="list")]
    result = chunker.chunk(rows, HEADERS)

    assert len(result.chunks) == 2
    assert all(c.token_count <= MAX_TOKENS for c in result.chunks)
    assert "".join(c.text for c in result.chunks).count("문장") == 4
    assert result.chunks[0].head_content_ids == [20]
    assert result.chunks[1].head_content_ids == []
    assert any("split" in w for w in result.warnings)


# ---- tables, images, charts ------------------------------------------------------

TABLE_HTML = (
    "<table><tr><td></td><td>2016</td><td>2017</td></tr>"
    "<tr><td>Asia</td><td>5.3</td><td>5.3</td></tr></table>"
)


def test_table_is_its_own_chunk_as_markdown_with_caption_and_footnote() -> None:
    rows = [
        row(11, "1. 세계 채무 현황"),
        row(20, "□ 표 앞의 본문"),
        row(
            21,
            TABLE_HTML,
            type="table",
            caption="<표 1> 성장률 전망\n(단위: %)",
            footnote="자료: IMF",
        ),
        row(22, "□ 표 뒤의 본문"),
    ]
    result = chunker.chunk(rows, HEADERS)

    assert [c.chunk_type for c in result.chunks] == ["text", "table"]
    text, table = result.chunks
    # Text before and after the table stays one chunk, placed at its first row.
    assert text.text == f"{BREADCRUMB}\n□ 표 앞의 본문\n□ 표 뒤의 본문"
    assert text.head_content_ids == [20, 22]
    assert table.text.startswith(f"{BREADCRUMB}\n<표 1> 성장률 전망\n(단위: %)\n|")
    assert "| Asia | 5.3 | 5.3 |" in table.text
    assert "<table>" not in table.text
    assert table.text.endswith("\n자료: IMF")
    assert table.head_content_ids == [21]
    assert table.needs_split is False


def test_table_without_caption_or_footnote_has_no_blank_lines() -> None:
    rows = [row(11, "1. 세계 채무 현황"), row(21, TABLE_HTML, type="table")]
    result = chunker.chunk(rows, HEADERS)

    lines = result.chunks[0].text.split("\n")
    assert lines[0] == BREADCRUMB
    assert lines[1].startswith("|")
    assert "" not in lines


def test_table_that_cannot_be_read_is_kept_as_html_with_a_warning() -> None:
    rows = [row(11, "1. 세계 채무 현황"), row(21, "<table></table>", type="table")]
    result = chunker.chunk(rows, HEADERS)

    assert result.chunks[0].text == f"{BREADCRUMB}\n<table></table>"
    assert any("could not be read" in w for w in result.warnings)


def test_oversized_table_is_saved_whole_and_flagged() -> None:
    cells = "".join(f"<tr><td>row {i}</td><td>{'가' * 40}</td></tr>" for i in range(30))
    rows = [row(11, "1. 세계 채무 현황"), row(21, f"<table>{cells}</table>", type="table")]
    result = chunker.chunk(rows, HEADERS)

    assert len(result.chunks) == 1
    assert result.chunks[0].needs_split is True
    assert result.chunks[0].token_count > 900
    assert any("needs_split" in w for w in result.warnings)


@pytest.mark.parametrize("kind", ["image", "chart"])
def test_image_and_chart_are_their_own_chunks(kind: str) -> None:
    rows = [
        row(11, "1. 세계 채무 현황"),
        row(
            21,
            "| Year | Debt |\n| --- | --- |\n| 2016 | 225 |",
            type=kind,
            caption="[그림 1] 세계 총채무 추이",
            footnote="자료: IMF, Fiscal Monitor",
        ),
    ]
    result = chunker.chunk(rows, HEADERS)

    assert len(result.chunks) == 1
    assert result.chunks[0].chunk_type == kind
    assert result.chunks[0].text == (
        f"{BREADCRUMB}\n[그림 1] 세계 총채무 추이\n| Year | Debt |\n| --- | --- |\n| 2016 | 225 |\n"
        "자료: IMF, Fiscal Monitor"
    )
    assert result.chunks[0].head_content_ids == [21]


def test_image_with_only_a_description_omits_the_missing_parts() -> None:
    rows = [row(11, "1. 세계 채무 현황"), row(21, "로고를 보여주는 이미지", type="image")]
    result = chunker.chunk(rows, HEADERS)

    assert result.chunks[0].text == f"{BREADCRUMB}\n로고를 보여주는 이미지"


def test_chart_with_caption_only_is_still_a_chunk() -> None:
    rows = [row(11, "1. 세계 채무 현황"), row(21, None, type="chart", caption="1. Advanced Economies")]
    result = chunker.chunk(rows, HEADERS)

    assert result.chunks[0].text == f"{BREADCRUMB}\n1. Advanced Economies"
    # A chart caption that looks like a chapter number never moves the path.
    assert result.chunks[0].section_path == BREADCRUMB


def test_chunks_keep_reading_order_across_sections_and_figures() -> None:
    rows = [
        row(11, "1. 세계 채무 현황"),
        row(20, "□ 첫 장"),
        row(21, "그림", type="image", caption="[그림 1]"),
        row(30, "2.채무 수준의 중요성", header_id=3),
        row(31, "□ 둘째 장", header_id=3),
        row(32, TABLE_HTML, type="table", header_id=3),
    ]
    result = chunker.chunk(rows, HEADERS)

    assert [(c.chunk_type, c.head_content_ids) for c in result.chunks] == [
        ("text", [20]),
        ("image", [21]),
        ("text", [31]),
        ("table", [32]),
    ]
