"""The client API's document endpoint, with the database played by a stub."""

import uuid
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app.db import get_db
from app.main import app
from app.models.document import Document

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
    )


class StubDb:
    """Stands in for the Session: only ``get`` by primary key is needed."""

    def __init__(self, doc: Document | None) -> None:
        self.doc = doc

    def get(self, model: type, pk: uuid.UUID) -> Document | None:
        if self.doc is not None and model is Document and pk == self.doc.id:
            return self.doc
        return None


@pytest.fixture
def client() -> Iterator[TestClient]:
    app.dependency_overrides[get_db] = lambda: StubDb(make_document())
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
