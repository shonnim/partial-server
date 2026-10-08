"""The client API's document endpoint, with the database played by a stub."""

import uuid
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app.db import get_db
from app.main import app
from app.models.document import Document
from app.models.header import Header

DOC_ID = uuid.uuid4()


def make_document() -> Document:
    return Document(
        id=DOC_ID,
        bucket="documents",
        s3_key=f"documents/{DOC_ID}.pdf",
        filename="2025 재정전망.pdf",
        size_bytes=1024,
        content_type="application/pdf",
        pages=34,
        summary="문서 전체 요약",
    )


def make_headers() -> list[Header]:
    return [
        Header(
            id=1,
            doc_id=DOC_ID,
            header="Ⅰ. 서론",
            parent_id=None,
            level=1,
            start_page=1,
            start_page_idx=2,
            conclusion="서론 결론",
        ),
        Header(
            id=2,
            doc_id=DOC_ID,
            header="1. 배경",
            parent_id=1,
            level=2,
            start_page=2,
            start_page_idx=3,
            conclusion=None,
        ),
    ]


class StubScalars:
    def __init__(self, rows: list[Header]) -> None:
        self.rows = rows

    def all(self) -> list[Header]:
        return self.rows


class StubDb:
    """Stands in for the Session: ``get`` by primary key and one headers query."""

    def __init__(self, doc: Document | None, headers: list[Header] | None = None) -> None:
        self.doc = doc
        self.headers = headers or []

    def get(self, model: type, pk: uuid.UUID) -> Document | None:
        if self.doc is not None and model is Document and pk == self.doc.id:
            return self.doc
        return None

    def scalars(self, statement: object) -> StubScalars:
        return StubScalars(self.headers)


@pytest.fixture
def client() -> Iterator[TestClient]:
    app.dependency_overrides[get_db] = lambda: StubDb(make_document(), make_headers())
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(get_db, None)


def test_document_endpoint_returns_link_pages_and_filename(client: TestClient) -> None:
    response = client.get(f"/document/{DOC_ID}")

    assert response.status_code == 200
    body = response.json()
    assert body["doc_id"] == str(DOC_ID)
    assert body["filename"] == "2025 재정전망.pdf"
    assert body["pages"] == 34
    # The link is a presigned RustFS URL: the object's key, signed. Signing
    # is local, so no RustFS needs to be running for this test.
    assert f"documents/{DOC_ID}.pdf" in body["file_url"]
    assert "X-Amz-Signature=" in body["file_url"]
    assert "X-Amz-Expires=" in body["file_url"]


def test_unknown_document_is_404(client: TestClient) -> None:
    response = client.get(f"/document/{uuid.uuid4()}")
    assert response.status_code == 404


def test_malformed_id_is_422(client: TestClient) -> None:
    response = client.get("/document/not-a-uuid")
    assert response.status_code == 422


def test_summary_endpoint_returns_doc_summary_and_headers(client: TestClient) -> None:
    response = client.get(f"/document/{DOC_ID}/summary")

    assert response.status_code == 200
    body = response.json()
    assert body["doc_summary"] == "문서 전체 요약"
    assert body["header_summary"] == [
        {
            "id": 1,
            "header": "Ⅰ. 서론",
            "parent_id": None,
            "start_page": 1,
            "start_page_idx": 2,
            "level": 1,
            "conclusion": "서론 결론",
        },
        {
            "id": 2,
            "header": "1. 배경",
            "parent_id": 1,
            "start_page": 2,
            "start_page_idx": 3,
            "level": 2,
            "conclusion": None,
        },
    ]


def test_summary_endpoint_unknown_document_is_404(client: TestClient) -> None:
    response = client.get(f"/document/{uuid.uuid4()}/summary")
    assert response.status_code == 404
