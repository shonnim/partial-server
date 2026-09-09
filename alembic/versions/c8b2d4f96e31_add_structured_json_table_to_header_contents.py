"""add structured_json_table to header_contents

Revision ID: c8b2d4f96e31
Revises: a3f7c1e58d24
Create Date: 2026-09-09 11:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'c8b2d4f96e31'
down_revision: Union[str, Sequence[str], None] = 'a3f7c1e58d24'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'header_contents',
        sa.Column('structured_json_table', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('header_contents', 'structured_json_table')
