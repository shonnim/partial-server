"""Turning a document's content rows into chunks: the Chunking run.

The Chunking page picks documents in the tree and a group id from the
documents that carry one. Each group has its own strategy — ``CHUNKERS`` maps
the group id to the class that knows the group's layout — and only documents
that actually carry the chosen group id are chunked; the rest of the
selection is reported back as skipped, with the reason, rather than failing
the click.

A document is chunked in one transaction: its old chunks are deleted and the
new ones inserted together, so re-running never doubles a document up and a
failure leaves the previous chunks as they were.
"""

import logging
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Protocol

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.models.chunk import Chunk
from app.models.document import Document
from app.models.header import Header, HeaderContent
from app.services.chunking_imf1 import ImfHierarchicalChunker, StrategyResult
from app.services.tokenizer import TOKENIZER_NAME

logger = logging.getLogger(__name__)

# Documents one click may chunk. Chunking is tokenising and no model calls,
# so a document is well under a second; the cap keeps one request bounded.
MAX_CHUNK_BATCH = 200


class Chunker(Protocol):
    """What every group strategy provides."""

    def chunk(
        self, rows: Sequence[HeaderContent], headers: dict[int, Header]
    ) -> StrategyResult: ...


# The strategy for each group id. A group without an entry cannot be chunked
# yet, and the page says so rather than guessing a layout.
CHUNKERS: dict[str, type[Chunker]] = {
    "imf-1": ImfHierarchicalChunker,
}


@dataclass(frozen=True)
class ProcessedDoc:
    doc_id: uuid.UUID
    filename: str
    chunks: int
    warnings: list[str] = field(default_factory=list)
    # Table chunks still past the table budget after splitting by rows.
    needs_split: int = 0

    @property
    def id(self) -> uuid.UUID:
        return self.doc_id


@dataclass(frozen=True)
class SkippedDoc:
    doc_id: uuid.UUID
    filename: str | None
    reason: str


@dataclass(frozen=True)
class ChunkingResult:
    """What one Chunking click did, in the shape the page reports."""

    group_id: str
    processed: list[ProcessedDoc]
    skipped: list[SkippedDoc]
    # Set when the group has no strategy; then nothing was processed.
    error: str | None = None

    @property
    def total_chunks(self) -> int:
        return sum(p.chunks for p in self.processed)

    @property
    def total_warnings(self) -> int:
        return sum(len(p.warnings) for p in self.processed)

    def to_dict(self) -> dict:
        return {
            "group_id": self.group_id,
            "processed": [
                {"doc_id": str(p.doc_id), "chunks": p.chunks, "warnings": list(p.warnings)}
                for p in self.processed
            ],
            "skipped": [
                {"doc_id": str(s.doc_id), "reason": s.reason} for s in self.skipped
            ],
            **({"error": self.error} if self.error else {}),
        }


def chunkable_group_ids(db: Session, *, limit: int = 200) -> list[str]:
    """Distinct group ids on some document, blank ones left out."""
    rows = db.execute(
        select(Document.group_id)
        .where(Document.group_id.is_not(None), Document.group_id != "")
        .group_by(Document.group_id)
        .order_by(Document.group_id)
        .limit(limit)
    ).scalars()
    return list(rows)


def chunk_count(db: Session, doc_id: uuid.UUID) -> int:
    return db.execute(
        select(func.count()).select_from(Chunk).where(Chunk.doc_id == doc_id)
    ).scalar_one()


def document_chunks(db: Session, doc_id: uuid.UUID) -> list[Chunk]:
    """A document's chunks in order. A document has a few dozen to a few
    hundred, so the whole list is read at once."""
    return list(
        db.scalars(select(Chunk).where(Chunk.doc_id == doc_id).order_by(Chunk.order)).all()
    )


def _load_rows(db: Session, doc_id: uuid.UUID) -> list[HeaderContent]:
    return list(
        db.scalars(
            select(HeaderContent)
            .where(HeaderContent.doc_id == doc_id)
            .order_by(HeaderContent.id)
        ).all()
    )


def _load_headers(db: Session, doc_id: uuid.UUID) -> dict[int, Header]:
    return {
        header.id: header
        for header in db.scalars(select(Header).where(Header.doc_id == doc_id)).all()
    }


def _replace_chunks(
    db: Session, doc: Document, group_id: str, result: StrategyResult
) -> None:
    """Delete the document's chunks and insert the new set, as one transaction."""
    db.execute(delete(Chunk).where(Chunk.doc_id == doc.id))
    for index, draft in enumerate(result.chunks, start=1):
        db.add(
            Chunk(
                doc_id=doc.id,
                group_id=group_id,
                emb_text=draft.text,
                chunk_type=draft.chunk_type,
                section_path=draft.section_path,
                token_count=draft.token_count,
                order=index,
                head_content_ids=draft.head_content_ids,
            )
        )
    db.commit()


def run_chunking(
    db: Session, group_id: str, doc_ids: Sequence[uuid.UUID]
) -> ChunkingResult:
    """Chunk every document in ``doc_ids`` that carries ``group_id``.

    A document that is missing, carries another group, or has no content
    rows is skipped with the reason. A strategy or database failure on one
    document is reported the same way and the others still run.
    """
    strategy_class = CHUNKERS.get(group_id)
    if strategy_class is None:
        known = ", ".join(sorted(CHUNKERS)) or "none"
        return ChunkingResult(
            group_id=group_id,
            processed=[],
            skipped=[],
            error=(
                f"Chunking is not implemented for group id '{group_id}'. "
                f"Strategies exist for: {known}."
            ),
        )
    strategy = strategy_class()

    processed: list[ProcessedDoc] = []
    skipped: list[SkippedDoc] = []

    for doc_id in doc_ids:
        doc = db.get(Document, doc_id)
        if doc is None:
            skipped.append(SkippedDoc(doc_id, None, "document not found"))
            continue
        if doc.group_id != group_id:
            skipped.append(
                SkippedDoc(
                    doc_id,
                    doc.filename,
                    f"group_id mismatch (expected {group_id}, got {doc.group_id or 'none'})",
                )
            )
            continue

        rows = _load_rows(db, doc.id)
        if not rows:
            skipped.append(
                SkippedDoc(doc_id, doc.filename, "no content rows stored (run Section first)")
            )
            continue

        try:
            result = strategy.chunk(rows, _load_headers(db, doc.id))
            _replace_chunks(db, doc, group_id, result)
        except Exception as exc:  # one document's failure must not stop the batch
            db.rollback()
            logger.exception("chunking %s failed", doc.id)
            skipped.append(SkippedDoc(doc_id, doc.filename, f"failed: {exc}"))
            continue

        processed.append(
            ProcessedDoc(
                doc_id=doc.id,
                filename=doc.filename,
                chunks=len(result.chunks),
                warnings=result.warnings,
                needs_split=sum(1 for c in result.chunks if c.needs_split),
            )
        )
        logger.info(
            "chunked %s with %s: %d chunks, %d warnings (tokens by %s)",
            doc.id,
            group_id,
            len(result.chunks),
            len(result.warnings),
            TOKENIZER_NAME,
        )

    return ChunkingResult(group_id=group_id, processed=processed, skipped=skipped)
