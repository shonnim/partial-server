"""add source provenance columns to documents

Revision ID: a6e8d4b29f73
Revises: fb31c9d07a52
Create Date: 2026-11-02 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a6e8d4b29f73'
down_revision: Union[str, Sequence[str], None] = 'fb31c9d07a52'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Provenance fields from the source open-data system. NULL for documents
    # that were uploaded by hand rather than pulled from it.
    op.add_column('documents', sa.Column('document_type', sa.Text(), nullable=True))
    op.add_column('documents', sa.Column('source_document_id', sa.Text(), nullable=True))
    op.add_column('documents', sa.Column('atch_file_id', sa.Text(), nullable=True))
    op.add_column('documents', sa.Column('atch_file_seq', sa.Integer(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('documents', 'atch_file_seq')
    op.drop_column('documents', 'atch_file_id')
    op.drop_column('documents', 'source_document_id')
    op.drop_column('documents', 'document_type')
