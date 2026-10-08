import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.db import get_db
from app.embedding.client import EmbeddingError
from app.schemas.retrieval import (
    TOP_K,
    RetrievalMatch,
    RetrievalRequest,
    RetrievalResponse,
)
from app.services.documents import get_document
from app.services.retrieval import search_chunks
from app.vectors.client import VectorStoreError

router = APIRouter(prefix="/retrieval", tags=["retrieval"])


@router.post("/search", response_model=RetrievalResponse, name="retrieval_search")
def retrieval_search(
    body: RetrievalRequest, db: Session = Depends(get_db)
) -> RetrievalResponse:
    """The top chunks of one document for a user's question.

    The question is embedded with the query model and matched against the
    document's points in Qdrant; the reply is chunk ids with scores, best
    first. A JSON API, unlike the HTMX pages: errors are status codes.
    """
    if not body.query.strip():
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "query is empty")
    if get_document(db, body.doc_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "document not found")

    try:
        hits = search_chunks(body.query, doc_id=str(body.doc_id), limit=TOP_K)
    except EmbeddingError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    except VectorStoreError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc

    return RetrievalResponse(
        query=body.query,
        doc_id=body.doc_id,
        matches=[RetrievalMatch(chunk_id=h.chunk_id, score=h.score) for h in hits],
    )
