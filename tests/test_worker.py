from datetime import UTC, datetime, timedelta

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.crypto import canonical_hash
from app.db import Base
from app.crypto import SecretBox
from app.models import EncryptedArtifact, InterruptRecord, NodeAttempt, WorkflowRun
from app.worker import WorkflowWorker


@pytest.mark.asyncio
async def test_expired_worker_lease_marks_started_action_unknown(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'worker.db'}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with sessions() as session:
        session.add(WorkflowRun(id="run_lost", knowledge_id="knw", workflow_version_id="wfv", ticket_id=1, initiated_by="uid", status="RUNNING", plan_hash=canonical_hash({}), workflow_snapshot={"nodes": []}, run_inputs={}, node_statuses={"sql-1": "RUNNING"}, output_refs={}, lease_owner="dead-worker", lease_expires_at=datetime.now(UTC) - timedelta(minutes=1)))
        session.add(NodeAttempt(id="att_lost", run_id="run_lost", node_id="sql-1", attempt=1, status="STARTED", command_summary="sql_read: 查询"))
        await session.commit()
    monkeypatch.setattr("app.worker.SessionLocal", sessions)
    worker = WorkflowWorker(InMemorySaver(), "replacement-worker")
    await worker.recover_unknown()
    async with sessions() as session:
        run = await session.get(WorkflowRun, "run_lost")
        attempt = await session.get(NodeAttempt, "att_lost")
        assert run.status == "UNKNOWN"
        assert run.node_statuses["sql-1"] == "UNKNOWN"
        assert attempt.status == "UNKNOWN"
        assert attempt.error_code == "WORKER_LOST"
    await engine.dispose()


@pytest.mark.asyncio
async def test_hitl_resume_is_safely_requeued_and_open_artifact_is_retained(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'hitl-worker.db'}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    now = datetime.now(UTC)
    async with sessions() as session:
        session.add(WorkflowRun(id="run_hitl_lost", knowledge_id="knw", workflow_version_id="wfv", ticket_id=1, initiated_by="uid", status="RUNNING", plan_hash=canonical_hash({}), workflow_snapshot={"nodes": [{"id": "choose", "type": "hitl_select"}]}, run_inputs={}, node_statuses={"choose": "WAITING"}, output_refs={}, current_node_id="choose", lease_owner="dead-worker", lease_expires_at=now - timedelta(minutes=1), resume_payload={"interactionId": "int_hitl", "action": "SELECT", "candidateIds": ["candidate_1"]}))
        session.add(EncryptedArtifact(id="art_hitl_options", run_id="run_hitl_lost", node_id="choose", artifact_type="HITL_OPTIONS", ciphertext=SecretBox().seal({"candidates": []}, purpose="artifact:art_hitl_options"), content_hash="0" * 64, size_bytes=2, expires_at=now - timedelta(days=1)))
        session.add(InterruptRecord(id="int_hitl", run_id="run_hitl_lost", node_id="choose", kind="HITL_SELECT", status="RESUME_PENDING", request_payload={}, option_artifact_id="art_hitl_options"))
        await session.commit()
    monkeypatch.setattr("app.worker.SessionLocal", sessions)
    worker = WorkflowWorker(InMemorySaver(), "replacement-worker")
    await worker.recover_unknown()
    await worker.cleanup_retention()
    async with sessions() as session:
        run = await session.get(WorkflowRun, "run_hitl_lost")
        artifact = await session.get(EncryptedArtifact, "art_hitl_options")
        assert run.status == "QUEUED" and run.waiting_reason is None
        assert artifact is not None
    await engine.dispose()
