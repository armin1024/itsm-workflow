"""Add encrypted HITL interaction references."""

from alembic import op
import sqlalchemy as sa


revision = "0008_hitl_interactions"
down_revision = "0007_diagnostics_transfer"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {item["name"] for item in sa.inspect(op.get_bind()).get_columns("workflow_interrupts")}
    definitions = (
        sa.Column("option_artifact_id", sa.String(length=64), nullable=True),
        sa.Column("response_artifact_id", sa.String(length=64), nullable=True),
        sa.Column("attempt_id", sa.String(length=64), nullable=True),
        sa.Column("option_count", sa.Integer(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    for column in definitions:
        if column.name not in columns:
            op.add_column("workflow_interrupts", column)
    op.execute("UPDATE workflow_interrupts SET updated_at = created_at WHERE updated_at IS NULL")


def downgrade() -> None:
    for column in ("updated_at", "option_count", "attempt_id", "response_artifact_id", "option_artifact_id"):
        op.drop_column("workflow_interrupts", column)
