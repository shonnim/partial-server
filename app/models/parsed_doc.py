import uuid
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import ENUM, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.document import DocumentStatus


class ParsedDoc(Base):
    __tablename__ = "parsed_docs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    doc_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("documents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    task_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    status: Mapped[DocumentStatus] = mapped_column(
        # Reuses the document_status type owned by the documents table, so this
        # column must never emit its own CREATE TYPE.
        ENUM(DocumentStatus, name="document_status", create_type=False),
        nullable=False,
        server_default=DocumentStatus.PROCESS.value,
    )
    backend: Mapped[str | None] = mapped_column(String(255), nullable=True)
    bucket: Mapped[str | None] = mapped_column(String(63), nullable=True)
    folder_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    category: Mapped[str | None] = mapped_column(Text, nullable=True)
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
