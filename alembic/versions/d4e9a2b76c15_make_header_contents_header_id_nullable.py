"""make header_contents.header_id nullable

Revision ID: d4e9a2b76c15
Revises: c8b2d4f96e31
Create Date: 2026-09-09 12:00:00.000000

A block that comes before a document's first header (front matter, the
contents page) belongs to the document but to no section of it, so its row
carries the document and no header.
"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'd4e9a2b76c15'
down_revision: Union[str, Sequence[str], None] = 'c8b2d4f96e31'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.alter_column('header_contents', 'header_id', nullable=True)


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("DELETE FROM header_contents WHERE header_id IS NULL")
    op.alter_column('header_contents', 'header_id', nullable=False)
