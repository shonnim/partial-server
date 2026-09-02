"""create parsed_docs table

Revision ID: b3d1c07af425
Revises: 00498a137b1f
Create Date: 2026-09-02 09:41:12.104553

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'b3d1c07af425'
down_revision: Union[str, Sequence[str], None] = '00498a137b1f'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('parsed_docs',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('doc_id', sa.UUID(), nullable=False),
    sa.Column('task_id', sa.String(length=255), nullable=True),
    # document_status already exists (created with the documents table), so this
    # reuses the type instead of creating it.
    sa.Column('status', postgresql.ENUM('PROCESS', 'COMPLETED', 'FAILED', name='document_status', create_type=False), server_default='PROCESS', nullable=False),
    sa.Column('backend', sa.String(length=255), nullable=True),
    sa.Column('bucket', sa.String(length=63), nullable=True),
    sa.Column('folder_path', sa.Text(), nullable=True),
    sa.Column('size_bytes', sa.BigInteger(), nullable=True),
    sa.Column('categories', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['doc_id'], ['documents.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_parsed_docs_doc_id'), 'parsed_docs', ['doc_id'], unique=False)
    op.create_index(op.f('ix_parsed_docs_task_id'), 'parsed_docs', ['task_id'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_parsed_docs_task_id'), table_name='parsed_docs')
    op.drop_index(op.f('ix_parsed_docs_doc_id'), table_name='parsed_docs')
    # document_status is owned by the documents table, so it is left in place.
    op.drop_table('parsed_docs')
