"""The imf-2 chunking strategy: imf-1's walk under imf-2's markers.

Only what imf-2 changes is tested here — the no-dot roman numeral at level 1
and ``○`` as the bullet a text chunk is cut at. Everything inherited
(footnotes, tables, budgets, ordering) is covered by test_chunking_imf1.

Tokens are counted by a stand-in — one token per character — so the budget
tests are exact and no tokenizer has to load.
"""

import pytest

from app.models.header import Header, HeaderContent
from app.services.chunking_imf1 import detect_level
from app.services.chunking_imf2 import Imf2HierarchicalChunker

chunker = Imf2HierarchicalChunker(count_tokens=len)


def detect(text: str | None) -> int | None:
    return detect_level(text, Imf2HierarchicalChunker.LEVEL_PATTERNS)


PART = Header(id=1, doc_id=None, header="Ⅰ 서론", level=1)
CHAPTER = Header(id=2, doc_id=None, header="1. 세계 경제 동향", level=2, parent_id=1)
CHAPTER_2 = Header(id=3, doc_id=None, header="2. 정책 대응", level=2, parent_id=1)
SECTION = Header(id=4, doc_id=None, header="가. 선진국", level=3, parent_id=2)
HEADERS = {h.id: h for h in (PART, CHAPTER, CHAPTER_2, SECTION)}

BREADCRUMB = "[Ⅰ 서론 > 1. 세계 경제 동향]"


def row(
    id: int,
    content: str | None,
    *,
    header_id: int | None = 2,
    type: str = "text",
    page_idx: int = 3,
) -> HeaderContent:
    return HeaderContent(
        id=id, header_id=header_id, type=type, content=content, page_idx=page_idx
    )


# ---- level detection ---------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "level"),
    [
        ("I 서론", 1),
        ("II 재정 전망", 1),
        ("VI 결론", 1),
        ("Ⅲ 전각 로마 숫자", 1),
        ("X Overview", 1),
        # the dot is optional: the group's documents print both forms
        ("I. 세계 경제전망과 정책", 1),
        ("IV.결론", 1),
        ("1. 세계 경제 동향", 2),
        ("2.정책 대응", 2),
        ("가. 선진국", 3),
        ("나.신흥국", 3),
    ],
)
def test_detects_the_three_outline_levels(text: str, level: int) -> None:
    assert detect(text) == level


@pytest.mark.parametrize(
    "text",
    [
        "I think so",  # English prose, not a heading
        "IMF 세계경제전망",  # the numeral runs straight into letters
        "I.V. 혼합",  # a dotted run, not a part marker
        "I.1 항목",
        "○ 세계 경제는 완만한 회복세",
        "■ 선진국의 성장률",
        "- 미국의 소비 지출",
        "1.5% 증가",
        "(1) 항목",
        "",
        None,
    ],
)
def test_does_not_mistake_prose_or_bullets_for_headings(text: str | None) -> None:
    assert detect(text) is None


# ---- the walk under imf-2 markers ----------------------------------------------


def test_breadcrumb_is_built_from_the_no_dot_part_heading() -> None:
    rows = [
        row(10, "Ⅰ 서론", header_id=1),
        row(11, "1. 세계 경제 동향"),
        row(12, "○ 세계 경제는 완만한 회복세"),
        row(13, "■ 선진국의 성장률"),
        row(14, "- 미국의 소비 지출"),
    ]
    result = chunker.chunk(rows, HEADERS)

    assert result.warnings == []
    assert len(result.chunks) == 1
    assert result.chunks[0].text == (
        f"{BREADCRUMB}\n○ 세계 경제는 완만한 회복세\n■ 선진국의 성장률\n- 미국의 소비 지출"
    )
    assert result.chunks[0].section_path == BREADCRUMB
    assert result.chunks[0].head_content_ids == [12, 13, 14]


def test_heading_printed_as_plain_text_moves_the_path() -> None:
    # The outline only knows the part; the chapters show up as text rows.
    rows = [
        row(10, "Ⅰ 서론", header_id=1),
        row(11, "1. 세계 경제 동향", header_id=1),
        row(12, "○ 첫 장의 본문", header_id=1),
        row(13, "2. 정책 대응", header_id=1),
        row(14, "○ 둘째 장의 본문", header_id=1),
    ]
    result = chunker.chunk(rows, {1: PART})

    assert [c.text for c in result.chunks] == [
        f"{BREADCRUMB}\n○ 첫 장의 본문",
        "[Ⅰ 서론 > 2. 정책 대응]\n○ 둘째 장의 본문",
    ]
    assert result.warnings == []


def test_level_three_section_extends_the_breadcrumb() -> None:
    rows = [
        row(11, "1. 세계 경제 동향"),
        row(12, "○ 장 머리의 본문"),
        row(30, "가. 선진국", header_id=4),
        row(31, "○ 절의 본문", header_id=4),
    ]
    result = chunker.chunk(rows, HEADERS)

    assert [c.text for c in result.chunks] == [
        f"{BREADCRUMB}\n○ 장 머리의 본문",
        f"{BREADCRUMB[:-1]} > 가. 선진국]\n○ 절의 본문",
    ]


# ---- the circle bullet as the cut point -----------------------------------------


def bullet(prefix: str, n: int, width: int) -> str:
    """A body line of exactly ``width`` characters, so its token cost is known."""
    head = f"{prefix} 문장 {n} "
    return head + "가" * (width - len(head))


def test_a_circle_bullet_is_preferred_as_the_cut_point() -> None:
    # ○ with three ■ children, twice over, 150 characters each under a
    # 20-character breadcrumb. The second ○ arrives once the target is
    # passed (625 ≥ 600) and everything would still fit under 800 (776), but
    # the cut is made at the ○ so the bullet keeps its children.
    rows = [
        row(11, "1. 세계 경제 동향"),
        row(20, bullet("○", 1, 150)),
        row(21, bullet("■", 2, 150)),
        row(22, bullet("■", 3, 150)),
        row(23, bullet("■", 4, 150)),
        row(24, bullet("○", 5, 150)),
        row(25, bullet("■", 6, 150)),
        row(26, bullet("■", 7, 150)),
        row(27, bullet("■", 8, 150)),
    ]
    result = chunker.chunk(rows, HEADERS)

    assert [c.head_content_ids for c in result.chunks] == [[20, 21, 22, 23], [24, 25, 26, 27]]
    assert result.chunks[1].text.split("\n")[1].startswith("○")


def test_an_overflowing_group_is_cut_back_to_its_last_circle_bullet() -> None:
    # ○ + 3 children = 4 × 140 = 560 past the breadcrumb (over the minimum),
    # then a second ○ and its children. The overflow comes mid-way through
    # the second block, and the cut goes back to that ○.
    rows = [
        row(11, "1. 세계 경제 동향"),
        row(20, bullet("○", 1, 140)),
        row(21, bullet("■", 2, 140)),
        row(22, bullet("■", 3, 140)),
        row(23, bullet("■", 4, 140)),
        row(24, bullet("○", 5, 100)),
        row(25, bullet("■", 6, 100)),
        row(26, bullet("■", 7, 100)),
        row(27, bullet("■", 8, 100)),
    ]
    result = chunker.chunk(rows, HEADERS)

    assert [c.head_content_ids for c in result.chunks] == [[20, 21, 22, 23], [24, 25, 26, 27]]
