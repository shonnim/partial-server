"""add fixing columns to documents

Revision ID: c5a9e2d47f31
Revises: b4f0c7a21d85
Create Date: 2026-11-12 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c5a9e2d47f31'
down_revision: Union[str, Sequence[str], None] = 'b4f0c7a21d85'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # When the Fixing page rewrote this document's two-up PDF into one page
    # per sheet. NULL means the PDF has never been rewritten.
    op.add_column(
        'documents',
        sa.Column('fixed_at', sa.DateTime(timezone=True), nullable=True),
    )
    # Where the Fixing page copied the untouched source PDF before replacing
    # the object at s3_key with the fixed one — kept so a fix is reversible.
    # NULL until the first fix.
    op.add_column(
        'documents',
        sa.Column('original_s3_key', sa.Text(), nullable=True),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('documents', 'original_s3_key')
    op.drop_column('documents', 'fixed_at')
