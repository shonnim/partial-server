"""Where each header starts and which header each block falls to.

Pure logic: headers are ORM objects never attached to a session, blocks are
the parser's dicts. The cases are the ways a document's hierarchy has gone
wrong in practice — above all a level-2 heading whose recorded page is off,
which used to file the heading's own text under its level-1 parent.
"""

from app.models.header import Header
from app.services.header_extraction import locate_heading
from app.services.sections import assign_headers, header_starts


def text(page: int, body: str, level: int | None = None) -> dict:
    block: dict = {"type": "text", "text": body, "page_idx": page}
    if level is not None:
        block["text_level"] = level
    return block


def test_level2_heading_is_not_filed_under_its_level1_parent() -> None:
    # The L2 heading sits on page 2 but was recorded as starting on page 3.
    # Trusting the recorded page would start the L2 at page 3's first block,
    # leaving its own heading text — and its page-2 body — under the L1.
    h1 = Header(id=1, header="I. 서론", level=1, start_page_idx=1)
    h2 = Header(id=2, header="1. 배경", level=2, parent_id=1, start_page_idx=3)
    blocks = [
        text(1, "I. 서론", level=1),
        text(1, "서론 본문"),
        text(2, "1. 배경", level=2),
        text(2, "배경 본문"),
        text(3, "다음 페이지 본문"),
    ]

    assert header_starts([h1, h2], blocks) == [0, 2]
    assert assign_headers([h1, h2], blocks) == [1, 1, 2, 2, 2]


def test_a_title_repeated_across_chapters_resolves_to_its_own_occurrence() -> None:
    # Both chapters open with "1. 개요". Located sequentially, the second
    # one's start is its own heading, not the first chapter's.
    a = Header(id=1, header="I. 경제 동향", level=1)
    b = Header(id=2, header="1. 개요", level=2, parent_id=1)
    c = Header(id=3, header="II. 재정 전망", level=1)
    d = Header(id=4, header="1. 개요", level=2, parent_id=3)
    blocks = [
        text(0, "I. 경제 동향", level=1),
        text(0, "1. 개요", level=2),
        text(0, "동향 본문"),
        text(1, "II. 재정 전망", level=1),
        text(1, "1. 개요", level=2),
        text(1, "전망 본문"),
    ]

    assert header_starts([a, b, c, d], blocks) == [0, 1, 3, 4]
    assert assign_headers([a, b, c, d], blocks) == [1, 2, 2, 3, 4, 4]


def test_english_inserted_mid_title_still_locates_the_heading() -> None:
    # From a real IMF Fiscal Monitor summary (page_idx 14): the contents page
    # lists "2. 공공 채무 대응 전략" but the body heading reads "2. 공공
    # 채무(Public Debt) 대응 전략". The English inserted mid-title defeats
    # plain startswith matching, and the page fallback then started the
    # section at "1. 서론"'s first body paragraph — all of 서론's body was
    # stored under "2. 공공 채무 대응 전략". The bare-key match (parenthesised
    # asides dropped on both sides) finds the real heading block.
    l1 = Header(id=1, header="II. 공공 재정 신뢰 강화", level=1, start_page_idx=14)
    s1 = Header(id=2, header="1. 서론", level=2, parent_id=1, start_page_idx=14)
    s2 = Header(
        id=3, header="2. 공공 채무 대응 전략", level=2, parent_id=1, start_page_idx=14
    )
    blocks = [
        text(14, "II. 공공 재정(Public Finances) 신뢰 강화", level=1),
        text(14, "1. 서론", level=1),
        text(14, "코로나19 위기로 정부의 재정수입이 감소하고 채무가 증가함에 따라 공공\n재정 신뢰 강화를 통한 재정확보가 요구됨"),
        text(14, "○ 2021년 말까지 선진국의 GDP 대비 채무 비율은 팬데믹 이전보다 18%p 높은 수준으로 예상되며…"),
        text(14, "2. 공공 채무(Public Debt) 대응 전략", level=1),
        text(14, "가. 재정 여력(Fiscal Space) 재건", level=1),
        text(14, "거시경제 측면에서 이상적인 채무 목표 및 경제 성장을 저해할 수 있는 채무 수준에 대한 명확한 임계값은 없지만…"),
    ]

    assert header_starts([l1, s1, s2], blocks) == [0, 1, 4]
    assert assign_headers([l1, s1, s2], blocks) == [1, 2, 2, 2, 3, 3, 3]


def test_locate_heading_forgives_a_parenthesis_kept_by_the_contents_page() -> None:
    # The reverse direction: the contents page carries the aside and the body
    # heading dropped it. The bare keys still agree.
    blocks = [text(3, "가. 재정 여력 재건", level=1)]

    located = locate_heading(blocks, "가. 재정 여력(Fiscal Space) 재건", None)

    assert located is not None
    assert (located.index, located.page) == (0, 3)


def test_blocks_before_the_first_header_stay_unassigned() -> None:
    h1 = Header(id=1, header="I. 서론", level=1, start_page_idx=1)
    blocks = [text(0, "표지"), text(1, "I. 서론", level=1), text(1, "본문")]

    assert assign_headers([h1], blocks) == [None, 1, 1]


def test_unlocatable_heading_falls_back_to_its_recorded_page() -> None:
    # The title cannot be found in the blocks, so the first block of the
    # recorded page — past the previous header's start — stands in.
    h1 = Header(id=1, header="I. 서론", level=1, start_page_idx=0)
    h2 = Header(id=2, header="2. 전혀 다른 제목", level=2, parent_id=1, start_page_idx=1)
    blocks = [
        text(0, "I. 서론", level=1),
        text(0, "본문"),
        text(1, "다른 모양의 제목"),
        text(1, "본문 둘"),
    ]

    assert header_starts([h1, h2], blocks) == [0, 2]
    assert assign_headers([h1, h2], blocks) == [1, 1, 2, 2]


def test_header_with_nothing_to_go_on_leaves_its_blocks_with_the_previous() -> None:
    # No recorded page and no locatable title: the header gets no start, and
    # its blocks stay with the header before it — documented behaviour.
    h1 = Header(id=1, header="I. 서론", level=1, start_page_idx=0)
    h2 = Header(id=2, header="찾을 수 없는 제목", level=2, parent_id=1, start_page_idx=None)
    blocks = [text(0, "I. 서론", level=1), text(0, "본문")]

    assert header_starts([h1, h2], blocks) == [0, None]
    assert assign_headers([h1, h2], blocks) == [1, 1]
