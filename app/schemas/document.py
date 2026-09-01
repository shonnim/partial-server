import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.models.document import DocumentStage, DocumentStatus


class DocumentIn(BaseModel):
    """One row of an import batch.

    ``id`` and the timestamps are optional: omit them for a freshly registered
    document and the database supplies them. Backup restores send them
    explicitly so the original identity and history are preserved.
    """

    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID | None = None
    bucket: str = Field(max_length=63, min_length=1)
    s3_key: str = Field(min_length=1)
    filename: str = Field(min_length=1)
    size_bytes: int = Field(ge=0)
    content_type: str = Field(max_length=255, min_length=1)
    category: str | None = None
    stage: DocumentStage = DocumentStage.DOC_UPLOAD
    status: DocumentStatus = DocumentStatus.PROCESS
    pages: int | None = Field(default=None, ge=0)
    created_at: datetime | None = None
    updated_at: datetime | None = None


class DocumentBulkIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    data: list[DocumentIn] = Field(min_length=1, max_length=1000)


class DocumentBulkOut(BaseModel):
    received: int
