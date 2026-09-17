"""Add indexes for paginated run and knowledge lists."""

from alembic import op
import sqlalchemy as sa

revision = "0006_list_search_indexes"
down_revision = "0005_knowledge_soft_delete"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for table, definitions in {"workflow_runs": (("ix_workflow_runs_initiator_created", ["initiated_by", "created_at"]), ("ix_workflow_runs_status_created", ["status", "created_at"])), "knowledge": (("ix_knowledge_status_updated", ["status", "updated_at"]), ("ix_knowledge_creator_status_updated", ["creator_uid", "status", "updated_at"]))}.items():
        indexes = {item["name"] for item in sa.inspect(op.get_bind()).get_indexes(table)}
        for name, columns in definitions:
            if name not in indexes:
                op.create_index(name, table, columns)


def downgrade() -> None:
    op.drop_index("ix_knowledge_creator_status_updated", table_name="knowledge")
    op.drop_index("ix_knowledge_status_updated", table_name="knowledge")
    op.drop_index("ix_workflow_runs_status_created", table_name="workflow_runs")
    op.drop_index("ix_workflow_runs_initiator_created", table_name="workflow_runs")
