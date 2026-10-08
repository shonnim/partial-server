"""The retrieval service, with the query model played by a fake."""

import uuid

from qdrant_client import QdrantClient

from app.embedding.client import EmbeddingResult
from app.services.retrieval import search_chunks
from app.vectors.client import VectorStore

DOC_A = str(uuid.uuid4())
DOC_B = str(uuid.uuid4())


def fake_embed(text: str) -> EmbeddingResult:
    """The 'query model': every question lands at [1, 0]."""
    return EmbeddingResult(vector=[1.0, 0.0], model="solar-embedding-2-query", prompt_tokens=3)


def store_with_points() -> VectorStore:
    store = VectorStore(QdrantClient(":memory:"), "chunks-test")
    store.ensure_collection(2)
    store.upsert(chunk_id=11, doc_id=DOC_A, vector=[0.9, 0.1], model="m")
    store.upsert(chunk_id=12, doc_id=DOC_A, vector=[0.1, 0.9], model="m")
    store.upsert(chunk_id=21, doc_id=DOC_B, vector=[1.0, 0.0], model="m")
    return store


def test_search_returns_the_documents_chunks_best_first() -> None:
    hits = search_chunks(
        "국가채무 전망은?", doc_id=DOC_A, limit=5, store=store_with_points(), embed=fake_embed
    )

    assert [h.chunk_id for h in hits] == [11, 12]
    assert hits[0].score > hits[1].score
    # The other document's exact match never leaks in.
    assert all(h.chunk_id != 21 for h in hits)


def test_search_against_an_empty_store_is_an_empty_list() -> None:
    store = VectorStore(QdrantClient(":memory:"), "chunks-test")
    assert search_chunks("질문", doc_id=DOC_A, limit=5, store=store, embed=fake_embed) == []
