"""The summarizer's pure logic, with the model played by a fake.

No Postgres and no model: ``Summarizer`` takes the completion call and the
token counter the way the chunker takes its counter, so the split-and-merge
behaviour and the prompt plumbing are exercised directly.
"""

import pytest

from app.models.header import Header, HeaderContent
from app.services import summarizing
from app.services.summarizing import (
    SYSTEM_MERGE,
    SYSTEM_PARENT,
    SYSTEM_SECTION,
    Summarizer,
    block_text,
    subtree_blocks,
)


class FakeModel:
    """Answers every call with a numbered reply and keeps what it was asked."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def __call__(self, system: str, user: str) -> str:
        self.calls.append((system, user))
        return f"요약 {len(self.calls)}"


def summarizer_of(model: FakeModel) -> Summarizer:
    # Tokens counted as characters, so budgets in tests are character counts.
    return Summarizer(complete=model, count_tokens=len)


# ---- one unit ---------------------------------------------------------------------


def test_unit_within_budget_is_one_call() -> None:
    model = FakeModel()
    unit = summarizer_of(model).summarize_unit("1. 세계 채무 현황", ["본문 한 덩어리"])

    assert unit.text == "요약 1"
    assert unit.calls == 1
    system, user = model.calls[0]
    assert system == SYSTEM_SECTION
    assert "SECTION HEADING:\n1. 세계 채무 현황" in user
    assert "본문 한 덩어리" in user


def test_over_budget_unit_is_split_summarized_and_merged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(summarizing, "MAX_INPUT_TOKENS", 70)
    model = FakeModel()
    blocks = ["가" * 30, "나" * 30, "다" * 30]

    unit = summarizer_of(model).summarize_unit("제목", blocks)

    # Each block costs 31 (its joiner counted): two fit the budget of 70,
    # the third opens a second part, then the merge.
    part_calls = [c for c in model.calls if c[0] == SYSTEM_SECTION]
    merge_calls = [c for c in model.calls if c[0] == SYSTEM_MERGE]
    assert len(part_calls) == 2
    assert len(merge_calls) == 1
    assert unit.calls == 3
    assert unit.text == "요약 3"
    # The merge reads the part summaries, numbered and in order.
    assert "1. 요약 1" in merge_calls[0][1]
    assert "2. 요약 2" in merge_calls[0][1]


def test_single_block_past_the_whole_budget_is_cut_by_lines(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(summarizing, "MAX_INPUT_TOKENS", 50)
    model = FakeModel()
    block = "\n".join(["줄" * 20] * 3)  # one 122-char block of 40-char lines

    unit = summarizer_of(model).summarize_unit("제목", [block])

    assert unit.calls > 1  # it was split rather than sent whole
    assert all("줄" in user for system, user in model.calls if system == SYSTEM_SECTION)


# ---- the parent -------------------------------------------------------------------


def test_parent_reads_its_children_titles_and_summaries() -> None:
    model = FakeModel()
    text = summarizer_of(model).summarize_parent(
        "I. 채무의 현명한 관리",
        [("1. 세계 채무 현황", "첫째 요약"), ("2. 채무 수준", "둘째 요약")],
    )

    assert text == "요약 1"
    system, user = model.calls[0]
    assert system == SYSTEM_PARENT
    assert "CHAPTER HEADING:\nI. 채무의 현명한 관리" in user
    assert "- 1. 세계 채무 현황: 첫째 요약" in user
    assert "- 2. 채무 수준: 둘째 요약" in user


# ---- building the text ------------------------------------------------------------


def row(
    id: int,
    content: str | None,
    *,
    header_id: int | None = 2,
    type: str = "text",
    caption: str | None = None,
    footnote: str | None = None,
) -> HeaderContent:
    return HeaderContent(
        id=id, header_id=header_id, type=type, content=content,
        caption=caption, footnote=footnote,
    )


def test_block_text_frames_a_figure_with_caption_and_footnote() -> None:
    figure = row(1, "그림 요약 문단", type="image", caption="<그림 1> 성장률", footnote="자료: IMF")
    assert block_text(figure) == "<그림 1> 성장률\n그림 요약 문단\n자료: IMF"


def test_block_text_renders_a_table_as_markdown() -> None:
    html = (
        "<table><tr><td></td><td>2024</td></tr>"
        "<tr><td>한국</td><td>1.5</td></tr></table>"
    )
    text = block_text(row(1, html, type="table", caption="<표 1> 전망"))
    assert text is not None
    assert text.startswith("<표 1> 전망\n")
    assert "| 한국 | 1.5 |" in text
    assert "<table>" not in text


def test_block_text_of_an_empty_row_is_none() -> None:
    assert block_text(row(1, "   ")) is None


def test_subtree_blocks_fold_in_descendants_under_their_headings() -> None:
    h2 = Header(id=2, header="1. 세계 채무 현황", parent_id=1, level=2)
    h3 = Header(id=3, header="가. 선진국", parent_id=2, level=3)
    children = {1: [h2], 2: [h3]}
    contents = {
        2: [row(10, "상위 본문")],
        3: [row(11, "하위 본문", header_id=3)],
    }

    assert subtree_blocks(h2, children, contents) == [
        "상위 본문",
        "가. 선진국",
        "하위 본문",
    ]
