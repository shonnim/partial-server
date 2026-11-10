import uuid
from datetime import datetime

from sqlalchemy import DateTime, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class Group(Base):
    """One group id's description: the human-readable name of the label.

    Documents carry their group id as a plain string column, and the
    strategies key on that string; this table only gives each id a name
    ("oft-1" → "해외재정동향"), written by hand on the Grouping page. An id
    with no row here is still a valid group — it simply has no description
    yet — and deleting a row never touches the documents that carry the id.
    """

    __tablename__ = "groups"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=func.gen_random_uuid(),
    )
    # The label as the documents column spells it, one row per id.
    group_id: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    description: Mapped[str] = mapped_column(Text, nullable=False)
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
