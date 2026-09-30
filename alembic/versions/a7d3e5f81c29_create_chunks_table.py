"""create chunks table

Revision ID: a7d3e5f81c29
Revises: f2a8c4d17b63
Create Date: 2026-09-29 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'a7d3e5f81c29'
down_revision: Union[str, Sequence[str], None] = 'f2a8c4d17b63'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('chunks',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('doc_id', sa.UUID(), nullable=False),
    sa.Column('emb_text', sa.Text(), nullable=False),
    sa.Column('order', sa.Integer(), nullable=False),
    sa.Column('head_content_ids', postgresql.ARRAY(sa.Integer()), server_default='{}', nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['doc_id'], ['documents.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    # Also the index for "this document's chunks, in order".
    sa.UniqueConstraint('doc_id', 'order', name='uq_chunks_doc_id_order')
    )
    # A sequence counts per table, not per document, so "order" is numbered by
    # a trigger: an insert that leaves it NULL gets the document's next number.
    # The advisory lock makes two concurrent inserts for one document queue up
    # instead of both reading the same MAX.
    op.execute(
        """
        CREATE FUNCTION chunks_assign_order() RETURNS trigger AS $$
        BEGIN
            IF NEW."order" IS NULL THEN
                PERFORM pg_advisory_xact_lock(hashtextextended(NEW.doc_id::text, 0));
                SELECT COALESCE(MAX("order"), 0) + 1 INTO NEW."order"
                FROM chunks
                WHERE doc_id = NEW.doc_id;
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER chunks_assign_order
        BEFORE INSERT ON chunks
        FOR EACH ROW EXECUTE FUNCTION chunks_assign_order()
        """
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('chunks')
    op.execute('DROP FUNCTION chunks_assign_order()')
