"""add summary to documents

Revision ID: ef10ab471685
Revises: d2b7f4a63c98
Create Date: 2026-10-06 11:04:11.254903

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'ef10ab471685'
down_revision: Union[str, Sequence[str], None] = 'd2b7f4a63c98'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # The general summary of the whole document, written by the Summarizing
    # run from the level-1 conclusions. NULL until that run has reached it.
    op.add_column('documents', sa.Column('summary', sa.Text(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('documents', 'summary')
