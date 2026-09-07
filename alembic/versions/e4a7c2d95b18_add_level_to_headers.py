"""add level to headers

Revision ID: e4a7c2d95b18
Revises: c7e2b9d41a56
Create Date: 2026-09-07 11:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e4a7c2d95b18'
down_revision: Union[str, Sequence[str], None] = 'c7e2b9d41a56'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'headers',
        sa.Column('level', sa.Integer(), nullable=False, server_default='1'),
    )
    # Rows written before this column existed get their depth from the
    # parent chain: a root is 1, a child is one deeper than its parent.
    op.execute(
        """
        WITH RECURSIVE depth AS (
            SELECT id, 1 AS level FROM headers WHERE parent_id IS NULL
            UNION ALL
            SELECT h.id, d.level + 1 FROM headers h JOIN depth d ON h.parent_id = d.id
        )
        UPDATE headers SET level = depth.level FROM depth WHERE headers.id = depth.id
        """
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('headers', 'level')
