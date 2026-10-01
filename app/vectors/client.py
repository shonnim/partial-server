"""The Qdrant wrapper: every vector goes through here, nothing else talks to it.

One collection holds every chunk vector. Its size is not hardcoded anywhere:
the collection is created from the first embedding that arrives, and from
then on the stored size is the law — a vector of another length is refused
with the reason rather than written.

The point id is ``chunks.id``, which Postgres hands out as an integer, so an
upsert of the same chunk overwrites its point and re-embedding can never
duplicate anything. The payload carries ``doc_id`` (as a string, indexed as
a keyword), ``chunk_id``, the model and the timestamp — never the chunk
text; Postgres remains the source of truth for content.

Every Qdrant failure surfaces as VectorStoreError with a message worth
showing on the panel.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from typing import Any

from qdrant_client import QdrantClient
from qdrant_client import models as qm

from app.config import get_settings

# Ordinary calls are quick; this is a local container.
TIMEOUT_SECONDS = 10


class VectorStoreError(RuntimeError):
    """Qdrant could not be reached, or refused what it was asked."""


@dataclass(frozen=True)
class CollectionInfo:
    """What the sidebar shows about the collection."""

    name: str
    vector_size: int
    distance: str
    points: int
    status: str


@dataclass(frozen=True)
class StoredPoint:
    """One chunk's point as Qdrant holds it."""

    chunk_id: int
    payload: dict[str, Any]
    dimension: int
    # The first few values, for the sidebar; never the whole vector.
    head: list[float]


class VectorStore:
    """The one collection of chunk vectors, by way of the official client."""

    def __init__(self, client: QdrantClient, collection: str) -> None:
        self._client = client
        self.collection = collection

    # ---- the collection -------------------------------------------------------

    def ensure_collection(self, vector_size: int) -> int:
        """The collection's vector size, creating the collection when missing.

        Raises VectorStoreError when a collection already exists with another
        size: that is a corpus embedded with a different model, and quietly
        mixing the two would poison every search.
        """
        existing = self._size_or_none()
        if existing is not None:
            if existing != vector_size:
                raise VectorStoreError(
                    f"collection '{self.collection}' holds {existing}-dimensional "
                    f"vectors but the model returned {vector_size}; "
                    "drop the collection or fix the model before embedding"
                )
            return existing

        try:
            self._client.create_collection(
                collection_name=self.collection,
                vectors_config=qm.VectorParams(
                    size=vector_size, distance=qm.Distance.COSINE
                ),
            )
            self._client.create_payload_index(
                self.collection, "doc_id", field_schema=qm.PayloadSchemaType.KEYWORD
            )
            self._client.create_payload_index(
                self.collection, "chunk_id", field_schema=qm.PayloadSchemaType.INTEGER
            )
        except VectorStoreError:
            raise
        except Exception as exc:
            raise VectorStoreError(f"could not create the collection: {exc}") from exc
        return vector_size

    def _size_or_none(self) -> int | None:
        try:
            if not self._client.collection_exists(self.collection):
                return None
            info = self._client.get_collection(self.collection)
        except Exception as exc:
            raise VectorStoreError(f"could not reach Qdrant: {exc}") from exc
        vectors = info.config.params.vectors
        if not isinstance(vectors, qm.VectorParams):
            raise VectorStoreError(
                f"collection '{self.collection}' uses named vectors; "
                "this panel expects a single unnamed vector"
            )
        return vectors.size

    def info(self) -> CollectionInfo | None:
        """The collection as the sidebar reports it, or None when not created yet."""
        try:
            if not self._client.collection_exists(self.collection):
                return None
            info = self._client.get_collection(self.collection)
        except Exception as exc:
            raise VectorStoreError(f"could not reach Qdrant: {exc}") from exc
        vectors = info.config.params.vectors
        size = vectors.size if isinstance(vectors, qm.VectorParams) else 0
        distance = (
            getattr(vectors.distance, "value", str(vectors.distance))
            if isinstance(vectors, qm.VectorParams)
            else "?"
        )
        return CollectionInfo(
            name=self.collection,
            vector_size=size,
            distance=distance,
            points=info.points_count or 0,
            status=str(info.status),
        )

    def healthy(self) -> bool:
        try:
            self._client.get_collections()
            return True
        except Exception:
            return False

    # ---- points ---------------------------------------------------------------

    def upsert(self, *, chunk_id: int, doc_id: str, vector: list[float], model: str) -> None:
        """Write one chunk's point, overwriting any previous one."""
        try:
            self._client.upsert(
                self.collection,
                points=[
                    qm.PointStruct(
                        id=chunk_id,
                        vector=vector,
                        payload={
                            "doc_id": doc_id,
                            "chunk_id": chunk_id,
                            "model": model,
                            "embedded_at": datetime.now(UTC).isoformat(),
                        },
                    )
                ],
                wait=True,
            )
        except Exception as exc:
            raise VectorStoreError(f"could not store the vector: {exc}") from exc

    def delete_document(self, doc_id: str, *, keep_chunk_ids: list[int] | None = None) -> None:
        """Drop a document's points — all of them, or all but ``keep_chunk_ids``.

        The keep list is how stale points die: a re-chunk gives a document new
        chunk ids, and the points written under the old ids would otherwise
        linger in every search.
        """
        must_not = [qm.HasIdCondition(has_id=keep_chunk_ids)] if keep_chunk_ids else None
        try:
            if not self._client.collection_exists(self.collection):
                return
            self._client.delete(
                self.collection,
                points_selector=qm.FilterSelector(
                    filter=qm.Filter(
                        must=[
                            qm.FieldCondition(key="doc_id", match=qm.MatchValue(value=doc_id))
                        ],
                        must_not=must_not,
                    )
                ),
                wait=True,
            )
        except Exception as exc:
            raise VectorStoreError(f"could not delete the document's vectors: {exc}") from exc

    def get_point(self, chunk_id: int, *, head: int = 8) -> StoredPoint | None:
        """One chunk's point with the first ``head`` vector values, or None."""
        try:
            if not self._client.collection_exists(self.collection):
                return None
            points = self._client.retrieve(
                self.collection, ids=[chunk_id], with_payload=True, with_vectors=True
            )
        except Exception as exc:
            raise VectorStoreError(f"could not read the vector: {exc}") from exc
        if not points:
            return None
        point = points[0]
        vector = point.vector if isinstance(point.vector, list) else []
        return StoredPoint(
            chunk_id=chunk_id,
            payload=dict(point.payload or {}),
            dimension=len(vector),
            head=[float(v) for v in vector[:head]],
        )


@lru_cache(maxsize=1)
def get_store() -> VectorStore:
    settings = get_settings()
    return VectorStore(
        QdrantClient(url=settings.qdrant_url, timeout=TIMEOUT_SECONDS),
        settings.qdrant_collection,
    )
