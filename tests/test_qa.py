"""The question-answering pipeline, with the models played by fakes."""

import asyncio
import json
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.db import get_db
from app.llm import LLMError
from app.main import app
from app.models.chunk import Chunk
from app.routers.client import _answer_stream
from app.models.document import Document
from app.models.header import Header, HeaderContent
from app.services.qa import (
    NOT_FOUND_MESSAGE,
    Classification,
    QASource,
    ScanAnswer,
    ScanBatch,
    StoredSummary,
    chunk_answers,
    classify_query,
    classify_summary_scope,
    find_stored_summary,
    generate_answer,
    generate_summary_answer,
    identify_sources,
    last_printed_page,
    pick_source_ids,
    printed_page,
    query_pages_exist,
    requested_pages,
    scan_batch_for_answer,
    scan_batches,
    suggest_questions,
)

DOC_ID = uuid.uuid4()


# ---- classification ---------------------------------------------------------------


def test_classify_returns_type_and_rewritten_query() -> None:
    def fake(**kwargs: Any) -> dict:
        assert kwargs["schema_name"] == "query_classification"
        return {"query_type": "question", "search_query": "2024년 국가채무 규모"}

    got = classify_query("국가채무가 얼마나 되나요?", complete_json=fake)

    assert got.query_type == "question"
    assert got.search_query == "2024년 국가채무 규모"


def test_classify_falls_back_to_the_query_when_the_rewrite_is_blank() -> None:
    def fake(**kwargs: Any) -> dict:
        return {"query_type": "summary", "search_query": "   "}

    got = classify_query("요약해 주세요", complete_json=fake)
    assert got.search_query == "요약해 주세요"


def test_classify_rejects_an_unknown_type() -> None:
    def fake(**kwargs: Any) -> dict:
        return {"query_type": "poetry", "search_query": "x"}

    with pytest.raises(LLMError):
        classify_query("질문", complete_json=fake)


# ---- summary routing --------------------------------------------------------------


def make_header(hid: int, header: str, *, level: int = 1, conclusion: str | None = None) -> Header:
    return Header(id=hid, doc_id=DOC_ID, header=header, level=level, conclusion=conclusion)


def test_classify_summary_scope_lists_the_outline_and_returns_the_pick() -> None:
    seen: dict[str, Any] = {}

    def fake(**kwargs: Any) -> dict:
        seen.update(kwargs)
        return {"scope": "section", "header_id": 2}

    got = classify_summary_scope(
        "서론을 요약해줘",
        [(1, 1, "Ⅰ. 서론"), (2, 2, "1. 배경")],
        complete_json=fake,
    )

    assert seen["schema_name"] == "summary_scope"
    assert "서론을 요약해줘" in seen["user"]
    assert "[id 1] Ⅰ. 서론" in seen["user"]
    assert "[id 2]   1. 배경" in seen["user"]
    assert got.scope == "section"
    assert got.header_id == 2


def test_classify_summary_scope_drops_an_unknown_header_id() -> None:
    def fake(**kwargs: Any) -> dict:
        return {"scope": "section", "header_id": 99}

    got = classify_summary_scope("서론 요약", [(1, 1, "서론")], complete_json=fake)
    assert got.header_id is None


def test_classify_summary_scope_rejects_an_unknown_scope() -> None:
    def fake(**kwargs: Any) -> dict:
        return {"scope": "paragraph", "header_id": None}

    with pytest.raises(LLMError):
        classify_summary_scope("요약해줘", [], complete_json=fake)


def test_find_stored_summary_serves_the_document_summary() -> None:
    doc = make_document()
    doc.summary = "  **전체** 요약임  "

    got = find_stored_summary(
        StubDb(doc),
        doc,
        "이 보고서 요약해줘",
        complete_json=lambda **k: {"scope": "document", "header_id": None},
        load_headers=lambda db, doc_id: [make_header(1, "서론", conclusion="서론 요약임")],
    )

    assert got == StoredSummary(text="**전체** 요약임", section=None)


