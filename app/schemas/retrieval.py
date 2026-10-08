"""The retrieval API's request and response bodies."""

import uuid

from pydantic import BaseModel, Field

# What one search returns: the spec of the retrieval step.
TOP_K = 5


class RetrievalRequest(BaseModel):
    query: str = Field(min_length=1, description="The user's question, as typed.")
    doc_id: uuid.UUID = Field(description="The document whose chunks are searched.")


class RetrievalMatch(BaseModel):
    chunk_id: int
    # Cosine similarity as Qdrant scores it: higher is closer.
    score: float


class RetrievalResponse(BaseModel):
    query: str
    doc_id: uuid.UUID
    matches: list[RetrievalMatch]
