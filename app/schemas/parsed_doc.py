import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.models.document import DocumentStatus


class ParsedDocIn(BaseModel):
    """One row of a parsed-doc import batch.

    ``id`` and the timestamps are optional: omit them and the database supplies
    them. Imports from an existing parse run send them explicitly so the
    original identity and history are preserved.
    """

    model_config = ConfigDict(extra="forbid")

    id: int | None = Field(default=None, ge=1)
    doc_id: uuid.UUID
    task_id: str | None = Field(default=None, max_length=255)
    status: DocumentStatus = DocumentStatus.PROCESS
    backend: str | None = Field(default=None, max_length=255)
    bucket: str | None = Field(default=None, max_length=63)
    folder_path: str | None = None
    size_bytes: int | None = Field(default=None, ge=0)
    category: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class ParsedDocBulkIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    data: list[ParsedDocIn] = Field(min_length=1, max_length=1000)


class ParsedDocBulkOut(BaseModel):
    inserted: int