def test_find_stored_summary_serves_the_sections_conclusion() -> None:
    doc = make_document()
    headers = [
        make_header(1, "Ⅰ. 서론", conclusion="서론 요약임"),
        make_header(2, " Ⅱ. 재정전망 ", level=1, conclusion="  **재정** 전망 요약임  "),
    ]

    got = find_stored_summary(
        StubDb(doc),
        doc,
        "재정전망 부분 요약해줘",
        complete_json=lambda **k: {"scope": "section", "header_id": 2},
        load_headers=lambda db, doc_id: headers,
    )

    assert got == StoredSummary(text="**재정** 전망 요약임", section="Ⅱ. 재정전망")


def test_find_stored_summary_without_anything_stored_asks_no_model() -> None:
    def explode(**kwargs: Any) -> dict:
        raise AssertionError("the model must not be called")

    got = find_stored_summary(
        StubDb(make_document()),
        make_document(),
        "요약해줘",
        complete_json=explode,
        load_headers=lambda db, doc_id: [],
    )
    assert got is None


def test_find_stored_summary_falls_through_when_nothing_matches() -> None:
    doc = make_document()
    doc.summary = "전체 요약임"
    headers = [make_header(1, "서론", conclusion=None)]

    # The scope resolves to a section, but no listed header matches.
    assert (
        find_stored_summary(
            doc_stub := StubDb(doc),
            doc,
            "결론 요약해줘",
            complete_json=lambda **k: {"scope": "section", "header_id": None},
            load_headers=lambda db, doc_id: headers,
        )
        is None
    )
    # The matched header carries no conclusion yet.
    assert (
        find_stored_summary(
            doc_stub,
            doc,
            "서론 요약해줘",
            complete_json=lambda **k: {"scope": "section", "header_id": 1},
            load_headers=lambda db, doc_id: headers,
        )
        is None
    )
    # The whole document is asked for, but its summary is not written yet.
    doc.summary = None
    assert (
        find_stored_summary(
            doc_stub,
            doc,
            "요약해줘",
            complete_json=lambda **k: {"scope": "document", "header_id": None},
            load_headers=lambda db, doc_id: headers,
        )
        is None
    )


def test_generate_summary_answer_carries_query_heading_and_summary() -> None:
    def fake(**kwargs: Any) -> str:
        assert "서론을 요약해줘" in kwargs["user"]
        assert "SECTION HEADING:\nⅠ. 서론" in kwargs["user"]
        assert "서론 요약임" in kwargs["user"]
        return "  서론을 요약하면 다음과 같습니다.  "

    got = generate_summary_answer(
        "서론을 요약해줘", "서론 요약임", section="Ⅰ. 서론", complete=fake
    )
    assert got == "서론을 요약하면 다음과 같습니다."


def test_generate_summary_answer_for_the_document_names_no_section() -> None:
    def fake(**kwargs: Any) -> str:
        assert "SECTION HEADING" not in kwargs["user"]
        return "답변"

    assert generate_summary_answer("요약해줘", "전체 요약임", complete=fake) == "답변"


# ---- the answer -------------------------------------------------------------------


def test_each_query_type_gets_its_own_prompt() -> None:
    systems: list[str] = []

    def fake(**kwargs: Any) -> str:
        systems.append(kwargs["system"])
        return "답변"

    for query_type in ("question", "summary", "analysis"):
        assert generate_answer("질문", "본문", query_type, complete=fake) == "답변"

    assert len(set(systems)) == 3


def test_the_answer_prompt_carries_query_and_passage() -> None:
    def fake(**kwargs: Any) -> str:
        assert "세출은 얼마나 늘었나요?" in kwargs["user"]
        assert "세출은 10% 늘었다" in kwargs["user"]
        return "  **10%** 늘었습니다.  "

    got = generate_answer(
        "세출은 얼마나 늘었나요?", "세출은 10% 늘었다", "question", complete=fake
    )
    assert got == "**10%** 늘었습니다."


def test_an_unanswerable_type_has_no_prompt() -> None:
    with pytest.raises(LLMError):
        generate_answer("질문", "본문", "unclear", complete=lambda **k: "x")


# ---- the sources ------------------------------------------------------------------


