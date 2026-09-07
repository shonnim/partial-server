"""add start_page_idx and end_page_idx to headers

Revision ID: c7e2b9d41a56
Revises: a1c4f6b28d93
Create Date: 2026-09-07 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c7e2b9d41a56'
down_revision: Union[str, Sequence[str], None] = 'a1c4f6b28d93'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('headers', sa.Column('start_page_idx', sa.Integer(), nullable=True))
    op.add_column('headers', sa.Column('end_page_idx', sa.Integer(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('headers', 'end_page_idx')
    op.drop_column('headers', 'start_page_idx')
