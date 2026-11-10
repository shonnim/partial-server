"""create groups table

Revision ID: b4f0c7a21d85
Revises: a6e8d4b29f73
Create Date: 2026-11-10 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'b4f0c7a21d85'
down_revision: Union[str, Sequence[str], None] = 'a6e8d4b29f73'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # One row per group id, carrying its hand-written description (the Korean
    # name shown on the Grouping page). Documents keep referring to the id by
    # its string; nothing references this table.
    op.create_table('groups',
    sa.Column('id', postgresql.UUID(as_uuid=True), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('group_id', sa.String(length=255), nullable=False),
    sa.Column('description', sa.Text(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('group_id', name='uq_groups_group_id')
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('groups')
