import uuid
from datetime import datetime

from sqlalchemy import (
    DateTime,
    FetchedValue,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class Chunk(Base):
    """One piece of a document's text, as it will be handed to the embedder."""

    __tablename__ = "chunks"
    __table_args__ = (UniqueConstraint("doc_id", "order", name="uq_chunks_doc_id_order"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    doc_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("documents.id", ondelete="CASCADE"),
        nullable=False,
    )
    # The strategy that made the chunk: the group id the document carried at
    # the time. A regrouped document's old chunks are told apart by this.
    group_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    # The final text, breadcrumb included — what the embedder is handed.
    emb_text: Mapped[str] = mapped_column(Text, nullable=False)
    # text | table | image | chart
    chunk_type: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default="text"
    )
    # The breadcrumb the chunk opens with, kept apart for filtering.
    section_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    token_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    # Position within the document, counted from 1 for each doc_id. Left unset
    # on insert, the chunks_assign_order trigger fills in the document's next
    # number. "order" is a reserved word in SQL; SQLAlchemy quotes it on emit.
    order: Mapped[int] = mapped_column(
        "order",
        Integer,
        nullable=False,
        server_default=FetchedValue(),
    )
    # ids of the header_contents rows this chunk was built from. Postgres
    # cannot put a foreign key on array elements, so nothing enforces these.
    head_content_ids: Mapped[list[int]] = mapped_column(
        ARRAY(Integer),
        nullable=False,
        server_default="{}",
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
