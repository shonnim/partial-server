"""The summarizer's pure logic, with the model played by a fake.

No Postgres and no model: ``Summarizer`` takes the completion call and the
token counter the way the chunker takes its counter, so the split-and-merge
behaviour and the prompt plumbing are exercised directly.
"""

import threading
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
    summarize_document,
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


def test_resolve_workers_offers_1_4_and_8_for_the_local_model() -> None:
    assert summarizing.resolve_workers(1) == 1
    assert summarizing.resolve_workers(4) == 4
    assert summarizing.resolve_workers(8, "gemma") == 8
    # Anything the picker does not offer means the default.
    assert summarizing.resolve_workers(2) == summarizing.DEFAULT_WORKERS
    assert summarizing.resolve_workers(0) == summarizing.DEFAULT_WORKERS
    assert summarizing.resolve_workers(None) == summarizing.DEFAULT_WORKERS
    # Solar is an external API: one by one whatever the picker says.
    assert summarizing.resolve_workers(8, "solar") == summarizing.DEFAULT_WORKERS


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


# ---- the pre-run content check ------------------------------------------------------


def test_empty_check_names_the_headers_without_content() -> None:
    # Two sections under a chapter; only the first has content rows. The run
    # could only fail on the second, so the check names it — and only it: the
    # chapter is summarised from its children's conclusions, never checked.
    l1 = Header(id=1, header="I. 공공 재정", level=1)
    s1 = Header(id=2, header="1. 서론", level=2, parent_id=1)
    s2 = Header(id=3, header="2. 대응 전략", level=2, parent_id=1)
    children = {None: [l1], 1: [s1, s2]}

    missing = summarizing._empty_summary_headers([l1, s1, s2], children, {2})

    assert missing == ["2. 대응 전략"]


def test_empty_check_reads_content_through_descendants() -> None:
    # The level-2 header's rows all sit under its level-3 child, the way
    # subtree_blocks reads them: the subtree has content, nothing is flagged.
    s2 = Header(id=2, header="1. 서론", level=2, parent_id=1)
    s3 = Header(id=3, header="가. 배경", level=3, parent_id=2)
    children = {2: [s3]}

    assert summarizing._empty_summary_headers([s2, s3], children, {3}) == []


def test_empty_check_flags_a_childless_level1_without_content() -> None:
    # A level-1 header without level-2 children is summarised from its own
    # rows, so it is checked like a section; with children it is not.
    alone = Header(id=1, header="부록", level=1)
    parent = Header(id=2, header="I. 본문", level=1)
    child = Header(id=3, header="1. 장", level=2, parent_id=2)
    children = {2: [child]}

    missing = summarizing._empty_summary_headers([alone, parent, child], children, {3})

    assert missing == ["부록"]


def test_empty_headers_reason_names_at_most_three() -> None:
    assert summarizing._empty_headers_reason(["1. 서론"]) == (
        "header has no content: '1. 서론' — run Section first"
    )
    assert summarizing._empty_headers_reason(["가", "나", "다", "라", "마"]) == (
        "5 headers have no content: '가', '나', '다' and 2 more — run Section first"
    )


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


# ---- the whole-document run and its workers ------------------------------------------


class OutlineDb:
    """Enough of a Session for ``summarize_document``: the two outline reads,
    then per-conclusion commits. Nothing is expired on commit, so conclusions
    written earlier in the run stay readable."""

    def __init__(self, headers: list[Header], contents: list[HeaderContent]) -> None:
        self._reads: list[list] = [headers, contents]
        self.commits = 0

    def scalars(self, stmt):
        rows = self._reads.pop(0)

        class Result:
            def all(self) -> list:
                return rows

        return Result()

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        pass


def _outline() -> tuple[Document, list[Header], list[HeaderContent]]:
    """One level-1 chapter with four level-2 sections, each with a text row."""
    doc = Document(id=uuid.uuid4(), filename="보고서.pdf", summary=None)
    parent = Header(id=1, header="I. 국제기구", level=1, parent_id=None)
    sections = [
        Header(id=i, header=f"{i - 1}. 절", level=2, parent_id=1) for i in range(2, 6)
    ]
    contents = [
        HeaderContent(id=10 + i, header_id=h.id, type="text", content=f"본문 {h.id}")
        for i, h in enumerate(sections)
    ]
    return doc, [parent, *sections], contents


def test_document_run_writes_level2_then_level1_then_the_doc_summary() -> None:
    doc, headers, contents = _outline()
    model = FakeModel()

    result = summarize_document(
        OutlineDb(headers, contents), doc, summarizer=summarizer_of(model)
    )

    assert result.ok
    assert result.summarized == 5 and result.failed == 0
    assert result.doc_summarized and result.has_summary
    assert result.calls == 6  # four sections, the chapter, the document
    # Sections first, then the chapter from their conclusions, then the doc.
    assert [system for system, _ in model.calls] == [
        *[SYSTEM_SECTION] * 4,
        SYSTEM_PARENT,
        SYSTEM_DOCUMENT,
    ]
    assert all(h.conclusion for h in headers)
    assert doc.summary


