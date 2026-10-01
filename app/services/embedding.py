"""Embedding chunks: Upstage makes the vectors, Qdrant keeps them.

The Embedding page drives this one document per request, the browser walking
a queue the same way the Section page does. A run embeds the chunks that
need it — pending, failed, or outdated, meaning the text no longer matches
the hash taken when it was embedded — and leaves current ones alone unless
forced. Statuses are committed chunk by chunk, so a half-finished run leaves
honest rows and the next run picks up exactly where it stopped.

Idempotency comes from the point id: a chunk's Qdrant point is keyed by
``chunks.id``, so re-embedding overwrites rather than duplicates. A re-chunk
gives a document new chunk ids, which is why every run first drops the
document's points that no current chunk owns — the stale vectors an old
chunking left behind.

Everything here is aggregate SQL or one document's chunks; nothing walks the
corpus.
"""

import hashlib
import logging
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select, text, update
from sqlalchemy.orm import Session

from app.embedding.client import EmbeddingError, EmbeddingResult, embed_passage
from app.models.chunk import Chunk
from app.models.document import Document, DocumentStage
from app.services.tree import _FOLDER_EXPR, Level
from app.vectors.client import (
    CollectionInfo,
    StoredPoint,
    VectorStore,
    VectorStoreError,
    get_store,
)

logger = logging.getLogger(__name__)

# The statuses a chunk row can carry.
PENDING = "pending"
PROCESSING = "processing"
EMBEDDED = "embedded"
FAILED = "failed"
SKIPPED = "skipped"
# Not stored: an embedded chunk whose text no longer matches its hash. It is
# computed when read, so nothing has to notice the moment text changes.
OUTDATED = "outdated"

STATUSES = (PENDING, PROCESSING, EMBEDDED, FAILED, SKIPPED, OUTDATED)

# What a plain run embeds; force embeds everything with text.
NEEDS_EMBEDDING = frozenset({PENDING, PROCESSING, FAILED, OUTDATED})


def content_hash(text_value: str) -> str:
    return hashlib.sha256(text_value.encode("utf-8")).hexdigest()


def effective_status(chunk: Chunk) -> str:
    """The row's status, with staleness read against the current text."""
    if chunk.embedding_status == EMBEDDED and chunk.content_hash != content_hash(
        chunk.emb_text or ""
    ):
        return OUTDATED
    return chunk.embedding_status


# ---- what a run reports ------------------------------------------------------


@dataclass(frozen=True)
class DocEmbedResult:
    """One document's embedding run, in the shape the queue row reports."""

    id: uuid.UUID
    filename: str
    ok: bool
    error: str | None = None
    embedded: int = 0
    failed: int = 0
    skipped: int = 0
    # Chunks already embedded and still current, left alone.
    unchanged: int = 0
    prompt_tokens: int = 0
    seconds: float = 0.0
    # The document's standing after the run, for the tree badge.
    total_chunks: int = 0
    embedded_total: int = 0
    # Every chunk embedded or skipped: the document reached VECTORDB_SAVED.
    done: bool = False


@dataclass(frozen=True)
class QueueCandidate:
    id: uuid.UUID
    filename: str
    chunks: int


@dataclass(frozen=True)
class QueueSkip:
    filename: str
    reason: str


@dataclass(frozen=True)
class EmbedQueue:
    eligible: list[QueueCandidate]
    skipped: list[QueueSkip]


def build_queue(
    db: Session, doc_ids: Sequence[uuid.UUID], *, force: bool = False
) -> EmbedQueue:
    """Split a selection into documents worth queueing and ones to skip.

    Only a document with chunks can be embedded, and one already fully
    embedded has nothing left to do unless the run is forced; both are
    reported with the reason rather than failing the click.
    """
    stats = file_stats(db, doc_ids)
    eligible: list[QueueCandidate] = []
    skipped: list[QueueSkip] = []
    for doc_id in doc_ids:
        doc = db.get(Document, doc_id)
        if doc is None:
            skipped.append(QueueSkip(str(doc_id), "document not found"))
            continue
        doc_stats = stats.get(doc.id)
        if doc_stats is None or not doc_stats.total:
            skipped.append(QueueSkip(doc.filename, "no chunks stored (run Chunking first)"))
            continue
        if doc_stats.done and not force:
            skipped.append(QueueSkip(doc.filename, "already embedded"))
            continue
        eligible.append(QueueCandidate(doc.id, doc.filename, doc_stats.total))
    return EmbedQueue(eligible=eligible, skipped=skipped)


# ---- embedding ----------------------------------------------------------------


def _doc_chunks(db: Session, doc_id: uuid.UUID) -> list[Chunk]:
    return list(
        db.scalars(select(Chunk).where(Chunk.doc_id == doc_id).order_by(Chunk.order)).all()
    )


