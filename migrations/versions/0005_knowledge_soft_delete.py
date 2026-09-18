"""Add recoverable knowledge deletion audit fields."""

from alembic import op
import sqlalchemy as sa

revision = "0005_knowledge_soft_delete"
down_revision = "0004_knowledge_review"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {item["name"] for item in sa.inspect(op.get_bind()).get_columns("knowledge")}
    for column in (sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True), sa.Column("deleted_by", sa.String(length=120), nullable=True)):
        if column.name not in columns:
            op.add_column("knowledge", column)


def downgrade() -> None:
    op.drop_column("knowledge", "deleted_by")
    op.drop_column("knowledge", "deleted_at")
