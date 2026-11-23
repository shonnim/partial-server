import json
import uuid
from collections import Counter
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.models.document import Document
from app.schemas.document import DocumentIn

# Documents one Get JSON click may cover. The export is a metadata-only
# SELECT, so it can take far more rows per click than the pipeline batches
# that do real work per document.
MAX_EXPORT_BATCH = 1000

# Columns an upsert refreshes when the id already exists. `id` and `created_at`
# are deliberately absent: identity and registration time never change.
_UPDATABLE = (
    "bucket",
    "s3_key",
    "filename",
    "size_bytes",
    "content_type",
    "category",
    "stage",
    "status",
    "pages",
    "updated_at",
)

# Rows per INSERT statement. Batches are capped well below this at the schema
# boundary; chunking keeps a single statement bounded regardless.
_CHUNK_SIZE = 500


class DuplicateIdsInBatch(ValueError):
    """The same id appeared more than once in one request."""

    def __init__(self, ids: list[str]) -> None:
        self.ids = ids
        super().__init__(f"duplicate ids within the batch: {', '.join(ids)}")


def _to_row(doc: DocumentIn, now: datetime) -> dict[str, Any]:
    """Flatten one payload item into insertable column values.

    Absent id and timestamps are filled here rather than left to the column
    defaults, so every row in the statement carries the same column set.
    """
    return {
        "id": doc.id or uuid.uuid4(),
        "bucket": doc.bucket,
        "s3_key": doc.s3_key,
        "filename": doc.filename,
        "size_bytes": doc.size_bytes,
        "content_type": doc.content_type,
        "category": doc.category,
        "stage": doc.stage,
        "status": doc.status,
        "pages": doc.pages,
        "created_at": doc.created_at or now,
        "updated_at": doc.updated_at or now,
    }


def upsert_documents(db: Session, documents: list[DocumentIn]) -> int:
    """Insert documents, refreshing any whose id already exists.

    Returns the number of rows persisted. Re-posting an identical batch is a
    no-op beyond the timestamp refresh, which is what makes an interrupted
    import safe to retry.

    Raises DuplicateIdsInBatch if one request repeats an id: Postgres cannot
    apply two conflicting updates to the same row in one statement, and
    silently keeping the last one would hide a malformed payload.
    """
    now = datetime.now(UTC)
    rows = [_to_row(doc, now) for doc in documents]

    repeated = [str(i) for i, n in Counter(r["id"] for r in rows).items() if n > 1]
    if repeated:
        raise DuplicateIdsInBatch(sorted(repeated))

    for start in range(0, len(rows), _CHUNK_SIZE):
        chunk = rows[start : start + _CHUNK_SIZE]
        stmt = insert(Document).values(chunk)
        db.execute(
            stmt.on_conflict_do_update(
                index_elements=[Document.id],
                set_={c: getattr(stmt.excluded, c) for c in _UPDATABLE},
            )
        )

    db.commit()
    return len(rows)


def get_document(db: Session, document_id: uuid.UUID) -> Document | None:
    return db.get(Document, document_id)


def find_by_source_ids(
    db: Session,
    *,
    source_document_id: str,
    atch_file_id: str,
    atch_file_seq: int,
) -> list[Document]:
    """Documents carrying these source-system ids, at most two rows.

    Two on purpose: the caller needs to tell "exactly one" from "more than
    one" — an ambiguous mapping — without fetching every match.
    """
    return (
        db.scalars(
            select(Document)
            .where(
                Document.source_document_id == source_document_id,
                Document.atch_file_id == atch_file_id,
                Document.atch_file_seq == atch_file_seq,
            )
            .order_by(Document.id)
            .limit(2)
        )
    ).all()


def documents_by_ids(db: Session, doc_ids: Sequence[uuid.UUID]) -> list[Document]:
    """The selected documents, in the tree's filename order."""
    if not doc_ids:
        return []

    return list(
        db.scalars(
            select(Document)
            .where(Document.id.in_(doc_ids))
            .order_by(Document.filename, Document.id)
        )
    )


def selection_json(docs: Sequence[Document]) -> str:
    """The selection as the JSON block the Documents page shows.

    ``ensure_ascii`` is off: filenames are frequently Korean, and \\u-escapes
    would make the block unreadable for exactly the people copying it out.
    """
    rows = [
        {
            "id": str(doc.id),
            "s3_key": doc.s3_key,
            "filename": doc.filename,
            "pages": doc.pages,
        }
        for doc in docs
    ]
    return json.dumps(rows, ensure_ascii=False, indent=2)
