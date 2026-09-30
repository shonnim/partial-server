import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Enum, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class DocumentStage(str, enum.Enum):
    DOC_UPLOAD = "DOC_UPLOAD"
    PARSING = "PARSING"
    PRE_CHUNKING = "PRE_CHUNKING"
    CHUNKING = "CHUNKING"
    EMBEDDING = "EMBEDDING"
    VECTORDB_SAVED = "VECTORDB_SAVED"


class DocumentStatus(str, enum.Enum):
    PROCESS = "PROCESS"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class HasHeader(str, enum.Enum):
    """Whether a document's outline has been extracted into ``headers``."""

    YES = "YES"
    NO = "NO"
    UNCHECKED = "UNCHECKED"


class Document(Base):
    __tablename__ = "documents"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=func.gen_random_uuid(),
    )
    bucket: Mapped[str] = mapped_column(String(63), nullable=False)
    s3_key: Mapped[str] = mapped_column(Text, nullable=False)
    filename: Mapped[str] = mapped_column(Text, nullable=False)
    folder_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    content_type: Mapped[str] = mapped_column(String(255), nullable=False)
    category: Mapped[str | None] = mapped_column(Text, nullable=True, index=True)
    # A free-text label assigned on the Grouping page. Documents that share
    # one are handled together downstream; NULL means never grouped.
    group_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    has_header: Mapped[HasHeader] = mapped_column(
        Enum(HasHeader, name="document_has_header", native_enum=True),
        nullable=False,
        server_default=HasHeader.UNCHECKED.value,
    )
    # Page count is a parse result, so it is unknown until PARSING completes.
    pages: Mapped[int | None] = mapped_column(Integer, nullable=True)
    stage: Mapped[DocumentStage] = mapped_column(
        Enum(DocumentStage, name="document_stage", native_enum=True),
        nullable=False,
        server_default=DocumentStage.DOC_UPLOAD.value,
    )
    status: Mapped[DocumentStatus] = mapped_column(
        Enum(DocumentStatus, name="document_status", native_enum=True),
        nullable=False,
        server_default=DocumentStatus.PROCESS.value,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
