"""The summarizer's pure logic, with the model played by a fake.

No Postgres and no model: ``Summarizer`` takes the completion call and the
token counter the way the chunker takes its counter, so the split-and-merge
behaviour and the prompt plumbing are exercised directly.
"""

import uuid

import pytest

from app.llm import LLMError, TokenUsage
from app.models.document import Document
from app.models.header import Header, HeaderContent
from app.services import summarizing
from app.services.summarizing import (
    SYSTEM_DOCUMENT,
    SYSTEM_MERGE,
    SYSTEM_PARENT,
    SYSTEM_SECTION,
    Summarizer,
    _header_summary,
    block_text,
    subtree_blocks,
)


class FakeModel:
    """Answers every call with a numbered reply and keeps what it was asked."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def __call__(self, system: str, user: str) -> tuple[str, TokenUsage]:
        self.calls.append((system, user))
        # A fixed usage per call, so tally tests can count calls off it.
        return f"요약 {len(self.calls)}", TokenUsage(prompt_tokens=100, completion_tokens=10)


def summarizer_of(model: FakeModel) -> Summarizer:
    # Tokens counted as characters, so budgets in tests are character counts.
    return Summarizer(complete=model, count_tokens=len)


# ---- the model picker ---------------------------------------------------------------


def test_resolve_llm_falls_back_to_the_default() -> None:
    assert summarizing.resolve_llm("gemma") == "gemma"
    assert summarizing.resolve_llm("solar") == "solar"
    assert summarizing.resolve_llm("gpt-5") == summarizing.DEFAULT_LLM
    assert summarizing.resolve_llm(None) == summarizing.DEFAULT_LLM
    assert summarizing.resolve_llm("") == summarizing.DEFAULT_LLM


def test_make_summarizer_binds_the_picked_model() -> None:
    assert summarizing.make_summarizer("gemma").complete is summarizing._chat
    assert summarizing.make_summarizer("solar").complete is summarizing._chat_solar
    # An unknown value never picks a model by accident.
    assert summarizing.make_summarizer("anything").complete is summarizing._chat


def test_summarizer_tallies_the_usage_of_every_call() -> None:
    model = FakeModel()
    summarizer = summarizer_of(model)
    summarizer.summarize_unit("1. 세계 채무 현황", ["본문 한 덩어리"])
    summarizer.summarize_parent("I. 부", [("1. 장", "요약")])

    assert len(model.calls) == 2
    assert summarizer.prompt_tokens == 200
    assert summarizer.completion_tokens == 20


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


# ---- the whole document -----------------------------------------------------------


def test_overall_reads_the_chapter_summaries_in_order() -> None:
    model = FakeModel()
    text = summarizer_of(model).summarize_overall(
        "IMF 재정점검보고서.pdf",
        [("I. 채무의 현명한 관리", "1장 요약"), ("II. 재정 전망", "2장 요약")],
    )

    assert text == "요약 1"
    system, user = model.calls[0]
    assert system == SYSTEM_DOCUMENT
    assert "DOCUMENT TITLE:\nIMF 재정점검보고서.pdf" in user
    assert "1. I. 채무의 현명한 관리: 1장 요약" in user
    assert "2. II. 재정 전망: 2장 요약" in user


# ---- one header, as the pop-up boxes run it ----------------------------------------


def test_header_summary_of_a_level2_reads_its_contents() -> None:
    model = FakeModel()
    h2 = Header(id=2, header="1. 세계 채무 현황", parent_id=1, level=2)
    contents = {2: [HeaderContent(id=10, header_id=2, type="text", content="본문")]}

    text, calls = _header_summary(h2, {}, contents, summarizer_of(model))

    assert (text, calls) == ("요약 1", 1)
    assert model.calls[0][0] == SYSTEM_SECTION


def test_header_summary_of_a_level1_reads_its_children_summaries() -> None:
    model = FakeModel()
    h1 = Header(id=1, header="I. 부", level=1)
    h2 = Header(id=2, header="1. 장", parent_id=1, level=2, conclusion="자식 요약")

    text, calls = _header_summary(h1, {1: [h2]}, {}, summarizer_of(model))

    assert (text, calls) == ("요약 1", 1)
    system, user = model.calls[0]
    assert system == SYSTEM_PARENT
    assert "- 1. 장: 자식 요약" in user


def test_header_summary_of_a_level1_without_child_summaries_falls_back() -> None:
    # The batch run's shape: a level-1 header with nothing summarised under
    # it is treated like a section, from its own content rows.
    model = FakeModel()
    h1 = Header(id=1, header="I. 부", level=1)
    h2 = Header(id=2, header="1. 장", parent_id=1, level=2, conclusion=None)
    contents = {1: [HeaderContent(id=10, header_id=1, type="text", content="본문")]}

    _header_summary(h1, {1: [h2]}, contents, summarizer_of(model))

    # Its own contents (and the unsummarised child's heading) go through the
    # section prompt, not the parent prompt.
    system, user = model.calls[0]
    assert system == SYSTEM_SECTION
    assert "본문" in user


def test_strict_level1_without_child_summaries_refuses_the_fallback() -> None:
    # The pop-up box's shape: the L1 button promises the children's
    # conclusions as the source, so missing ones are an error, never a
    # quiet re-read of the raw contents.
    model = FakeModel()
    h1 = Header(id=1, header="I. 부", level=1)
    h2 = Header(id=2, header="1. 장", parent_id=1, level=2, conclusion=None)
    contents = {1: [HeaderContent(id=10, header_id=1, type="text", content="본문")]}

    with pytest.raises(LLMError, match="level-2"):
        _header_summary(h1, {1: [h2]}, contents, summarizer_of(model), strict=True)
    assert model.calls == []


def test_strict_level2_still_reads_its_contents() -> None:
    model = FakeModel()
    h2 = Header(id=2, header="1. 장", parent_id=1, level=2)
    contents = {2: [HeaderContent(id=10, header_id=2, type="text", content="본문")]}

    text, calls = _header_summary(h2, {}, contents, summarizer_of(model), strict=True)

    assert (text, calls) == ("요약 1", 1)
    assert model.calls[0][0] == SYSTEM_SECTION


def test_header_summary_without_contents_raises() -> None:
    h2 = Header(id=2, header="1. 장", parent_id=1, level=2)
    with pytest.raises(LLMError):
        _header_summary(h2, {}, {}, summarizer_of(FakeModel()))


# ---- the document box's strict source -----------------------------------------------


def test_strict_doc_summary_refuses_the_level2_fallback() -> None:
    # Level-2 summaries exist but no level-1 ones: the batch path would fall
    # back to them, the document box must not. The error path touches no db,
    # so None stands in for the session.
    doc = Document(filename="보고서.pdf", summary=None)
    h2 = Header(id=2, header="1. 장", parent_id=None, level=2, conclusion="요약 있음")
    model = FakeModel()
    result = summarizing.SummarizeResult(id=uuid.uuid4(), filename=doc.filename)

    summarizing._write_doc_summary(
        None, doc, summarizer_of(model), [], [h2], result, force=True, strict=True
    )

    assert result.failed == 1
    assert "level-1" in result.errors[0]
    assert model.calls == []
    assert doc.summary is None


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