def test_pick_source_ids_keeps_only_known_ids_in_the_models_order() -> None:
    def fake(**kwargs: Any) -> dict:
        return {"source_ids": [2, 99, 1, 2]}

    got = pick_source_ids("답변", [(1, "본문 하나"), (2, "본문 둘")], complete_json=fake)
    assert got == [2, 1]


def test_printed_page_reads_the_pages_number_block() -> None:
    blocks = [
        {"type": "text", "page_idx": 4, "text": "본문 7쪽"},
        {"type": "page_number", "page_idx": 4, "text": "- 12 -"},
        {"type": "page_number", "page_idx": 5, "text": "13 14"},  # a two-up spread
    ]
    assert printed_page(blocks, 4) == 12
    assert printed_page(blocks, 5) == 13
    assert printed_page(blocks, 6) is None
    assert printed_page(blocks, None) is None
    assert printed_page(None, 4) is None


class StubScalars:
    def __init__(self, rows: list) -> None:
        self.rows = rows

    def all(self) -> list:
        return self.rows


class StubDb:
    """Stands in for the Session: ``get`` by primary key and one rows query."""

    def __init__(self, doc: Document | None, rows: list | None = None) -> None:
        self.doc = doc
        self.rows = rows or []

    def get(self, model: type, pk: uuid.UUID) -> Document | None:
        if self.doc is not None and model is Document and pk == self.doc.id:
            return self.doc
        return None

    def scalars(self, statement: object) -> StubScalars:
        return StubScalars(self.rows)


def make_document() -> Document:
    return Document(
        id=DOC_ID,
        bucket="documents",
        s3_key=f"documents/{DOC_ID}.pdf",
        filename="2025 재정전망.pdf",
        size_bytes=1024,
        content_type="application/pdf",
        pages=34,
    )


def make_chunk(head_content_ids: list[int]) -> Chunk:
    return Chunk(
        id=7,
        doc_id=DOC_ID,
        group_id="g1",
        emb_text="[경로] 본문",
        text="본문",
        head_content_ids=head_content_ids,
    )


def test_identify_sources_returns_page_text_and_bbox() -> None:
    rows = [
        HeaderContent(
            id=1, doc_id=DOC_ID, page_idx=4, type="text", content="부채가 늘었다",
            bbox_0=10, bbox_1=20, bbox_2=300, bbox_3=40,
        ),
        HeaderContent(
            id=2, doc_id=DOC_ID, page_idx=5, type="text", content="세출이 줄었다",
            bbox_0=11, bbox_1=21, bbox_2=301, bbox_3=41,
        ),
    ]
    blocks = [{"type": "page_number", "page_idx": 4, "text": "12"}]

    got = identify_sources(
        StubDb(make_document(), rows),
        make_document(),
        answer="부채가 늘었습니다",
        chunk=make_chunk([1, 2]),
        complete_json=lambda **k: {"source_ids": [1]},
        load_blocks=lambda db, doc: (blocks, None),
    )

    assert len(got) == 1
    assert got[0].page_idx == 4
    assert got[0].page == 12
    assert got[0].text == "부채가 늘었다"
    assert got[0].bbox == (10, 20, 300, 40)


def test_identify_sources_without_candidates_asks_no_model() -> None:
    def explode(**kwargs: Any) -> dict:
        raise AssertionError("the model must not be called")

    got = identify_sources(
        StubDb(make_document(), []),
        make_document(),
        answer="답변",
        chunk=make_chunk([]),
        complete_json=explode,
        load_blocks=lambda db, doc: (None, "no artifact"),
    )
    assert got == []


def test_identify_sources_survives_a_missing_content_list() -> None:
    rows = [
        HeaderContent(id=1, doc_id=DOC_ID, page_idx=4, type="text", content="본문")
    ]

    got = identify_sources(
        StubDb(make_document(), rows),
        make_document(),
        answer="답변",
        chunk=make_chunk([1]),
        complete_json=lambda **k: {"source_ids": [1]},
        load_blocks=lambda db, doc: (None, "content list missing from storage"),
    )

    assert got[0].page_idx == 4
    assert got[0].page is None


# ---- the header scan (test-only "headers" mode) -------------------------------------