def _mark(db: Session, chunk: Chunk, status: str, *, error: str | None = None) -> None:
    chunk.embedding_status = status
    chunk.embedding_error = error
    db.commit()


def _is_done(chunks: Sequence[Chunk]) -> bool:
    """Whether every chunk is settled: embedded, or skipped for having no text."""
    return bool(chunks) and all(
        c.embedding_status in (EMBEDDED, SKIPPED) for c in chunks
    )


def _sync_stage(db: Session, doc: Document, chunks: Sequence[Chunk]) -> bool:
    """Keep ``documents.stage`` honest against the chunks' statuses.

    Fully embedded moves the document to VECTORDB_SAVED; anything less takes
    that stage away again (back to CHUNKING — the chunks still exist), so the
    stage never claims vectors that are not all there. Other stages are left
    alone: this service only owns the last step. Returns whether the document
    now stands at VECTORDB_SAVED.
    """
    done = _is_done(chunks)
    if done and doc.stage != DocumentStage.VECTORDB_SAVED:
        doc.stage = DocumentStage.VECTORDB_SAVED
        db.commit()
    elif not done and doc.stage == DocumentStage.VECTORDB_SAVED:
        doc.stage = DocumentStage.CHUNKING
        db.commit()
    return done


def _embed_one(
    db: Session,
    chunk: Chunk,
    *,
    store: VectorStore,
    embed: Callable[[str], EmbeddingResult],
) -> tuple[str, int]:
    """Embed one chunk and settle its row: (final status, prompt tokens).

    The row is committed at every step, so a crash mid-run leaves at worst
    one chunk marked processing — which the next run treats as unfinished
    work and redoes.
    """
    text_value = (chunk.emb_text or "").strip()
    if not text_value:
        _mark(db, chunk, SKIPPED)
        return SKIPPED, 0

    _mark(db, chunk, PROCESSING)
    try:
        result = embed(chunk.emb_text)
    except EmbeddingError as exc:
        _mark(db, chunk, FAILED, error=str(exc))
        return FAILED, 0

    try:
        size = store.ensure_collection(result.dimension)
        if result.dimension != size:
            raise VectorStoreError(
                f"the model returned {result.dimension} dimensions, "
                f"the collection holds {size}"
            )
        store.upsert(
            chunk_id=chunk.id,
            doc_id=str(chunk.doc_id),
            vector=result.vector,
            model=result.model,
        )
    except VectorStoreError as exc:
        _mark(db, chunk, FAILED, error=str(exc))
        return FAILED, 0

    chunk.embedding_status = EMBEDDED
    chunk.embedding_model = result.model
    chunk.embedded_at = datetime.now(UTC)
    chunk.prompt_tokens = result.prompt_tokens
    chunk.embedding_error = None
    chunk.content_hash = content_hash(chunk.emb_text)
    db.commit()
    return EMBEDDED, result.prompt_tokens


def embed_document(
    db: Session,
    doc: Document,
    *,
    force: bool = False,
    store: VectorStore | None = None,
    embed: Callable[[str], EmbeddingResult] = embed_passage,
) -> DocEmbedResult:
    """Embed one document's chunks that need it; everything with ``force``."""
    store = store or get_store()
    started = time.perf_counter()
    chunks = _doc_chunks(db, doc.id)
    if not chunks:
        return DocEmbedResult(
            id=doc.id,
            filename=doc.filename,
            ok=False,
            error="no chunks stored (run Chunking first)",
        )

    # Stale points first: a re-chunk renumbered this document's chunks, and
    # the points under the old ids would linger in every search.
    try:
        store.delete_document(str(doc.id), keep_chunk_ids=[c.id for c in chunks])
    except VectorStoreError as exc:
        return DocEmbedResult(
            id=doc.id,
            filename=doc.filename,
            ok=False,
            error=str(exc),
            seconds=time.perf_counter() - started,
        )

    counts = {EMBEDDED: 0, FAILED: 0, SKIPPED: 0}
    tokens = 0
    unchanged = 0
    for chunk in chunks:
        if not force and effective_status(chunk) not in NEEDS_EMBEDDING:
            if chunk.embedding_status == EMBEDDED:
                unchanged += 1
            else:
                counts[SKIPPED] += 1  # stays skipped: there is still no text
            continue
        status, spent = _embed_one(db, chunk, store=store, embed=embed)
        counts[status] += 1
        tokens += spent

    embedded_total = sum(1 for c in chunks if c.embedding_status == EMBEDDED)
    done = _sync_stage(db, doc, chunks)
    result = DocEmbedResult(
        id=doc.id,
        filename=doc.filename,
        ok=True,
        embedded=counts[EMBEDDED],
        failed=counts[FAILED],
        skipped=counts[SKIPPED],
        unchanged=unchanged,
        prompt_tokens=tokens,
        seconds=time.perf_counter() - started,
        total_chunks=len(chunks),
        embedded_total=embedded_total,
        done=done,
    )
    logger.info(
        "embedded %s: %d embedded, %d failed, %d skipped, %d unchanged, %d tokens",
        doc.id,
        result.embedded,
        result.failed,
        result.skipped,
        result.unchanged,
        result.prompt_tokens,
    )
    return result


