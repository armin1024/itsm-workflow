from datetime import UTC, datetime, timedelta

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.crypto import canonical_hash
from app.db import Base
from app.models import NodeAttempt, WorkflowRun
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
