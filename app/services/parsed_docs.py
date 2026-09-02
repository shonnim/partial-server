import uuid
from collections import Counter
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.models.document import Document
from app.models.parsed_doc import ParsedDoc
from app.schemas.parsed_doc import ParsedDocIn

# Columns an upsert refreshes when the id already exists. `id` and `created_at`
# are deliberately absent: identity and first-seen time never change.
_UPDATABLE = (
    "doc_id",
    "task_id",
    "status",
    "backend",
    "bucket",
    "folder_path",
    "size_bytes",
    "category",
    "updated_at",
)

# Rows per INSERT statement. Batches are capped well below this at the schema
# boundary; chunking keeps a single statement bounded regardless.
_CHUNK_SIZE = 500

# Explicit ids bypass the sequence, which would then hand out an id that is
# already taken the next time a row arrives without one.
_RESYNC_SEQUENCE = text(
    "SELECT setval("
    "  pg_get_serial_sequence('parsed_docs', 'id'),"
    "  COALESCE(MAX(id), 1),"
    "  MAX(id) IS NOT NULL"
    ") FROM parsed_docs"
)


class DuplicateIdsInBatch(ValueError):
    """The same id appeared more than once in one request."""

    def __init__(self, ids: list[int]) -> None:
        self.ids = ids
        super().__init__(
            f"duplicate ids within the batch: {', '.join(str(i) for i in ids)}"
        )


class UnknownDocumentIds(ValueError):
    """The batch referenced doc_ids that are not registered documents."""

    def __init__(self, ids: list[str]) -> None:
        self.ids = ids
        super().__init__(f"unknown doc_ids: {', '.join(ids)}")


def _to_row(parsed: ParsedDocIn, now: datetime) -> dict[str, Any]:
    """Flatten one payload item into insertable column values.

    Absent timestamps are filled here rather than left to the column defaults,
    so every row in the statement carries the same column set. ``id`` is left
    out when absent and the sequence assigns it.
    """
    row: dict[str, Any] = {
        "doc_id": parsed.doc_id,
        "task_id": parsed.task_id,
        "status": parsed.status,
        "backend": parsed.backend,
        "bucket": parsed.bucket,
        "folder_path": parsed.folder_path,
        "size_bytes": parsed.size_bytes,
        "category": parsed.category,
        "created_at": parsed.created_at or now,
        "updated_at": parsed.updated_at or now,
    }
    if parsed.id is not None:
        row["id"] = parsed.id
    return row


def _assert_documents_exist(db: Session, doc_ids: set[uuid.UUID]) -> None:
    """Fail the whole batch when it points at a document that isn't registered.

    The foreign key would catch this anyway, but only as an opaque error on the
    first offending row; checking up front names every bad id at once.
    """
    known = set(
        db.scalars(select(Document.id).where(Document.id.in_(doc_ids))).all()
    )
    missing = doc_ids - known
    if missing:
        raise UnknownDocumentIds(sorted(str(i) for i in missing))


def upsert_parsed_docs(db: Session, parsed_docs: list[ParsedDocIn]) -> int:
    """Insert parsed-doc rows, refreshing any whose id already exists.

    Returns the number of rows persisted. Re-posting an identical batch is a
    no-op beyond the timestamp refresh, which is what makes an interrupted
    import safe to retry.

    Raises DuplicateIdsInBatch if one request repeats an id: Postgres cannot
    apply two conflicting updates to the same row in one statement, and
    silently keeping the last one would hide a malformed payload.
    """
    now = datetime.now(UTC)
    rows = [_to_row(parsed, now) for parsed in parsed_docs]

    with_id = [r for r in rows if "id" in r]
    repeated = [i for i, n in Counter(r["id"] for r in with_id).items() if n > 1]
    if repeated:
        raise DuplicateIdsInBatch(sorted(repeated))

    _assert_documents_exist(db, {r["doc_id"] for r in rows})

    # Rows carrying an id upsert on it; rows without one can't conflict, so
    # they are a plain insert that lets the sequence assign the id.
    for start in range(0, len(with_id), _CHUNK_SIZE):
        chunk = with_id[start : start + _CHUNK_SIZE]
        stmt = insert(ParsedDoc).values(chunk)
        db.execute(
            stmt.on_conflict_do_update(
                index_elements=[ParsedDoc.id],
                set_={c: getattr(stmt.excluded, c) for c in _UPDATABLE},
            )
        )

    without_id = [r for r in rows if "id" not in r]
    for start in range(0, len(without_id), _CHUNK_SIZE):
        db.execute(insert(ParsedDoc).values(without_id[start : start + _CHUNK_SIZE]))

    if with_id:
        db.execute(_RESYNC_SEQUENCE)

    db.commit()
    return len(rows)
