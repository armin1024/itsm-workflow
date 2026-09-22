from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, LargeBinary, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import JSON

from app.db import Base


JsonType = JSON().with_variant(JSONB, "postgresql")


def utcnow() -> datetime:
    return datetime.now(UTC)


class Knowledge(Base):
    __tablename__ = "knowledge"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    status: Mapped[str] = mapped_column(String(32), index=True, default="DRAFT")
    name: Mapped[str] = mapped_column(String(200))
    summary: Mapped[str] = mapped_column(Text)
    match_phrases: Mapped[list[str]] = mapped_column(JsonType, default=list)
    negative_phrases: Mapped[list[str]] = mapped_column(JsonType, default=list)
    system_keys: Mapped[list[str]] = mapped_column(JsonType, default=list)
    creator_uid: Mapped[str] = mapped_column(String(120), index=True)
    source_type: Mapped[str] = mapped_column(String(32), index=True, default="MANUAL")
    source_ticket_id: Mapped[int | None] = mapped_column(Integer, index=True, nullable=True)
    source_ticket_no: Mapped[str | None] = mapped_column(String(120), index=True, nullable=True)
    public: Mapped[bool] = mapped_column(Boolean, default=False)
    draft_definition: Mapped[dict[str, Any]] = mapped_column(JsonType)
    published_version_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    retrieval_text: Mapped[str] = mapped_column(Text, default="")
    embedding: Mapped[list[float] | None] = mapped_column(JsonType, nullable=True)
    embedding_model: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    submitted_by: Mapped[str | None] = mapped_column(String(120), nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reviewed_by: Mapped[str | None] = mapped_column(String(120), nullable=True)
    review_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_published_by: Mapped[str | None] = mapped_column(String(120), nullable=True)
    origin_environment: Mapped[str | None] = mapped_column(String(200), nullable=True)
    origin_knowledge_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    origin_version_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    origin_content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    import_package_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deleted_by: Mapped[str | None] = mapped_column(String(120), nullable=True)
    users: Mapped[list["KnowledgeUser"]] = relationship(cascade="all, delete-orphan", lazy="selectin")


class KnowledgeUser(Base):
    __tablename__ = "knowledge_users"
    knowledge_id: Mapped[str] = mapped_column(ForeignKey("knowledge.id", ondelete="CASCADE"), primary_key=True)
    uid: Mapped[str] = mapped_column(String(120), primary_key=True, index=True)


class WorkflowVersion(Base):
    __tablename__ = "workflow_versions"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    knowledge_id: Mapped[str] = mapped_column(ForeignKey("knowledge.id"), index=True)
    version_number: Mapped[int] = mapped_column(Integer)
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    definition: Mapped[dict[str, Any]] = mapped_column(JsonType)
    published_by: Mapped[str] = mapped_column(String(120))
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    __table_args__ = (UniqueConstraint("knowledge_id", "version_number"),)


class WorkflowRun(Base):
    __tablename__ = "workflow_runs"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    knowledge_id: Mapped[str] = mapped_column(String(64), index=True)
    workflow_version_id: Mapped[str] = mapped_column(ForeignKey("workflow_versions.id"), index=True)
    ticket_id: Mapped[int] = mapped_column(Integer, index=True)
    initiated_by: Mapped[str] = mapped_column(String(120), index=True)
    status: Mapped[str] = mapped_column(String(40), index=True)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    plan_hash: Mapped[str] = mapped_column(String(64))
    workflow_snapshot: Mapped[dict[str, Any]] = mapped_column(JsonType)
    run_inputs: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict)
    node_statuses: Mapped[dict[str, str]] = mapped_column(JsonType, default=dict)
    output_refs: Mapped[dict[str, str]] = mapped_column(JsonType, default=dict)
    current_node_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    waiting_reason: Mapped[str | None] = mapped_column(String(200), nullable=True)
    resume_payload: Mapped[dict[str, Any] | None] = mapped_column(JsonType, nullable=True)
    lease_owner: Mapped[str | None] = mapped_column(String(120), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class NodeAttempt(Base):
    __tablename__ = "node_attempts"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("workflow_runs.id", ondelete="CASCADE"), index=True)
    node_id: Mapped[str] = mapped_column(String(120), index=True)
    attempt: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(32), index=True)
    command_summary: Mapped[str] = mapped_column(Text)
    cli_version: Mapped[str] = mapped_column(String(120), default="")
    cli_sha256: Mapped[str] = mapped_column(String(64), default="")
    exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    diagnostic_artifact_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    diagnostic_truncated: Mapped[bool] = mapped_column(Boolean, default=False)
    artifact_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    __table_args__ = (UniqueConstraint("run_id", "node_id", "attempt"),)