def embed_chunk(
    db: Session,
    chunk: Chunk,
    *,
    store: VectorStore | None = None,
    embed: Callable[[str], EmbeddingResult] = embed_passage,
) -> str:
    """Embed one chunk regardless of its status; returns the final status."""
    store = store or get_store()
    status, _ = _embed_one(db, chunk, store=store, embed=embed)
    # One chunk can be the one that completes — or breaks — the document.
    doc = db.get(Document, chunk.doc_id)
    if doc is not None:
        _sync_stage(db, doc, _doc_chunks(db, doc.id))
    return status


def delete_document_vectors(db: Session, doc: Document, *, store: VectorStore | None = None) -> int:
    """Drop a document's points and put its rows back to pending.

    Returns how many rows were reset. Raises VectorStoreError when Qdrant
    cannot be reached, before any row is touched.
    """
    store = store or get_store()
    store.delete_document(str(doc.id))
    reset = db.execute(
        update(Chunk)
        .where(Chunk.doc_id == doc.id)
        .values(
            embedding_status=PENDING,
            embedding_model=None,
            embedded_at=None,
            prompt_tokens=None,
            embedding_error=None,
            content_hash=None,
        )
    )
    # The vectors are gone, so the document no longer stands at VECTORDB_SAVED.
    if doc.stage == DocumentStage.VECTORDB_SAVED:
        doc.stage = DocumentStage.CHUNKING
    db.commit()
    return reset.rowcount or 0


# ---- what the page reads -------------------------------------------------------


@dataclass(frozen=True)
class DocEmbedStats:
    """One document's standing, for badges and the panel header."""

    total: int = 0
    embedded: int = 0
    failed: int = 0
    skipped: int = 0

    @property
    def done(self) -> bool:
        return self.total > 0 and self.embedded + self.skipped == self.total


def file_stats(db: Session, doc_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, DocEmbedStats]:
    """Per-document chunk counts for the file rows of one tree level."""
    if not doc_ids:
        return {}
    rows = db.execute(
        text("""
            SELECT doc_id,
                   count(*) AS total,
                   count(*) FILTER (WHERE embedding_status = 'embedded') AS embedded,
                   count(*) FILTER (WHERE embedding_status = 'failed') AS failed,
                   count(*) FILTER (WHERE embedding_status = 'skipped') AS skipped
            FROM chunks
            WHERE doc_id = ANY(cast(:ids AS uuid[]))
            GROUP BY doc_id
        """),
        {"ids": [str(i) for i in doc_ids]},
    ).all()
    return {
        r.doc_id: DocEmbedStats(r.total, r.embedded, r.failed, r.skipped) for r in rows
    }


@dataclass(frozen=True)
class FolderEmbedStats:
    """How a folder's documents stand: fully embedded ones and troubled ones."""

    done: int = 0
    failed: int = 0


def folder_stats(db: Session, path: str) -> dict[str, FolderEmbedStats]:
    """Per-subfolder counts for one tree level, keyed by subfolder name."""
    child_depth = len(path.split("/")) + 1 if path else 1
    rows = db.execute(
        text(f"""
            WITH scoped AS (
                SELECT id, {_FOLDER_EXPR} AS folder
                FROM documents
                WHERE :path = '' OR starts_with(s3_key, :path || '/')
            ),
            per_doc AS (
                SELECT s.folder,
                       count(c.id) AS chunks,
                       count(*) FILTER (WHERE c.embedding_status = 'embedded') AS embedded,
                       count(*) FILTER (WHERE c.embedding_status = 'skipped') AS skipped,
                       count(*) FILTER (WHERE c.embedding_status = 'failed') AS failed
                FROM scoped s
                JOIN chunks c ON c.doc_id = s.id
                WHERE s.folder IS NOT NULL
                GROUP BY s.id, s.folder
            )
            SELECT split_part(folder, '/', :child_depth) AS name,
                   count(*) FILTER (WHERE embedded + skipped = chunks) AS done,
                   count(*) FILTER (WHERE failed > 0) AS failed
            FROM per_doc
            WHERE split_part(folder, '/', :child_depth) <> ''
            GROUP BY 1
        """),
        {"path": path, "child_depth": child_depth},
    ).all()
    return {r.name: FolderEmbedStats(r.done, r.failed) for r in rows}