def make_content(cid: int, header_id: int, content: str) -> HeaderContent:
    return HeaderContent(
        id=cid, doc_id=DOC_ID, header_id=header_id, page_idx=4, type="text",
        content=content, bbox_0=10, bbox_1=20, bbox_2=300, bbox_3=40,
    )


def test_scan_batches_walks_headers_in_order_and_splits_a_long_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.services.qa.MAX_SCAN_CHARS", 10)
    headers = [make_header(1, "서론"), make_header(2, "본론")]
    rows = [
        make_content(11, 1, "aaaa"),
        make_content(12, 1, "bbbb"),
        make_content(13, 1, "cccc"),  # pushes header 1 over 10 chars
        make_content(14, 2, "x" * 99),  # alone over the limit: its own batch
        HeaderContent(id=15, doc_id=DOC_ID, header_id=2, type="text", content="  "),
    ]

    got = scan_batches(StubDb(None, rows), DOC_ID, load_headers=lambda db, d: headers)

    assert [(b.heading, [r.id for r in b.rows]) for b in got] == [
        ("서론", [11, 12]),
        ("서론", [13]),
        ("본론", [14]),
    ]


def test_scan_batches_without_headers_reads_no_rows() -> None:
    def explode(statement: object) -> StubScalars:
        raise AssertionError("no rows query without headers")

    db = StubDb(None)
    db.scalars = explode  # type: ignore[method-assign]
    assert scan_batches(db, DOC_ID, load_headers=lambda d, i: []) == []


def test_scan_batch_for_answer_returns_the_answer_and_known_rows() -> None:
    batch = ScanBatch(
        header_id=1,
        heading="서론",
        rows=[make_content(11, 1, "국가채무는 2024년에 100조 원 늘어 국내총생산 대비 50%에 도달하였다")],
    )
    seen: dict[str, Any] = {}

    def fake(**kwargs: Any) -> dict:
        seen.update(kwargs)
        return {"found": True, "answer": " 부채가 늘었습니다 ", "source_ids": [11, 11, 99]}

    got = scan_batch_for_answer("부채는?", batch, complete_json=fake)

    assert seen["schema_name"] == "header_scan"
    assert "서론" in seen["user"] and "[id 11]" in seen["user"]
    assert got == ScanAnswer(answer="부채가 늘었습니다", rows=[batch.rows[0]])


def test_scan_batch_for_answer_drops_a_label_only_source() -> None:
    main = make_content(
        11, 1, "민간 소비는 2024년에 연 2.8% 성장하여 2000~2019년 평균치인 2.4%를 상회하였다"
    )
    label = make_content(12, 1, "■ (통화정책 가정)")
    batch = ScanBatch(header_id=1, heading="본론", rows=[label, main])

    got = scan_batch_for_answer(
        "소비는?",
        batch,
        complete_json=lambda **k: {
            "found": True,
            "answer": "연 2.8% 성장하였습니다",
            "source_ids": [12, 11],
        },
    )

    # The label block the model named is dropped; the substantial one stays.
    assert got is not None
    assert [r.id for r in got.rows] == [11]


def test_scan_batch_without_the_answer_returns_none() -> None:
    batch = ScanBatch(header_id=1, heading="서론", rows=[make_content(11, 1, "본문")])

    fake_not_found = lambda **k: {"found": False, "answer": "", "source_ids": []}  # noqa: E731
    assert scan_batch_for_answer("질문", batch, complete_json=fake_not_found) is None

    # "found" without an answer counts as not found.
    fake_empty = lambda **k: {"found": True, "answer": "  ", "source_ids": [11]}  # noqa: E731
    assert scan_batch_for_answer("질문", batch, complete_json=fake_empty) is None

    with pytest.raises(LLMError):
        scan_batch_for_answer(
            "질문", batch, complete_json=lambda **k: {"found": "yes", "answer": "", "source_ids": []}
        )


# ---- suggested questions ----------------------------------------------------------


