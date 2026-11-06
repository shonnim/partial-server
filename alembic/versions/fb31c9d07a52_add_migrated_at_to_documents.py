"""add migrated_at to documents

Revision ID: fb31c9d07a52
Revises: ef10ab471685
Create Date: 2026-10-26 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'fb31c9d07a52'
down_revision: Union[str, Sequence[str], None] = 'ef10ab471685'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # When the Transform page last migrated this document's folder into the
    # RAG files bucket, content list included. NULL means never, or only a
    # partial migration whose parse outputs were missing.
    op.add_column(
        'documents',
        sa.Column('migrated_at', sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('documents', 'migrated_at')
