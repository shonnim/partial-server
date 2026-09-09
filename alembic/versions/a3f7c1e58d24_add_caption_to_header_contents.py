"""add caption to header_contents

Revision ID: a3f7c1e58d24
Revises: b9e6d3a17c42
Create Date: 2026-09-09 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a3f7c1e58d24'
down_revision: Union[str, Sequence[str], None] = 'b9e6d3a17c42'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('header_contents', sa.Column('caption', sa.Text(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('header_contents', 'caption')
