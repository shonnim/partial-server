"""add text to chunks

Revision ID: d2b7f4a63c98
Revises: c9f6e3a85d21
Create Date: 2026-10-02 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd2b7f4a63c98'
down_revision: Union[str, Sequence[str], None] = 'c9f6e3a85d21'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema.

    ``text`` is the chunk as an LLM should read it, next to ``emb_text``,
    which is shaped for the embedder (the bracketed breadcrumb). Chunks that
    already exist get '' through the server default; the Chunking page has a
    button that derives their value from emb_text and section_path.
    """
    op.add_column(
        'chunks',
        sa.Column('text', sa.Text(), nullable=False, server_default=''),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('chunks', 'text')
