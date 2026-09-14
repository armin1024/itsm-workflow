"""Add knowledge source metadata for ticket-derived drafts."""

from alembic import op
import sqlalchemy as sa

revision = "0002_knowledge_sources"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("knowledge", sa.Column("source_type", sa.String(length=32), nullable=False, server_default="MANUAL"))
    op.add_column("knowledge", sa.Column("source_ticket_id", sa.Integer(), nullable=True))
    op.add_column("knowledge", sa.Column("source_ticket_no", sa.String(length=120), nullable=True))
    op.create_index("ix_knowledge_source_type", "knowledge", ["source_type"])
    op.create_index("ix_knowledge_source_ticket_id", "knowledge", ["source_ticket_id"])
    op.create_index("ix_knowledge_source_ticket_no", "knowledge", ["source_ticket_no"])


def downgrade() -> None:
    op.drop_index("ix_knowledge_source_ticket_no", table_name="knowledge")
    op.drop_index("ix_knowledge_source_ticket_id", table_name="knowledge")
    op.drop_index("ix_knowledge_source_type", table_name="knowledge")
    op.drop_column("knowledge", "source_ticket_no")
    op.drop_column("knowledge", "source_ticket_id")
    op.drop_column("knowledge", "source_type")
