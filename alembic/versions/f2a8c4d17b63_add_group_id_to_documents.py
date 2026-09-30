"""add group_id to documents

Revision ID: f2a8c4d17b63
Revises: e1f4b7c3a950
Create Date: 2026-09-28 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f2a8c4d17b63'
down_revision: Union[str, Sequence[str], None] = 'e1f4b7c3a950'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('documents', sa.Column('group_id', sa.String(length=255), nullable=True))
    op.create_index('ix_documents_group_id', 'documents', ['group_id'])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_documents_group_id', table_name='documents')
    op.drop_column('documents', 'group_id')
