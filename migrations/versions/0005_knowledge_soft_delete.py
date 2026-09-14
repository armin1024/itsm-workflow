"""Add recoverable knowledge deletion audit fields."""

from alembic import op
import sqlalchemy as sa

revision = "0005_knowledge_soft_delete"
down_revision = "0004_knowledge_review"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("knowledge", sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("knowledge", sa.Column("deleted_by", sa.String(length=120), nullable=True))


def downgrade() -> None:
    op.drop_column("knowledge", "deleted_by")
    op.drop_column("knowledge", "deleted_at")