def decorate_level(db: Session, level: Level) -> dict:
    """The template context for one tree level: the level plus its stats."""
    return {
        "level": level,
        "estats": file_stats(db, [f.id for f in level.files]),
        "fstats": folder_stats(db, level.path) if level.folders else {},
    }


@dataclass(frozen=True)
class PanelChunk:
    """One chunk row of the document panel."""

    chunk: Chunk
    status: str  # effective: embedding_status, or 'outdated'


@dataclass(frozen=True)
class DocPanel:
    doc: Document
    chunks: list[PanelChunk]
    stats: DocEmbedStats
    outdated: int
    status_filter: str
    query: str

    @property
    def shown(self) -> int:
        return len(self.chunks)


def document_panel(
    db: Session, doc: Document, *, status_filter: str = "", query: str = ""
) -> DocPanel:
    """The document's chunks with their effective statuses, filtered for show.

    A document holds a few dozen to a few hundred chunks, so the whole list
    is read at once — same as the chunks pop-up on the Chunking page.
    """
    rows = _doc_chunks(db, doc.id)
    panel = [PanelChunk(chunk=c, status=effective_status(c)) for c in rows]

    stats = DocEmbedStats(
        total=len(rows),
        embedded=sum(1 for c in rows if c.embedding_status == EMBEDDED),
        failed=sum(1 for c in rows if c.embedding_status == FAILED),
        skipped=sum(1 for c in rows if c.embedding_status == SKIPPED),
    )
    outdated = sum(1 for p in panel if p.status == OUTDATED)

    if status_filter:
        panel = [p for p in panel if p.status == status_filter]
    needle = query.strip().lower()
    if needle:
        panel = [p for p in panel if needle in (p.chunk.emb_text or "").lower()]

    return DocPanel(
        doc=doc,
        chunks=panel,
        stats=stats,
        outdated=outdated,
        status_filter=status_filter,
        query=query,
    )


@dataclass(frozen=True)
class FolderDocRow:
    id: uuid.UUID
    filename: str
    stats: DocEmbedStats


@dataclass(frozen=True)
class FolderPanel:
    path: str
    docs: list[FolderDocRow]
    total_docs: int
    with_chunks: int


FOLDER_PANEL_LIMIT = 200


def folder_panel(db: Session, path: str) -> FolderPanel:
    """A folder's documents with their embedding standing, capped for one page."""
    rows = db.execute(
        text(f"""
            SELECT d.id, d.filename,
                   count(c.id) AS total,
                   count(*) FILTER (WHERE c.embedding_status = 'embedded') AS embedded,
                   count(*) FILTER (WHERE c.embedding_status = 'failed') AS failed,
                   count(*) FILTER (WHERE c.embedding_status = 'skipped') AS skipped
            FROM documents d
            LEFT JOIN chunks c ON c.doc_id = d.id
            WHERE {"starts_with(d.s3_key, :path || '/')" if path else "TRUE"}
            GROUP BY d.id, d.filename
            ORDER BY d.filename, d.id
            LIMIT :limit
        """),
        {"path": path, "limit": FOLDER_PANEL_LIMIT},
    ).all()
    docs = [
        FolderDocRow(
            id=r.id,
            filename=r.filename,
            stats=DocEmbedStats(r.total, r.embedded or 0, r.failed or 0, r.skipped or 0),
        )
        for r in rows
    ]
    return FolderPanel(
        path=path,
        docs=docs,
        total_docs=len(docs),
        with_chunks=sum(1 for d in docs if d.stats.total),
    )


@dataclass(frozen=True)
class ChunkDetail:
    """What the sidebar shows about one chunk."""

    chunk: Chunk
    status: str
    point: StoredPoint | None = None
    store_error: str | None = None


def chunk_detail(chunk: Chunk, *, store: VectorStore | None = None) -> ChunkDetail:
    store = store or get_store()
    point = None
    store_error = None
    try:
        point = store.get_point(chunk.id)
    except VectorStoreError as exc:
        store_error = str(exc)
    return ChunkDetail(chunk=chunk, status=effective_status(chunk), point=point, store_error=store_error)


@dataclass(frozen=True)
class CollectionOverview:
    """The sidebar's resting state: the collection and whether Qdrant answers."""

    healthy: bool
    info: CollectionInfo | None = None
    error: str | None = None


def collection_overview(*, store: VectorStore | None = None) -> CollectionOverview:
    store = store or get_store()
    healthy = store.healthy()
    if not healthy:
        return CollectionOverview(healthy=False, error="Qdrant is not reachable")
    try:
        return CollectionOverview(healthy=True, info=store.info())
    except VectorStoreError as exc:
        return CollectionOverview(healthy=True, error=str(exc))
