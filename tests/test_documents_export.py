"""The JSON block behind the Documents page's Get JSON button.

The DB-facing resolution is the same resolve_selection the batch pages
exercise; these tests pin the serialized shape — exactly the four fields, in
the order given, with Korean filenames kept readable.
"""

import json
import uuid

from app.models.document import Document
from app.services.documents import selection_json


def doc(filename: str, pages: int | None) -> Document:
    return Document(
        id=uuid.uuid4(),
        bucket="opendata",
        s3_key=f"reports/2024/{filename}",
        filename=filename,
        size_bytes=1234,
        pages=pages,
    )


def test_each_document_becomes_exactly_the_four_fields() -> None:
    d = doc("보고서.pdf", 12)

    rows = json.loads(selection_json([d]))

    assert rows == [
        {
            "id": str(d.id),
            "s3_key": "reports/2024/보고서.pdf",
            "filename": "보고서.pdf",
            "pages": 12,
        }
    ]


def test_order_is_kept_and_unknown_pages_stay_null() -> None:
    first, second = doc("a.pdf", None), doc("b.pdf", 3)

    rows = json.loads(selection_json([first, second]))

    assert [r["filename"] for r in rows] == ["a.pdf", "b.pdf"]
    assert rows[0]["pages"] is None


def test_korean_filenames_are_not_ascii_escaped() -> None:
    text = selection_json([doc("연차보고서.pdf", 8)])

    # The block is meant to be read and copied by a person, so the hangul must
    # appear as written, not as \uXXXX escapes.
    assert "연차보고서.pdf" in text


def test_empty_selection_serializes_to_an_empty_array() -> None:
    assert json.loads(selection_json([])) == []
