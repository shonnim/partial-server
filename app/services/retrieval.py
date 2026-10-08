"""Retrieval: a user's question against one document's embedded chunks.

The question goes through the query model — the other half of the passage
model the chunks were embedded with — and the vector is matched against the
document's points in Qdrant. What comes back is chunk ids and scores, best
first; the chunk text itself stays in Postgres, where ids resolve to rows.
"""

from collections.abc import Callable

from app.embedding.client import EmbeddingResult, embed_query
from app.vectors.client import SearchHit, VectorStore, get_store


def search_chunks(
    query: str,
    *,
    doc_id: str,
    limit: int,
    store: VectorStore | None = None,
    embed: Callable[[str], EmbeddingResult] = embed_query,
) -> list[SearchHit]:
    """The ``limit`` chunks of one document nearest to ``query``, best first.

    Raises EmbeddingError when the question cannot be embedded and
    VectorStoreError when Qdrant cannot be searched; the router turns those
    into status codes.
    """
    store = store or get_store()
    result = embed(query)
    return store.search(result.vector, limit=limit, doc_id=doc_id)
