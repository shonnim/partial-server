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
        source_document_id="51750",
        atch_file_id="F0000000000000004810",
        atch_file_seq=1,
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
    def __init__(self, rows: list) -> None:
        self.rows = rows

    def all(self) -> list:
        return self.rows


class StubDb:
    """Stands in for the Session: ``get`` by primary key and two scalar queries.

    ``scalars`` routes on the selected entity: a Document select is the
    source-ids lookup and answers with ``source_docs``; anything else is the
    headers query. The stub applies no WHERE — each test says what the lookup
    finds by constructing the stub with it.
    """

    def __init__(
        self,
        doc: Document | None,
        headers: list[Header] | None = None,
        source_docs: list[Document] | None = None,
    ) -> None:
        self.doc = doc
        self.headers = headers or []
        self.source_docs = source_docs or []

    def get(self, model: type, pk: uuid.UUID) -> Document | None:
        if self.doc is not None and model is Document and pk == self.doc.id:
            return self.doc
        return None

    def scalars(self, statement) -> StubScalars:
        if statement.column_descriptions[0]["entity"] is Document:
            return StubScalars(self.source_docs)
        return StubScalars(self.headers)


def override_db(db: StubDb) -> None:
    app.dependency_overrides[get_db] = lambda: db


@pytest.fixture
def client() -> Iterator[TestClient]:
    override_db(StubDb(make_document(), make_headers(), source_docs=[make_document()]))
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(get_db, None)


# The query string the portal sends, as in its real URLs.
BY_SOURCE_PARAMS = {
    "service": "report",
    "documentType": "publication",
    "sourceDocumentId": "51750",
    "atchFileId": "F0000000000000004810",
    "atchFileSeq": "1",
}


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


def test_lookup_by_source_ids_answers_like_the_uuid_route(client: TestClient) -> None:
    response = client.get("/document", params=BY_SOURCE_PARAMS)

    assert response.status_code == 200
    body = response.json()
    # Same shape and values as the uuid route; the presigned link is compared
    # by key only, because its signature embeds the signing time.
    uuid_body = client.get(f"/document/{DOC_ID}").json()
    assert body["doc_id"] == uuid_body["doc_id"] == str(DOC_ID)
    assert body["filename"] == uuid_body["filename"]
    assert body["pages"] == uuid_body["pages"]
    assert f"documents/{DOC_ID}.pdf" in body["file_url"]
    assert "X-Amz-Signature=" in body["file_url"]


def test_lookup_without_document_type_still_answers(client: TestClient) -> None:
    params = {k: v for k, v in BY_SOURCE_PARAMS.items() if k != "documentType"}
    assert client.get("/document", params=params).status_code == 200


def test_lookup_with_another_service_is_422(client: TestClient) -> None:
    response = client.get("/document", params={**BY_SOURCE_PARAMS, "service": "news"})
    assert response.status_code == 422


def test_lookup_missing_an_id_is_422(client: TestClient) -> None:
    params = {k: v for k, v in BY_SOURCE_PARAMS.items() if k != "atchFileId"}
    assert client.get("/document", params=params).status_code == 422


def test_unmapped_source_ids_are_404(client: TestClient) -> None:
    override_db(StubDb(make_document(), make_headers(), source_docs=[]))
    assert client.get("/document", params=BY_SOURCE_PARAMS).status_code == 404


def test_ids_mapped_to_two_documents_are_409(client: TestClient) -> None:
    override_db(
        StubDb(
            make_document(),
            make_headers(),
            source_docs=[make_document(), make_document()],
        )
    )
    assert client.get("/document", params=BY_SOURCE_PARAMS).status_code == 409


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
