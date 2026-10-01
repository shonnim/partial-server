"""The embed pipeline for one chunk, with the API and Qdrant played by fakes.

No Postgres: the chunk rows are plain ORM objects and the session stand-in
only has to swallow commits, which is all ``_embed_one`` asks of it.
"""

import uuid

from qdrant_client import QdrantClient

from app.embedding.client import EmbeddingError, EmbeddingResult
from app.models.chunk import Chunk
from app.models.document import Document, DocumentStage
from app.services.embedding import (
    EMBEDDED,
    FAILED,
    OUTDATED,
    SKIPPED,
    _embed_one,
    _sync_stage,
    content_hash,
    effective_status,
)
from app.vectors.client import VectorStore


class FakeDB:
    """Enough of a Session for the per-chunk pipeline: commits, counted."""

    def __init__(self) -> None:
        self.commits = 0

    def commit(self) -> None:
        self.commits += 1


def make_chunk(text: str = "본문", chunk_id: int = 7) -> Chunk:
    return Chunk(
        id=chunk_id,
        doc_id=uuid.uuid4(),
        emb_text=text,
        chunk_type="text",
        token_count=len(text),
        embedding_status="pending",
    )


def ok_embed(text: str) -> EmbeddingResult:
    return EmbeddingResult(vector=[0.1, 0.2, 0.3], model="solar", prompt_tokens=5)


def store_of() -> VectorStore:
    return VectorStore(QdrantClient(":memory:"), "chunks-test")


def test_success_marks_embedded_and_stores_the_point() -> None:
    db, store, chunk = FakeDB(), store_of(), make_chunk()
    status, tokens = _embed_one(db, chunk, store=store, embed=ok_embed)

    assert (status, tokens) == (EMBEDDED, 5)
    assert chunk.embedding_status == EMBEDDED
    assert chunk.embedding_model == "solar"
    assert chunk.prompt_tokens == 5
    assert chunk.embedded_at is not None
    assert chunk.embedding_error is None
    assert chunk.content_hash == content_hash(chunk.emb_text)
    assert effective_status(chunk) == EMBEDDED

    point = store.get_point(chunk.id)
    assert point is not None
    assert point.payload["doc_id"] == str(chunk.doc_id)


def test_empty_text_is_skipped_without_an_api_call() -> None:
    calls: list[str] = []

    def spy(text: str) -> EmbeddingResult:
        calls.append(text)
        return ok_embed(text)

    db, chunk = FakeDB(), make_chunk(text="   ")
    status, _ = _embed_one(db, chunk, store=store_of(), embed=spy)

    assert status == SKIPPED
    assert chunk.embedding_status == SKIPPED
    assert calls == []


def test_api_failure_marks_failed_with_the_reason() -> None:
    def broken(text: str) -> EmbeddingResult:
        raise EmbeddingError("embedding API returned HTTP 429 (after 3 retries)")

    db, store, chunk = FakeDB(), store_of(), make_chunk()
    status, _ = _embed_one(db, chunk, store=store, embed=broken)

    assert status == FAILED
    assert chunk.embedding_status == FAILED
    assert "HTTP 429" in chunk.embedding_error
    assert store.get_point(chunk.id) is None


def test_dimension_mismatch_against_the_collection_marks_failed() -> None:
    db, store = FakeDB(), store_of()
    store.ensure_collection(8)  # the corpus was embedded at another size
    chunk = make_chunk()
    status, _ = _embed_one(db, chunk, store=store, embed=ok_embed)

    assert status == FAILED
    assert "8-dimensional" in chunk.embedding_error


def test_re_embedding_overwrites_instead_of_duplicating() -> None:
    db, store, chunk = FakeDB(), store_of(), make_chunk()
    _embed_one(db, chunk, store=store, embed=ok_embed)
    _embed_one(db, chunk, store=store, embed=ok_embed)

    assert store.info().points == 1


def make_doc(stage: DocumentStage) -> Document:
    return Document(id=uuid.uuid4(), filename="doc.pdf", stage=stage)


def chunk_with(status: str) -> Chunk:
    chunk = make_chunk()
    chunk.embedding_status = status
    return chunk


def test_fully_embedded_document_moves_to_vectordb_saved() -> None:
    doc = make_doc(DocumentStage.CHUNKING)
    chunks = [chunk_with(EMBEDDED), chunk_with(SKIPPED)]
    assert _sync_stage(FakeDB(), doc, chunks) is True
    assert doc.stage == DocumentStage.VECTORDB_SAVED


def test_incomplete_document_loses_vectordb_saved() -> None:
    doc = make_doc(DocumentStage.VECTORDB_SAVED)
    chunks = [chunk_with(EMBEDDED), chunk_with("pending")]
    assert _sync_stage(FakeDB(), doc, chunks) is False
    assert doc.stage == DocumentStage.CHUNKING


def test_a_failed_chunk_keeps_the_stage_away() -> None:
    doc = make_doc(DocumentStage.CHUNKING)
    chunks = [chunk_with(EMBEDDED), chunk_with(FAILED)]
    assert _sync_stage(FakeDB(), doc, chunks) is False
    assert doc.stage == DocumentStage.CHUNKING


def test_other_stages_are_left_alone_when_not_done() -> None:
    doc = make_doc(DocumentStage.PARSING)
    assert _sync_stage(FakeDB(), doc, [chunk_with("pending")]) is False
    assert doc.stage == DocumentStage.PARSING


def test_no_chunks_is_never_done() -> None:
    doc = make_doc(DocumentStage.CHUNKING)
    assert _sync_stage(FakeDB(), doc, []) is False
    assert doc.stage == DocumentStage.CHUNKING


def test_changed_text_reads_as_outdated_until_re_embedded() -> None:
    db, store, chunk = FakeDB(), store_of(), make_chunk(text="원래 본문")
    _embed_one(db, chunk, store=store, embed=ok_embed)
    assert effective_status(chunk) == EMBEDDED

    chunk.emb_text = "고쳐 쓴 본문"
    assert effective_status(chunk) == OUTDATED

    _embed_one(db, chunk, store=store, embed=ok_embed)
    assert effective_status(chunk) == EMBEDDED