def test_suggest_questions_reads_the_chunks_and_cleans_the_reply() -> None:
    chunks = [make_chunk([]), make_chunk([])]
    seen: dict[str, str] = {}

    def fake(**kwargs: Any) -> dict:
        seen["user"] = kwargs["user"]
        return {"questions": ["  세출은 얼마나 늘었나요?  ", "세출은 얼마나 늘었나요?", "", "부채 원인은 무엇인가요?"]}

    got = suggest_questions(StubDb(None, chunks), DOC_ID, complete_json=fake)

    # The chunk text reaches the prompt; blanks and repeats are dropped.
    assert "본문" in seen["user"]
    assert got == ["세출은 얼마나 늘었나요?", "부채 원인은 무엇인가요?"]


def test_suggest_questions_without_chunks_asks_no_model() -> None:
    def explode(**kwargs: Any) -> dict:
        raise AssertionError("the model must not be called")

    assert suggest_questions(StubDb(None, []), DOC_ID, complete_json=explode) == []


def test_a_failed_suggestion_call_costs_only_the_box() -> None:
    def fail(**kwargs: Any) -> dict:
        raise LLMError("model down")

    assert suggest_questions(StubDb(None, [make_chunk([])]), DOC_ID, complete_json=fail) == []


# ---- page validation --------------------------------------------------------------


def test_requested_pages_reads_korean_and_latin_forms() -> None:
    assert requested_pages("99페이지의 2022년 수치를 설명해줘") == [99]
    assert requested_pages("12쪽과 page 12를 비교해줘") == [12]
    assert requested_pages("3 페이지와 p.7의 내용은?") == [3, 7]
    assert requested_pages("세출 추이가 궁금해요") == []


def test_last_printed_page_reads_the_last_numbered_block() -> None:
    blocks = [
        {"type": "text", "text": "본문 2024"},
        {"type": "page_number", "text": "- 97 -"},
        # A two-up parse prints the spread's both numbers on one parse page.
        {"type": "page_number", "text": "98 99"},
        # A trailing block without digits does not hide the numbered ones.
        {"type": "page_number", "text": "끝"},
    ]
    assert last_printed_page(blocks) == 99
    assert last_printed_page(None) is None
    assert last_printed_page([{"type": "text", "text": "1"}]) is None


def test_a_page_beyond_the_document_fails_validation() -> None:
    blocks = [{"type": "page_number", "text": "99"}]
    fake = lambda db, doc: (blocks, None)  # noqa: E731

    assert not query_pages_exist(
        StubDb(None), make_document(), "100페이지를 요약해줘", load_blocks=fake
    )
    assert query_pages_exist(
        StubDb(None), make_document(), "99페이지를 요약해줘", load_blocks=fake
    )


def test_a_query_without_a_page_skips_the_content_list() -> None:
    def explode(db: Any, doc: Any) -> tuple[Any, str | None]:
        raise AssertionError("the content list must not be loaded")

    assert query_pages_exist(
        StubDb(None), make_document(), "세출 추이를 분석해줘", load_blocks=explode
    )


def test_a_missing_content_list_does_not_refuse_the_query() -> None:
    fake = lambda db, doc: (None, "content list missing from storage")  # noqa: E731

    assert query_pages_exist(
        StubDb(None), make_document(), "500페이지 내용 알려줘", load_blocks=fake
    )


# ---- chunk verification -----------------------------------------------------------


def test_chunk_answers_returns_the_models_verdict() -> None:
    seen: dict[str, Any] = {}

    def fake(**kwargs: Any) -> dict:
        seen.update(kwargs)
        return {"answerable": False}

    assert chunk_answers("세출은 얼마나 늘었나요?", "세입은 줄었다", complete_json=fake) is False
    assert seen["schema_name"] == "chunk_verification"
    assert "세출은 얼마나 늘었나요?" in seen["user"]
    assert "세입은 줄었다" in seen["user"]


def test_chunk_answers_rejects_a_reply_without_the_boolean() -> None:
    with pytest.raises(LLMError):
        chunk_answers("질문", "본문", complete_json=lambda **k: {"answerable": "yes"})


# ---- the route --------------------------------------------------------------------


@pytest.fixture
def client() -> Iterator[TestClient]:
    app.dependency_overrides[get_db] = lambda: StubDb(make_document())
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(get_db, None)


