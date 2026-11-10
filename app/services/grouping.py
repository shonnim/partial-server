"""Assigning a group id to a set of documents, and naming the groups.

A group is nothing more than a label on the document row: the Grouping page
lets the user tick files and folders in the tree, type a label, and stamp it
on every document picked. Nothing is derived from it here — downstream stages
read it to handle the documents together.

The ids are terse ("oft-1", "bpa-2"), so the page also keeps a list of every
known group with a hand-written description — its Korean name, "oft-1" →
"해외재정동향" — stored one row per id in ``groups``. The description is
documentation only: strategies key on the id string, never on the name.
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from app.models.document import Document
from app.models.group import Group
from app.services.chunking import CHUNKERS
from app.services.header_pages import GROUP_STRATEGIES

# Documents one Save click may stamp. Folder ticks are resolved server-side
# before this cap is applied, so it bounds the UPDATE rather than the ids the
# browser happened to send.
MAX_GROUP_BATCH = 2000

# The column is VARCHAR(255); refuse anything longer at the boundary rather
# than let Postgres raise.
MAX_GROUP_ID_LENGTH = 255

# A description is a name, not a paragraph. The cap is boundary validation;
# the column itself is text.
MAX_DESCRIPTION_LENGTH = 255


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


# ---- the group list and its descriptions --------------------------------------


@dataclass(frozen=True)
class GroupInfo:
    """One known group id, as the page's list shows it."""

    group_id: str
    #: The hand-written name; None for an id never described.
    description: str | None
    #: Documents currently carrying the id.
    documents: int


def _document_counts(db: Session, *, limit: int = 200) -> dict[str, int]:
    """Documents per group id, for every id some document carries."""
    rows = db.execute(
        select(Document.group_id, func.count())
        .where(Document.group_id.is_not(None))
        .group_by(Document.group_id)
        .order_by(Document.group_id)
        .limit(limit)
    ).all()
    return {group_id: count for group_id, count in rows}


def _descriptions(db: Session) -> dict[str, str]:
    """The stored descriptions, by group id."""
    return {
        row.group_id: row.description for row in db.scalars(select(Group)).all()
    }


def list_groups(db: Session) -> list[GroupInfo]:
    """Every known group id with its description and document count, sorted.

    Known means: carried by some document, registered with some stage's
    strategy — header extraction's GROUP_STRATEGIES, chunking's CHUNKERS —
    or described by hand. A registered id is listed before any document
    carries it, so a fresh corpus can be labelled straight into its strategy
    without retyping the id from memory.
    """
    counts = _document_counts(db)
    described = _descriptions(db)
    ids = sorted({*counts, *described, *GROUP_STRATEGIES, *CHUNKERS})
    return [
        GroupInfo(
            group_id=group_id,
            description=described.get(group_id),
            documents=counts.get(group_id, 0),
        )
        for group_id in ids
    ]


def save_description(db: Session, group_id: str, description: str | None) -> None:
    """Write one group id's description; an empty one removes the row.

    An upsert by hand: one row per id is enforced by the unique constraint,
    and the Grouping page is a single-user tool, so read-then-write is
    enough. Removing a description never touches the documents that carry
    the id — the id stays known through them and the registries.
    """
    row = db.scalar(select(Group).where(Group.group_id == group_id))
    if description is None:
        if row is not None:
            db.execute(delete(Group).where(Group.id == row.id))
    elif row is None:
        db.add(Group(group_id=group_id, description=description))
    else:
        row.description = description
    db.commit()
