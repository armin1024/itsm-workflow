"""Add knowledge source metadata for ticket-derived drafts."""

from alembic import op
import sqlalchemy as sa

revision = "0002_knowledge_sources"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    columns = {item["name"] for item in inspector.get_columns("knowledge")}
    for column in (sa.Column("source_type", sa.String(length=32), nullable=False, server_default="MANUAL"), sa.Column("source_ticket_id", sa.Integer(), nullable=True), sa.Column("source_ticket_no", sa.String(length=120), nullable=True)):
        if column.name not in columns:
            op.add_column("knowledge", column)
    indexes = {item["name"] for item in sa.inspect(op.get_bind()).get_indexes("knowledge")}
    for name, columns in (("ix_knowledge_source_type", ["source_type"]), ("ix_knowledge_source_ticket_id", ["source_ticket_id"]), ("ix_knowledge_source_ticket_no", ["source_ticket_no"])):
        if name not in indexes:
            op.create_index(name, "knowledge", columns)


def downgrade() -> None:
    op.drop_index("ix_knowledge_source_ticket_no", table_name="knowledge")
    op.drop_index("ix_knowledge_source_ticket_id", table_name="knowledge")
    op.drop_index("ix_knowledge_source_type", table_name="knowledge")
    op.drop_column("knowledge", "source_ticket_no")
    op.drop_column("knowledge", "source_ticket_id")
    op.drop_column("knowledge", "source_type")
