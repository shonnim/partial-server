"""add chunking columns to chunks

Revision ID: b8c5d2e94f17
Revises: a7d3e5f81c29
Create Date: 2026-09-30 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b8c5d2e94f17'
down_revision: Union[str, Sequence[str], None] = 'a7d3e5f81c29'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema.

    The chunks table was created two revisions back on this same branch and
    nothing has written to it yet, so the NOT NULL columns need no backfill.
    """
    # Which strategy made the chunk: the group id the document carried when it
    # was chunked. Kept on the row so a chunk can be told apart from one made
    # by another strategy after the document is regrouped.
    op.add_column('chunks', sa.Column('group_id', sa.String(length=255), nullable=False))
    op.add_column(
        'chunks',
        sa.Column('chunk_type', sa.String(length=20), nullable=False, server_default='text'),
    )
    # The breadcrumb the chunk opens with, on its own for filtering.
    op.add_column('chunks', sa.Column('section_path', sa.Text(), nullable=True))
    op.add_column(
        'chunks',
        sa.Column('token_count', sa.Integer(), nullable=False, server_default='0'),
    )
    op.create_index('ix_chunks_group_id', 'chunks', ['group_id'])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_chunks_group_id', table_name='chunks')
    op.drop_column('chunks', 'token_count')
    op.drop_column('chunks', 'section_path')
    op.drop_column('chunks', 'chunk_type')
    op.drop_column('chunks', 'group_id')
