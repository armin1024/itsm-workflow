"""Add indexes for paginated run and knowledge lists."""

from alembic import op

revision = "0006_list_search_indexes"
down_revision = "0005_knowledge_soft_delete"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index("ix_workflow_runs_initiator_created", "workflow_runs", ["initiated_by", "created_at"])
    op.create_index("ix_workflow_runs_status_created", "workflow_runs", ["status", "created_at"])
    op.create_index("ix_knowledge_status_updated", "knowledge", ["status", "updated_at"])
    op.create_index("ix_knowledge_creator_status_updated", "knowledge", ["creator_uid", "status", "updated_at"])


def downgrade() -> None:
    op.drop_index("ix_knowledge_creator_status_updated", table_name="knowledge")
    op.drop_index("ix_knowledge_status_updated", table_name="knowledge")
    op.drop_index("ix_workflow_runs_status_created", table_name="workflow_runs")
    op.drop_index("ix_workflow_runs_initiator_created", table_name="workflow_runs")
