"""Add execution diagnostics, knowledge lifecycle, and transfer provenance."""

from __future__ import annotations

import uuid

from alembic import op
import sqlalchemy as sa


revision = "0007_diagnostics_transfer"
down_revision = "0006_list_search_indexes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for table, definitions in {
        "node_attempts": (sa.Column("error_message", sa.Text(), nullable=True), sa.Column("diagnostic_artifact_id", sa.String(length=64), nullable=True), sa.Column("diagnostic_truncated", sa.Boolean(), nullable=False, server_default=sa.false())),
        "encrypted_artifacts": (sa.Column("artifact_type", sa.String(length=32), nullable=False, server_default="RESULT"),),
        "knowledge": (sa.Column("last_published_at", sa.DateTime(timezone=True), nullable=True), sa.Column("last_published_by", sa.String(length=120), nullable=True), sa.Column("origin_environment", sa.String(length=200), nullable=True), sa.Column("origin_knowledge_id", sa.String(length=64), nullable=True), sa.Column("origin_version_id", sa.String(length=64), nullable=True), sa.Column("origin_content_hash", sa.String(length=64), nullable=True), sa.Column("import_package_id", sa.String(length=64), nullable=True)),
    }.items():
        columns = {item["name"] for item in sa.inspect(op.get_bind()).get_columns(table)}
        for column in definitions:
            if column.name not in columns:
                op.add_column(table, column)
    if "knowledge_lifecycle_events" not in sa.inspect(op.get_bind()).get_table_names():
        op.create_table(
        "knowledge_lifecycle_events",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("knowledge_id", sa.String(length=64), sa.ForeignKey("knowledge.id", ondelete="CASCADE"), nullable=False),
        sa.Column("workflow_version_id", sa.String(length=64), nullable=True),
        sa.Column("event_type", sa.String(length=40), nullable=False),
        sa.Column("actor_uid", sa.String(length=120), nullable=True),
        sa.Column("safe_summary", sa.Text(), nullable=False, server_default=""),
        sa.Column("source", sa.String(length=40), nullable=False, server_default="APPLICATION"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        )
    lifecycle_indexes = {item["name"] for item in sa.inspect(op.get_bind()).get_indexes("knowledge_lifecycle_events")}
    for name, columns in (("ix_knowledge_lifecycle_knowledge", ["knowledge_id", "created_at"]), ("ix_knowledge_lifecycle_version", ["workflow_version_id"]), ("ix_knowledge_lifecycle_type", ["event_type"]), ("ix_knowledge_lifecycle_actor", ["actor_uid"])):
        if name not in lifecycle_indexes:
            op.create_index(name, "knowledge_lifecycle_events", columns)
    if "knowledge_transfer_audits" not in sa.inspect(op.get_bind()).get_table_names():
        op.create_table(
        "knowledge_transfer_audits",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("action", sa.String(length=32), nullable=False),
        sa.Column("operator_uid", sa.String(length=120), nullable=False),
        sa.Column("package_id", sa.String(length=64), nullable=True),
        sa.Column("result", sa.String(length=32), nullable=False, server_default="SUCCEEDED"),
        sa.Column("details", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        )
    transfer_indexes = {item["name"] for item in sa.inspect(op.get_bind()).get_indexes("knowledge_transfer_audits")}
    for name, columns in (("ix_transfer_audit_action", ["action"]), ("ix_transfer_audit_operator", ["operator_uid"]), ("ix_transfer_audit_package", ["package_id"]), ("ix_transfer_audit_created", ["created_at"])):
        if name not in transfer_indexes:
            op.create_index(name, "knowledge_transfer_audits", columns)

    connection = op.get_bind()
    knowledge = sa.table("knowledge", sa.column("id"), sa.column("creator_uid"), sa.column("created_at"), sa.column("updated_at"), sa.column("submitted_at"), sa.column("submitted_by"), sa.column("reviewed_at"), sa.column("reviewed_by"), sa.column("published_version_id"))
    versions = sa.table("workflow_versions", sa.column("id"), sa.column("knowledge_id"), sa.column("published_at"), sa.column("published_by"))
    events = sa.table("knowledge_lifecycle_events", sa.column("id"), sa.column("knowledge_id"), sa.column("workflow_version_id"), sa.column("event_type"), sa.column("actor_uid"), sa.column("safe_summary"), sa.column("source"), sa.column("created_at"))
    for row in connection.execute(sa.select(knowledge)).mappings():
        connection.execute(events.insert().values(id="kle_" + uuid.uuid4().hex, knowledge_id=row["id"], event_type="CREATED", actor_uid=row["creator_uid"], safe_summary="历史知识创建记录", source="MIGRATION", created_at=row["created_at"]))
        if row["submitted_at"]:
            connection.execute(events.insert().values(id="kle_" + uuid.uuid4().hex, knowledge_id=row["id"], event_type="SUBMITTED", actor_uid=row["submitted_by"], safe_summary="历史提交审核记录", source="MIGRATION", created_at=row["submitted_at"]))
    for row in connection.execute(sa.select(versions)).mappings():
        connection.execute(events.insert().values(id="kle_" + uuid.uuid4().hex, knowledge_id=row["knowledge_id"], workflow_version_id=row["id"], event_type="PUBLISHED", actor_uid=row["published_by"], safe_summary="历史版本发布记录", source="MIGRATION", created_at=row["published_at"]))
    connection.execute(sa.text("UPDATE knowledge SET last_published_at = v.published_at, last_published_by = v.published_by FROM workflow_versions v WHERE knowledge.published_version_id = v.id"))


def downgrade() -> None:
    op.drop_table("knowledge_transfer_audits")
    op.drop_table("knowledge_lifecycle_events")
    for column in ("import_package_id", "origin_content_hash", "origin_version_id", "origin_knowledge_id", "origin_environment", "last_published_by", "last_published_at"):
        op.drop_column("knowledge", column)
    op.drop_column("encrypted_artifacts", "artifact_type")
    for column in ("diagnostic_truncated", "diagnostic_artifact_id", "error_message"):
        op.drop_column("node_attempts", column)
