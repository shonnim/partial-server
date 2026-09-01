import uuid
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from math import ceil
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.models.document import Document
from app.schemas.document import DocumentIn

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


# The corpus is ~9k documents; a list view never selects without a LIMIT.
DEFAULT_PAGE_SIZE = 50


@dataclass(frozen=True)
class DocumentPage:
    """One page of the document list, plus what the pager needs to render."""

    items: Sequence[Document]
    total: int
    page: int
    page_size: int

    @property
    def pages(self) -> int:
        return max(1, ceil(self.total / self.page_size))

    @property
    def has_prev(self) -> bool:
        return self.page > 1

    @property
    def has_next(self) -> bool:
        return self.page < self.pages

    @property
    def first_index(self) -> int:
        """1-based index of the first row on this page, 0 when empty."""
        return (self.page - 1) * self.page_size + 1 if self.total else 0

    @property
    def last_index(self) -> int:
        return min(self.page * self.page_size, self.total)


def list_documents(
    db: Session,
    *,
    page: int = 1,
    page_size: int = DEFAULT_PAGE_SIZE,
    q: str | None = None,
    category: str | None = None,
) -> DocumentPage:
    """Return one page of documents, newest first.

    ``q`` matches the filename or the object key, case-insensitively.
    """
    page = max(1, page)
    page_size = max(1, min(page_size, 500))

    filters = []
    if q:
        pattern = f"%{q.strip()}%"
        filters.append(
            or_(Document.filename.ilike(pattern), Document.s3_key.ilike(pattern))
        )
    if category:
        filters.append(Document.category == category)

    total = db.scalar(select(func.count()).select_from(Document).where(*filters)) or 0

    # A page past the end is clamped to the last one rather than rendering an
    # empty table under a range like "4901-5 of 5".
    page = min(page, max(1, ceil(total / page_size)))

    items = (
        db.scalars(
            select(Document)
            .where(*filters)
            .order_by(Document.created_at.desc(), Document.id)
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        .all()
    )

    return DocumentPage(items=items, total=total, page=page, page_size=page_size)


def list_categories(db: Session) -> list[str]:
    """Distinct categories present in the corpus, for the filter dropdown."""
    rows = db.scalars(
        select(Document.category)
        .where(Document.category.is_not(None))
        .distinct()
        .order_by(Document.category)
    ).all()
    return [c for c in rows if c]


def get_document(db: Session, document_id: uuid.UUID) -> Document | None:
    return db.get(Document, document_id)
