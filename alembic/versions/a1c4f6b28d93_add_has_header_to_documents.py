"""add has_header to documents

Revision ID: a1c4f6b28d93
Revises: f820e179af57
Create Date: 2026-09-03 10:12:04.118273

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a1c4f6b28d93'
down_revision: Union[str, Sequence[str], None] = 'f820e179af57'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


has_header = sa.Enum('YES', 'NO', 'UNCHECKED', name='document_has_header')


def upgrade() -> None:
    """Upgrade schema."""
    has_header.create(op.get_bind(), checkfirst=True)
    op.add_column(
        'documents',
        sa.Column(
            'has_header',
            has_header,
            nullable=False,
            server_default='UNCHECKED',
        ),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('documents', 'has_header')
    has_header.drop(op.get_bind(), checkfirst=True)
