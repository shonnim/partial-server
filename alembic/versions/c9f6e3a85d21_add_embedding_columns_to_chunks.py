"""add embedding columns to chunks

Revision ID: c9f6e3a85d21
Revises: b8c5d2e94f17
Create Date: 2026-10-01 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c9f6e3a85d21'
down_revision: Union[str, Sequence[str], None] = 'b8c5d2e94f17'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema.

    Embedding state lives on the chunk row itself: a re-chunk replaces the
    rows, so the state dies with the chunk it describes, which is what a
    re-chunked document should look like — unembedded again. Rows that
    already exist become 'pending' through the server default.
    """
    op.add_column(
        'chunks',
        sa.Column(
            'embedding_status', sa.String(length=20),
            nullable=False, server_default='pending',
        ),
    )
    op.add_column('chunks', sa.Column('embedding_model', sa.String(length=255), nullable=True))
    op.add_column('chunks', sa.Column('embedded_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('chunks', sa.Column('prompt_tokens', sa.Integer(), nullable=True))
    op.add_column('chunks', sa.Column('embedding_error', sa.Text(), nullable=True))
    # sha256 of emb_text at embedding time, to tell an outdated chunk apart.
    op.add_column('chunks', sa.Column('content_hash', sa.String(length=64), nullable=True))
    # The tree badges aggregate by document and status; the unique constraint
    # on (doc_id, "order") already serves the doc_id lookups.
    op.create_index('ix_chunks_embedding_status', 'chunks', ['embedding_status'])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_chunks_embedding_status', table_name='chunks')
    op.drop_column('chunks', 'content_hash')
    op.drop_column('chunks', 'embedding_error')
    op.drop_column('chunks', 'prompt_tokens')
    op.drop_column('chunks', 'embedded_at')
    op.drop_column('chunks', 'embedding_model')
    op.drop_column('chunks', 'embedding_status')