def sse_events(body: str) -> list[tuple[str, dict]]:
    """(event name, decoded data) per event, in stream order."""
    events = []
    for block in body.split("\n\n"):
        name, data = None, None
        for line in block.splitlines():
            if line.startswith("event: "):
                name = line.removeprefix("event: ")
            elif line.startswith("data: "):
                data = json.loads(line.removeprefix("data: "))
        if name is not None:
            events.append((name, data))
    return events


def stream_events(stream: Any) -> list[tuple[str, dict]]:
    """The decoded events an answer stream yields, in order.

    The route is pinned to the scan pipeline while the big-prompt test runs,
    so the retrieval pipeline's tests drive its generator directly.
    """

    async def collect() -> list[str]:
        return [event async for event in stream]

    return sse_events("".join(asyncio.run(collect())))


def test_a_blank_query_is_422(client: TestClient) -> None:
    response = client.post(f"/document/{DOC_ID}/ask", json={"query": "   "})
    assert response.status_code == 422


def test_an_unknown_document_is_404(client: TestClient) -> None:
    response = client.post(f"/document/{uuid.uuid4()}/ask", json={"query": "질문"})
    assert response.status_code == 404


def test_an_unclear_query_stops_after_the_first_step(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "app.routers.client.classify_query",
        lambda query: Classification(query_type="unclear", search_query=query),
    )

    events = stream_events(_answer_stream("안녕?", DOC_ID))
    assert events == [
        (
            "progress",
            {
                "step": "question-checking",
                "label": "질문 의도를 분석하고 있습니다...",
                "status": "running",
            },
        ),
        (
            "progress",
            {
                "step": "question-checking",
                "label": "질문 의도를 분석하고 있습니다...",
                "status": "completed",
            },
        ),
        ("result", {"answer": NOT_FOUND_MESSAGE, "sources": [], "suggested_questions": []}),
    ]


class StubSessionLocal:
    def __enter__(self) -> StubDb:
        return StubDb(make_document())

    def __exit__(self, *exc: object) -> bool:
        return False


