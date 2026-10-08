"""The question-answering pipeline, with the models played by fakes."""

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
from app.models.document import Document
from app.models.header import HeaderContent
from app.services.qa import (
    NOT_FOUND_MESSAGE,
    Classification,
    classify_query,
    generate_answer,
    identify_sources,
    pick_source_ids,
    printed_page,
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


def test_a_blank_query_is_422(client: TestClient) -> None:
    response = client.post(f"/document/{DOC_ID}/ask", json={"query": "   "})
    assert response.status_code == 422


def test_an_unknown_document_is_404(client: TestClient) -> None:
    response = client.post(f"/document/{uuid.uuid4()}/ask", json={"query": "질문"})
    assert response.status_code == 404


def test_an_unclear_query_stops_after_the_first_step(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "app.routers.client.classify_query",
        lambda query: Classification(query_type="unclear", search_query=query),
    )

    response = client.post(f"/document/{DOC_ID}/ask", json={"query": "안녕?"})

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    events = sse_events(response.text)
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
        ("result", {"answer": NOT_FOUND_MESSAGE, "sources": []}),
    ]
