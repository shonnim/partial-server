"""rename header_contents.text to content, add footnote, drop structured_json_table

Revision ID: e1f4b7c3a950
Revises: d4e9a2b76c15
Create Date: 2026-09-14 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'e1f4b7c3a950'
down_revision: Union[str, Sequence[str], None] = 'd4e9a2b76c15'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.drop_column('header_contents', 'structured_json_table')
    op.alter_column('header_contents', 'text', new_column_name='content')
    op.add_column('header_contents', sa.Column('footnote', sa.Text(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('header_contents', 'footnote')
    op.alter_column('header_contents', 'content', new_column_name='text')
    op.add_column(
        'header_contents',
        sa.Column('structured_json_table', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
