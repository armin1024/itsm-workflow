"""Add committed run revisions and idempotent control request ledger."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0003_mcp_facts"
down_revision = "0002_knowledge_sources"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "revision" not in {item["name"] for item in inspector.get_columns("workflow_runs")}:
        op.add_column("workflow_runs", sa.Column("revision", sa.Integer(), nullable=False, server_default="1"))
    if "run_revision" not in {item["name"] for item in inspector.get_columns("workflow_events")}:
        op.add_column("workflow_events", sa.Column("run_revision", sa.Integer(), nullable=False, server_default="1"))
    if "workflow_control_requests" not in inspector.get_table_names():
        op.create_table(
        "workflow_control_requests",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("idempotency_key", sa.String(length=120), nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=True),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("actor_uid", sa.String(length=120), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("response_status", sa.Integer(), nullable=False),
        sa.Column("response_payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("actor_uid", "idempotency_key", "action"),
        )
    indexes = {item["name"] for item in sa.inspect(op.get_bind()).get_indexes("workflow_control_requests")}
    if "ix_workflow_control_requests_actor_uid" not in indexes:
        op.create_index("ix_workflow_control_requests_actor_uid", "workflow_control_requests", ["actor_uid"])
    if "ix_workflow_control_requests_run_id" not in indexes:
        op.create_index("ix_workflow_control_requests_run_id", "workflow_control_requests", ["run_id"])


def downgrade() -> None:
    op.drop_index("ix_workflow_control_requests_run_id", table_name="workflow_control_requests")
    op.drop_index("ix_workflow_control_requests_actor_uid", table_name="workflow_control_requests")
    op.drop_table("workflow_control_requests")
    op.drop_column("workflow_events", "run_revision")
    op.drop_column("workflow_runs", "revision")
