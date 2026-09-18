"""Add knowledge review submission audit fields."""

from alembic import op
import sqlalchemy as sa

revision = "0004_knowledge_review"
down_revision = "0003_mcp_facts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {item["name"] for item in sa.inspect(op.get_bind()).get_columns("knowledge")}
    for column in (sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True), sa.Column("submitted_by", sa.String(length=120), nullable=True), sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True), sa.Column("reviewed_by", sa.String(length=120), nullable=True), sa.Column("review_note", sa.Text(), nullable=True)):
        if column.name not in columns:
            op.add_column("knowledge", column)


def downgrade() -> None:
    op.drop_column("knowledge", "review_note")
    op.drop_column("knowledge", "reviewed_by")
    op.drop_column("knowledge", "reviewed_at")
    op.drop_column("knowledge", "submitted_by")
    op.drop_column("knowledge", "submitted_at")