def test_workers_run_a_level_of_model_calls_in_parallel() -> None:
    # Four section calls under 4 workers: the barrier opens only when all
    # four are in flight together, so a sequential run would break it (and
    # fail the test) rather than pass one by one.
    doc, headers, contents = _outline()
    barrier = threading.Barrier(4, timeout=10)
    lock = threading.Lock()
    calls: list[str] = []

    def model(system: str, user: str) -> tuple[str, TokenUsage]:
        if system == SYSTEM_SECTION:
            barrier.wait()
        with lock:
            calls.append(system)
        return "요약", TokenUsage(prompt_tokens=100, completion_tokens=10)

    result = summarize_document(
        OutlineDb(headers, contents),
        doc,
        summarizer=Summarizer(complete=model, count_tokens=len),
        workers=4,
    )

    assert result.ok and result.summarized == 5 and result.doc_summarized
    # The parent still runs after every section: it reads their conclusions.
    assert calls[-2:] == [SYSTEM_PARENT, SYSTEM_DOCUMENT]
    assert all(h.conclusion == "요약" for h in headers)


# ---- group mode: the general summary from the group's own source ---------------------


def test_resolve_group_knows_only_registered_modes() -> None:
    assert summarizing.resolve_group("oft-1") == "oft-1"
    assert summarizing.resolve_group("") is None
    assert summarizing.resolve_group(None) is None
    assert summarizing.resolve_group("bpa-1") is None  # no summary strategy


def _oft_outline() -> tuple[Document, list[Header], list[HeaderContent]]:
    """An oft-1 document: the 요약 section and one country section."""
    doc = Document(id=uuid.uuid4(), filename="재정동향.pdf", summary=None)
    summary = Header(id=1, header="요 약", level=1, parent_id=None)
    country = Header(id=2, header="I. 국제기구", level=1, parent_id=None)
    contents = [
        HeaderContent(id=10, header_id=1, type="text", content="요 약"),  # the heading row
        HeaderContent(id=11, header_id=1, type="text", content="요"),  # split syllables
        HeaderContent(id=12, header_id=1, type="text", content="○ 재정수지는 흑자"),
        HeaderContent(id=13, header_id=1, type="text", content="∙ 세입이 증가"),
        HeaderContent(id=14, header_id=2, type="text", content="국제기구 본문"),
    ]
    return doc, [summary, country], contents


def test_oft_group_mode_takes_the_요약_page_as_the_general_summary() -> None:
    doc, headers, contents = _oft_outline()
    model = FakeModel()

    result = summarize_document(
        OutlineDb(headers, contents), doc, summarizer=summarizer_of(model), group="oft-1"
    )

    # The 요약 page's own text, verbatim: heading row and syllables dropped,
    # no SYSTEM_DOCUMENT call spent on it.
    assert doc.summary == "○ 재정수지는 흑자\n\n∙ 세입이 증가"
    assert result.doc_summarized and result.has_summary
    assert SYSTEM_DOCUMENT not in [system for system, _ in model.calls]
    # The headers themselves are still summarised the default way.
    assert result.summarized == 2
    assert all(h.conclusion for h in headers)


def test_oft_group_mode_without_a_요약_header_fails_the_doc_summary() -> None:
    doc = Document(id=uuid.uuid4(), filename="재정동향.pdf", summary=None)
    headers = [Header(id=2, header="I. 국제기구", level=1, parent_id=None)]
    contents = [HeaderContent(id=14, header_id=2, type="text", content="본문")]
    model = FakeModel()

    result = summarize_document(
        OutlineDb(headers, contents), doc, summarizer=summarizer_of(model), group="oft-1"
    )

    assert not result.doc_summarized
    assert doc.summary is None
    assert any("요약" in e for e in result.errors)


def test_general_run_in_group_mode_needs_no_header_conclusions() -> None:
    # The default general run reads conclusions; the group mode reads the
    # 요약 rows, so a document with none summarised still gets its summary.
    doc, headers, contents = _oft_outline()
    model = FakeModel()

    result = summarizing.summarize_document_general(
        OutlineDb(headers, contents), doc, summarizer=summarizer_of(model), group="oft-1"
    )

    assert doc.summary == "○ 재정수지는 흑자\n\n∙ 세입이 증가"
    assert result.doc_summarized
    assert model.calls == []  # no model call at all


def test_a_failed_header_does_not_stop_a_parallel_run() -> None:
    doc, headers, contents = _outline()

    def model(system: str, user: str) -> tuple[str, TokenUsage]:
        if "본문 2" in user:
            raise LLMError("the model did not answer")
        return "요약", TokenUsage(prompt_tokens=100, completion_tokens=10)

    result = summarize_document(
        OutlineDb(headers, contents),
        doc,
        summarizer=Summarizer(complete=model, count_tokens=len),
        workers=4,
    )

    assert not result.ok
    assert result.failed == 1
    assert result.summarized == 4  # three sections, the chapter
    assert any("1. 절" in e for e in result.errors)
