"""Assigning a group id to a set of documents.

A group is nothing more than a label on the document row: the Grouping page
lets the user tick files and folders in the tree, type a label, and stamp it
on every document picked. Nothing is derived from it here — downstream stages
read it to handle the documents together.
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.models.document import Document

# Documents one Save click may stamp. Folder ticks are resolved server-side
# before this cap is applied, so it bounds the UPDATE rather than the ids the
# browser happened to send.
MAX_GROUP_BATCH = 2000

# The column is VARCHAR(255); refuse anything longer at the boundary rather
# than let Postgres raise.
MAX_GROUP_ID_LENGTH = 255


@dataclass(frozen=True)
class GroupResult:
    """What one Save click did."""

    group_id: str | None
    doc_ids: Sequence[uuid.UUID]

    @property
    def count(self) -> int:
        return len(self.doc_ids)

    @property
    def cleared(self) -> bool:
        return self.group_id is None


def normalise_group_id(raw: str) -> str | None:
    """Trim the typed label; an empty one means "no group"."""
    value = raw.strip()
    return value or None


def assign_group(
    db: Session, doc_ids: Sequence[uuid.UUID], group_id: str | None
) -> GroupResult:
    """Stamp ``group_id`` on every document in ``doc_ids`` (NULL clears it).

    Re-running with the same label is a no-op beyond ``updated_at``, so a
    second click cannot corrupt anything. Returns the ids actually updated,
    which can be fewer than asked when a selection outlived a document.
    """
    if not doc_ids:
        return GroupResult(group_id=group_id, doc_ids=[])

    updated = (
        db.execute(
            update(Document)
            .where(Document.id.in_(list(doc_ids)))
            .values(group_id=group_id, updated_at=func.now())
            .returning(Document.id)
        )
        .scalars()
        .all()
    )
    db.commit()

    return GroupResult(group_id=group_id, doc_ids=updated)


def group_ids_in_use(db: Session, *, limit: int = 200) -> list[str]:
    """Distinct labels already on some document, for the input's suggestions."""
    rows = db.execute(
        select(Document.group_id)
        .where(Document.group_id.is_not(None))
        .group_by(Document.group_id)
        .order_by(Document.group_id)
        .limit(limit)
    ).scalars()
    return list(rows)