class WorkflowEvent(Base):
    __tablename__ = "workflow_events"
    sequence: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("workflow_runs.id", ondelete="CASCADE"), index=True)
    node_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    attempt_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    type: Mapped[str] = mapped_column(String(80))
    status: Mapped[str | None] = mapped_column(String(40), nullable=True)
    run_revision: Mapped[int] = mapped_column(Integer, default=1)
    safe_summary: Mapped[str] = mapped_column(Text, default="")
    progress_current: Mapped[int | None] = mapped_column(Integer, nullable=True)
    progress_total: Mapped[int | None] = mapped_column(Integer, nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    __table_args__ = (Index("workflow_events_run_sequence", "run_id", "sequence"),)


class InterruptRecord(Base):
    __tablename__ = "workflow_interrupts"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("workflow_runs.id", ondelete="CASCADE"), index=True)
    node_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    kind: Mapped[str] = mapped_column(String(40))
    status: Mapped[str] = mapped_column(String(24), default="OPEN")
    request_payload: Mapped[dict[str, Any]] = mapped_column(JsonType)
    response_payload: Mapped[dict[str, Any] | None] = mapped_column(JsonType, nullable=True)
    option_artifact_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    response_artifact_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    attempt_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    option_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_by: Mapped[str | None] = mapped_column(String(120), nullable=True)


class EncryptedArtifact(Base):
    __tablename__ = "encrypted_artifacts"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("workflow_runs.id", ondelete="CASCADE"), index=True)
    node_id: Mapped[str] = mapped_column(String(120))
    artifact_type: Mapped[str] = mapped_column(String(32), default="RESULT")
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary)
    content_hash: Mapped[str] = mapped_column(String(64))
    size_bytes: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class KnowledgeLifecycleEvent(Base):
    __tablename__ = "knowledge_lifecycle_events"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    knowledge_id: Mapped[str] = mapped_column(ForeignKey("knowledge.id", ondelete="CASCADE"), index=True)
    workflow_version_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    event_type: Mapped[str] = mapped_column(String(40), index=True)
    actor_uid: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)
    safe_summary: Mapped[str] = mapped_column(Text, default="")
    source: Mapped[str] = mapped_column(String(40), default="APPLICATION")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class KnowledgeTransferAudit(Base):
    __tablename__ = "knowledge_transfer_audits"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    action: Mapped[str] = mapped_column(String(32), index=True)
    operator_uid: Mapped[str] = mapped_column(String(120), index=True)
    package_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    result: Mapped[str] = mapped_column(String(32), default="SUCCEEDED")
    details: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class RunCredential(Base):
    __tablename__ = "run_credentials"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("workflow_runs.id", ondelete="CASCADE"), unique=True)
    uid: Mapped[str] = mapped_column(String(120))
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class Approval(Base):
    __tablename__ = "workflow_approvals"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("workflow_runs.id", ondelete="CASCADE"), index=True)
    node_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    kind: Mapped[str] = mapped_column(String(32))
    decision: Mapped[str] = mapped_column(String(24))
    plan_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    decided_by: Mapped[str] = mapped_column(String(120))
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class WorkflowControlRequest(Base):
    __tablename__ = "workflow_control_requests"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(String(120))
    run_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    action: Mapped[str] = mapped_column(String(64))
    actor_uid: Mapped[str] = mapped_column(String(120), index=True)
    request_hash: Mapped[str] = mapped_column(String(64))
    response_status: Mapped[int] = mapped_column(Integer)
    response_payload: Mapped[dict[str, Any]] = mapped_column(JsonType)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    __table_args__ = (UniqueConstraint("actor_uid", "idempotency_key", "action"),)
