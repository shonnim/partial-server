"""rename parsed_docs.categories to category

Revision ID: d5a2e9c14b70
Revises: b3d1c07af425
Create Date: 2026-09-02 10:08:47.291044

"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'd5a2e9c14b70'
down_revision: Union[str, Sequence[str], None] = 'b3d1c07af425'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Matches documents.category; a row carries one category, not a list.
    op.alter_column('parsed_docs', 'categories', new_column_name='category')


def downgrade() -> None:
    """Downgrade schema."""
    op.alter_column('parsed_docs', 'category', new_column_name='categories')
