import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class Header(Base):
    """One heading in a document's outline.

    Headers form a tree within a single document via ``parent_id``; a row with a
    NULL parent is a top-level heading.
    """

    __tablename__ = "headers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    doc_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("documents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    header: Mapped[str] = mapped_column(Text, nullable=False)
    parent_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("headers.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    # Depth in the outline: 1 for a root, 2 for its children, and so on.
    level: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    start_page: Mapped[int | None] = mapped_column(Integer, nullable=True)
    start_page_idx: Mapped[int | None] = mapped_column(Integer, nullable=True)
    end_page_idx: Mapped[int | None] = mapped_column(Integer, nullable=True)
    conclusion: Mapped[str | None] = mapped_column(Text, nullable=True)
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


class HeaderContent(Base):
    """One extracted block sitting under a header, in reading order."""

    __tablename__ = "header_contents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    header_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("headers.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    doc_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("documents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    page_idx: Mapped[int | None] = mapped_column(Integer, nullable=True)
    type: Mapped[str] = mapped_column(String(50), nullable=False)
    text: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Caption attached to a table, image, or chart block; NULL for plain text.
    caption: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Table block parsed into a structured (rows/cells) form; NULL for non-table blocks.
    structured_json_table: Mapped[dict | list | None] = mapped_column(JSONB, nullable=True)
    # "order" is a reserved word in SQL; SQLAlchemy quotes it on emit.
    order: Mapped[int | None] = mapped_column("order", Integer, nullable=True)
    bbox_0: Mapped[int | None] = mapped_column(Integer, nullable=True)
    bbox_1: Mapped[int | None] = mapped_column(Integer, nullable=True)
    bbox_2: Mapped[int | None] = mapped_column(Integer, nullable=True)
    bbox_3: Mapped[int | None] = mapped_column(Integer, nullable=True)