def test_a_summary_query_is_answered_from_the_stored_summary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.routers.client.SessionLocal", StubSessionLocal)
    monkeypatch.setattr(
        "app.routers.client.classify_query",
        lambda query: Classification(query_type="summary", search_query=query),
    )
    monkeypatch.setattr(
        "app.routers.client.find_stored_summary",
        lambda db, document, query: StoredSummary(text="**전체** 요약임", section=None),
    )
    monkeypatch.setattr(
        "app.routers.client.generate_summary_answer",
        lambda query, summary, section=None: "전체 내용을 요약하면 다음과 같습니다.",
    )
    monkeypatch.setattr(
        "app.routers.client.suggest_questions", lambda db, doc_id: ["세출은 얼마나 늘었나요?"]
    )

    def explode(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("retrieval must not run for a stored summary")

    monkeypatch.setattr("app.routers.client.retrieve_chunk", explode)

    events = stream_events(_answer_stream("이 보고서 요약해줘", DOC_ID))
    assert [name for name, _ in events] == ["progress"] * 6 + ["result"]
    assert [(d["step"], d["status"]) for _, d in events[:-1]] == [
        ("question-checking", "running"),
        ("question-checking", "completed"),
        ("context-finding", "running"),
        ("context-finding", "completed"),
        ("generating", "running"),
        ("generating", "completed"),
    ]
    assert events[-1][1] == {
        "answer": "전체 내용을 요약하면 다음과 같습니다.",
        "sources": [],
        "suggested_questions": ["세출은 얼마나 늘었나요?"],
    }


def test_a_summary_query_without_a_stored_summary_falls_through_to_retrieval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.routers.client.SessionLocal", StubSessionLocal)
    monkeypatch.setattr(
        "app.routers.client.classify_query",
        lambda query: Classification(query_type="summary", search_query=query),
    )
    monkeypatch.setattr(
        "app.routers.client.find_stored_summary", lambda db, document, query: None
    )
    # Nothing of the document is embedded: the body pipeline ends not-found.
    monkeypatch.setattr(
        "app.routers.client.retrieve_chunk", lambda db, *, doc_id, query: None
    )

    events = stream_events(_answer_stream("결론 요약해줘", DOC_ID))
    assert [(d["step"], d["status"]) for name, d in events if name == "progress"] == [
        ("question-checking", "running"),
        ("question-checking", "completed"),
        ("context-finding", "running"),
        ("context-finding", "completed"),
    ]
    assert events[-1] == (
        "result",
        {"answer": NOT_FOUND_MESSAGE, "sources": [], "suggested_questions": []},
    )


def test_headers_mode_scans_until_a_batch_answers(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.routers.client.SessionLocal", StubSessionLocal)
    row = make_content(11, 2, "부채가 늘었다")
    batches = [
        ScanBatch(header_id=1, heading="서론", rows=[make_content(10, 1, "배경")]),
        ScanBatch(header_id=2, heading="본론", rows=[row]),
    ]
    monkeypatch.setattr("app.routers.client.scan_batches", lambda db, doc_id: batches)
    monkeypatch.setattr(
        "app.routers.client.scan_batch_for_answer",
        lambda query, batch: (
            ScanAnswer(answer="부채가 늘었습니다", rows=[row])
            if batch.header_id == 2
            else None
        ),
    )
    monkeypatch.setattr(
        "app.routers.client.suggest_questions", lambda db, doc_id: ["세출은요?"]
    )
    monkeypatch.setattr(
        "app.routers.client.locate_rows",
        lambda db, document, rows: [
            QASource(
                page_idx=r.page_idx,
                page=None,
                text=r.content or "",
                bbox=(r.bbox_0, r.bbox_1, r.bbox_2, r.bbox_3),
            )
            for r in rows
        ],
    )

    def explode(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("retrieval must not run in headers mode")

    monkeypatch.setattr("app.routers.client.retrieve_chunk", explode)
    monkeypatch.setattr("app.routers.client.classify_query", explode)

    # The route is pinned to the scan pipeline while the big-prompt test runs.
    response = client.post(f"/document/{DOC_ID}/ask", json={"query": "부채는?"})

    assert response.status_code == 200
    events = sse_events(response.text)
    assert [(d["step"], d["status"]) for name, d in events if name == "progress"] == [
        ("question-checking", "running"),
        ("question-checking", "completed"),
        ("context-finding", "running"),
        ("context-finding", "completed"),
        ("generating", "running"),
        ("generating", "completed"),
    ]
    assert events[-1][0] == "result"
    assert events[-1][1]["answer"] == "부채가 늘었습니다"
    assert events[-1][1]["suggested_questions"] == ["세출은요?"]
    assert [s["text"] for s in events[-1][1]["sources"]] == ["부채가 늘었다"]


def test_headers_mode_with_no_answering_batch_is_not_found(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.routers.client.SessionLocal", StubSessionLocal)
    monkeypatch.setattr(
        "app.routers.client.scan_batches",
        lambda db, doc_id: [
            ScanBatch(header_id=1, heading="서론", rows=[make_content(10, 1, "배경")])
        ],
    )
    monkeypatch.setattr(
        "app.routers.client.scan_batch_for_answer", lambda query, batch: None
    )

    response = client.post(f"/document/{DOC_ID}/ask", json={"query": "부채는?"})

    events = sse_events(response.text)
    assert events[-1] == (
        "result",
        {"answer": NOT_FOUND_MESSAGE, "sources": [], "suggested_questions": []},
    )


def test_a_page_beyond_the_document_stops_before_any_model_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.routers.client.SessionLocal", StubSessionLocal)
    monkeypatch.setattr(
        "app.routers.client.query_pages_exist", lambda db, document, query: False
    )

    def explode(query: str) -> Classification:
        raise AssertionError("classification must not run for a refused page")

    monkeypatch.setattr("app.routers.client.classify_query", explode)

    events = stream_events(_answer_stream("500페이지 내용을 설명해줘", DOC_ID))
    assert [name for name, _ in events] == ["progress", "progress", "result"]
    assert events[-1][1] == {
        "answer": NOT_FOUND_MESSAGE,
        "sources": [],
        "suggested_questions": [],
    }
