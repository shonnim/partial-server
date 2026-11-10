"""The oft-1 chunking strategy: outline levels, ○ bullets, split syllables.

Only what oft-1 changes is tested here — levels read from the stored outline
with no markers and no warnings, ``○`` as the bullet a text chunk is cut at,
and the single-character rows a split title leaves behind. Everything
inherited (footnotes, tables, budgets, ordering) is covered by
test_chunking_imf1.

Tokens are counted by a stand-in — one token per character — so the budget
tests are exact and no tokenizer has to load.
"""

from app.models.header import Header, HeaderContent
from app.services.chunking import CHUNKERS
from app.services.chunking_oft import OftHierarchicalChunker

chunker = OftHierarchicalChunker(count_tokens=len)

# The group's headings carry no numbering: plain titles, levels from the TOC.
SUMMARY = Header(id=1, doc_id=None, header="요 약", level=1)
PART = Header(id=2, doc_id=None, header="재정 운용 동향", level=1)
CHAPTER = Header(id=3, doc_id=None, header="총수입", level=2, parent_id=2)
CHAPTER_2 = Header(id=4, doc_id=None, header="총지출", level=2, parent_id=2)
HEADERS = {h.id: h for h in (SUMMARY, PART, CHAPTER, CHAPTER_2)}

BREADCRUMB = "[재정 운용 동향 > 총수입]"


def row(
    id: int,
    content: str | None,
    *,
    header_id: int | None = 3,
    type: str = "text",
    page_idx: int = 5,
) -> HeaderContent:
    return HeaderContent(
        id=id, header_id=header_id, type=type, content=content, page_idx=page_idx
    )


def test_registered_for_the_group() -> None:
    assert CHUNKERS["oft-1"] is OftHierarchicalChunker


def test_breadcrumb_comes_from_the_outline_without_warnings() -> None:
    rows = [
        row(10, "총수입"),  # the heading printed again: dropped, not content
        row(11, "경과에 대한 평문 설명이다."),
        row(12, "국세수입은 증가 추세였다."),  # a printed □ the parser dropped
        row(13, "○ 국세수입 세부 내용"),
        row(14, "∙ 소득세는 증가"),
        row(15, "- 법인세는 감소"),
    ]
    result = chunker.chunk(rows, HEADERS)

    assert result.warnings == []
    assert len(result.chunks) == 1
    assert result.chunks[0].section_path == BREADCRUMB
    assert result.chunks[0].text == (
        f"{BREADCRUMB}\n경과에 대한 평문 설명이다.\n국세수입은 증가 추세였다."
        "\n○ 국세수입 세부 내용\n∙ 소득세는 증가\n- 법인세는 감소"
    )
    assert result.chunks[0].head_content_ids == [11, 12, 13, 14, 15]


def test_sections_of_the_outline_bound_the_chunks() -> None:
    rows = [
        row(20, "○ 총수입 본문", header_id=3),
        row(21, "○ 총지출 본문", header_id=4),
    ]
    result = chunker.chunk(rows, HEADERS)

    assert [c.section_path for c in result.chunks] == [
        BREADCRUMB,
        "[재정 운용 동향 > 총지출]",
    ]
    assert result.warnings == []


def test_split_title_syllables_are_dropped() -> None:
    # The summary page's letter-spaced "요 약" arrives as one block per
    # syllable, filed under the summary header like its real content.
    rows = [
        row(30, "요", header_id=1, page_idx=3),
        row(31, "약", header_id=1, page_idx=3),
        row(32, "○ 재정 수지는 흑자", header_id=1, page_idx=3),
    ]
    result = chunker.chunk(rows, HEADERS)

    assert len(result.chunks) == 1
    assert result.chunks[0].text == "[요 약]\n○ 재정 수지는 흑자"
    assert result.chunks[0].head_content_ids == [32]


def bullet(prefix: str, n: int, width: int) -> str:
    """A body line of exactly ``width`` characters, so its token cost is known."""
    head = f"{prefix} 문장 {n} "
    return head + "가" * (width - len(head))


def test_a_circle_bullet_is_preferred_as_the_cut_point() -> None:
    # ○ with ∙/- children, twice over, 150 characters each. The second ○
    # arrives once the target is passed and everything would still fit under
    # the maximum, but the cut is made at the ○ so it keeps its children.
    rows = [
        row(40, bullet("○", 1, 150)),
        row(41, bullet("∙", 2, 150)),
        row(42, bullet("-", 3, 150)),
        row(43, bullet("∙", 4, 150)),
        row(44, bullet("○", 5, 150)),
        row(45, bullet("∙", 6, 150)),
        row(46, bullet("-", 7, 150)),
        row(47, bullet("∙", 8, 150)),
    ]
    result = chunker.chunk(rows, HEADERS)

    assert [c.head_content_ids for c in result.chunks] == [
        [40, 41, 42, 43],
        [44, 45, 46, 47],
    ]
    assert result.chunks[1].text.split("\n")[1].startswith("○")
