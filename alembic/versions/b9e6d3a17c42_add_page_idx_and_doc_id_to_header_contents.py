"""add page_idx and doc_id to header_contents

Revision ID: b9e6d3a17c42
Revises: e4a7c2d95b18
Create Date: 2026-09-08 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'b9e6d3a17c42'
down_revision: Union[str, Sequence[str], None] = 'e4a7c2d95b18'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('header_contents', sa.Column('page_idx', sa.Integer(), nullable=True))
    op.add_column(
        'header_contents',
        sa.Column('doc_id', postgresql.UUID(as_uuid=True), nullable=True),
    )
    # Backfill doc_id from the owning header so the NOT NULL constraint holds.
    op.execute(
        """
        UPDATE header_contents AS hc
        SET doc_id = h.doc_id
        FROM headers AS h
        WHERE hc.header_id = h.id
        """
    )
    op.alter_column('header_contents', 'doc_id', nullable=False)
    op.create_foreign_key(
        op.f('fk_header_contents_doc_id_documents'),
        'header_contents',
        'documents',
        ['doc_id'],
        ['id'],
        ondelete='CASCADE',
    )
    op.create_index(op.f('ix_header_contents_doc_id'), 'header_contents', ['doc_id'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_header_contents_doc_id'), table_name='header_contents')
    op.drop_constraint(op.f('fk_header_contents_doc_id_documents'), 'header_contents', type_='foreignkey')
    op.drop_column('header_contents', 'doc_id')
    op.drop_column('header_contents', 'page_idx')
