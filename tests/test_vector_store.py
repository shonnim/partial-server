"""The Qdrant wrapper, against the client's in-memory local mode — no server.

Local mode speaks the same API as the container, so collection creation,
idempotent upserts and delete-by-document run for real here.
"""

import uuid

import pytest
from qdrant_client import QdrantClient

from app.vectors.client import VectorStore, VectorStoreError

DOC_A = str(uuid.uuid4())
DOC_B = str(uuid.uuid4())


@pytest.fixture
def store() -> VectorStore:
    return VectorStore(QdrantClient(":memory:"), "chunks-test")


def test_collection_is_created_once_and_size_is_law(store: VectorStore) -> None:
    assert store.info() is None
    assert store.ensure_collection(4) == 4
    assert store.ensure_collection(4) == 4  # idempotent

    info = store.info()
    assert info is not None
    assert info.name == "chunks-test"
    assert info.vector_size == 4
    assert info.distance == "Cosine"
    assert info.points == 0

    with pytest.raises(VectorStoreError, match="4-dimensional"):
        store.ensure_collection(8)


def test_upsert_is_idempotent_and_point_id_is_the_chunk_id(store: VectorStore) -> None:
    store.ensure_collection(3)
    store.upsert(chunk_id=42, doc_id=DOC_A, vector=[0.1, 0.2, 0.3], model="m")
    first = store.get_point(42).head
    store.upsert(chunk_id=42, doc_id=DOC_A, vector=[0.9, 0.2, 0.1], model="m")

    assert store.info().points == 1
    point = store.get_point(42)
    assert point is not None
    assert point.chunk_id == 42
    assert point.dimension == 3
    assert point.payload["doc_id"] == DOC_A
    assert point.payload["chunk_id"] == 42
    assert point.payload["model"] == "m"
    assert point.payload["embedded_at"]
    # The later upsert overwrote the vector. (Cosine storage may normalise
    # the values, so the proof is the change, not the exact numbers.)
    assert point.head != first


def test_payload_never_holds_the_chunk_text(store: VectorStore) -> None:
    store.ensure_collection(2)
    store.upsert(chunk_id=1, doc_id=DOC_A, vector=[0.1, 0.2], model="m")
    assert set(store.get_point(1).payload) == {"doc_id", "chunk_id", "model", "embedded_at"}


def test_delete_document_only_touches_that_document(store: VectorStore) -> None:
    store.ensure_collection(2)
    store.upsert(chunk_id=1, doc_id=DOC_A, vector=[0.1, 0.2], model="m")
    store.upsert(chunk_id=2, doc_id=DOC_A, vector=[0.3, 0.4], model="m")
    store.upsert(chunk_id=3, doc_id=DOC_B, vector=[0.5, 0.6], model="m")

    store.delete_document(DOC_A)
    assert store.get_point(1) is None
    assert store.get_point(2) is None
    assert store.get_point(3) is not None


def test_delete_document_keeps_the_listed_chunk_ids(store: VectorStore) -> None:
    """The stale-point sweep: old ids die, the current ones stay."""
    store.ensure_collection(2)
    store.upsert(chunk_id=10, doc_id=DOC_A, vector=[0.1, 0.2], model="m")  # stale
    store.upsert(chunk_id=20, doc_id=DOC_A, vector=[0.3, 0.4], model="m")  # current

    store.delete_document(DOC_A, keep_chunk_ids=[20])
    assert store.get_point(10) is None
    assert store.get_point(20) is not None


def test_missing_collection_reads_as_empty_not_an_error(store: VectorStore) -> None:
    assert store.get_point(1) is None
    store.delete_document(DOC_A)  # a no-op, not a crash
    assert store.search([0.1, 0.2], limit=5) == []
    assert store.healthy() is True


def test_search_ranks_by_similarity_within_one_document(store: VectorStore) -> None:
    store.ensure_collection(2)
    store.upsert(chunk_id=1, doc_id=DOC_A, vector=[1.0, 0.0], model="m")   # exact
    store.upsert(chunk_id=2, doc_id=DOC_A, vector=[0.7, 0.7], model="m")   # close
    store.upsert(chunk_id=3, doc_id=DOC_A, vector=[0.0, 1.0], model="m")   # far
    store.upsert(chunk_id=4, doc_id=DOC_B, vector=[1.0, 0.0], model="m")   # other doc

    hits = store.search([1.0, 0.0], limit=5, doc_id=DOC_A)

    assert [h.chunk_id for h in hits] == [1, 2, 3]
    assert hits[0].score >= hits[1].score >= hits[2].score
    assert all(h.chunk_id != 4 for h in hits)


def test_search_limit_caps_the_hits(store: VectorStore) -> None:
    store.ensure_collection(2)
    for i in range(8):
        store.upsert(chunk_id=i, doc_id=DOC_A, vector=[1.0, i / 10], model="m")

    assert len(store.search([1.0, 0.0], limit=5, doc_id=DOC_A)) == 5
