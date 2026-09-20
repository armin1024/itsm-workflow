from __future__ import annotations

import asyncio
import os
import socket
from datetime import UTC, datetime, timedelta

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.checkpoint.serde.encrypted import EncryptedSerializer
from langgraph.types import Command
from sqlalchemy import delete, select

from app.config import settings
from app.db import SessionLocal, initialize_database
from app.engine import WorkflowEngine
from app.events import emit_event
from app.models import NodeAttempt, RunCredential, WorkflowRun
from app.models import EncryptedArtifact
from app.workflow import WorkflowDefinition


class WorkflowWorker:
    def __init__(self, checkpointer, worker_id: str | None = None):
        self.worker_id = worker_id or f"{socket.gethostname()}:{os.getpid()}"
        self.engine = WorkflowEngine(SessionLocal, checkpointer)

    async def recover_unknown(self) -> None:
        now = datetime.now(UTC)
        async with SessionLocal() as session:
            result = await session.execute(select(WorkflowRun).where(WorkflowRun.status == "RUNNING", WorkflowRun.lease_expires_at < now))
            for run in result.scalars():
                attempt = await session.scalar(select(NodeAttempt).where(NodeAttempt.run_id == run.id, NodeAttempt.status == "STARTED").order_by(NodeAttempt.started_at.desc()))
                run.status, run.waiting_reason = "UNKNOWN", "Worker 在外部操作期间中断，需要人工处理"
                if attempt:
                    attempt.status, attempt.error_code, attempt.finished_at = "UNKNOWN", "WORKER_LOST", now
                    statuses = dict(run.node_statuses)
                    statuses[attempt.node_id] = "UNKNOWN"
                    run.node_statuses = statuses
                await emit_event(session, run, "RUN_UNKNOWN", node_id=attempt.node_id if attempt else None, status="UNKNOWN", summary=run.waiting_reason)
            await session.commit()

    async def claim(self) -> str | None:
        now = datetime.now(UTC)
        async with SessionLocal() as session:
            query = select(WorkflowRun).where(WorkflowRun.status == "QUEUED").order_by(WorkflowRun.created_at).limit(1).with_for_update(skip_locked=True)
            run = await session.scalar(query)
            if not run:
                return None
            run.status, run.lease_owner = "RUNNING", self.worker_id
            run.lease_expires_at = now + timedelta(seconds=settings.worker_lease_seconds)
            await emit_event(session, run, "RUN_STARTED", status="RUNNING", summary="Worker 已领取运行")
            await session.commit()
            return run.id

    async def execute(self, run_id: str) -> None:
        async with SessionLocal() as session:
            run = await session.get(WorkflowRun, run_id)
            definition = WorkflowDefinition.model_validate(run.workflow_snapshot)
            resume_payload = run.resume_payload
            run.resume_payload = None
            is_first = run.started_at is None
            if is_first:
                run.started_at = datetime.now(UTC)
            await session.commit()
        graph = self.engine.compile(definition)
        config = {"configurable": {"thread_id": run_id}}
        graph_input = {"run_id": run_id, "inputs": run.run_inputs, "output_refs": run.output_refs, "routes": {}, "refinements": {}} if is_first else (Command(resume=resume_payload or {}) if resume_payload is not None else None)
        async def heartbeat():
            while True:
                await asyncio.sleep(max(3, settings.worker_lease_seconds // 3))
                async with SessionLocal() as heartbeat_session:
                    active = await heartbeat_session.get(WorkflowRun, run_id)
                    if not active or active.lease_owner != self.worker_id:
                        return
                    active.lease_expires_at = datetime.now(UTC) + timedelta(seconds=settings.worker_lease_seconds)
                    await heartbeat_session.commit()

        heartbeat_task = asyncio.create_task(heartbeat())
        try:
            await graph.ainvoke(graph_input, config=config)
            snapshot = await graph.aget_state(config)
            async with SessionLocal() as session:
                current = await session.get(WorkflowRun, run_id)
                if snapshot.tasks and any(task.interrupts for task in snapshot.tasks):
                    current.lease_owner, current.lease_expires_at = None, None
                elif current.status not in {"FAILED", "CANCELLED", "UNKNOWN", "CANCEL_REQUESTED", "WAITING_INPUT", "WAITING_NODE_APPROVAL", "PAUSED", "WAITING_CREDENTIAL"}:
                    current.status, current.finished_at, current.current_node_id = "SUCCEEDED", datetime.now(UTC), None
                    await emit_event(session, current, "RUN_SUCCEEDED", status="SUCCEEDED", summary="工作流执行完成")
                    credential = await session.scalar(select(RunCredential).where(RunCredential.run_id == run_id))
                    if credential:
                        await session.delete(credential)
                current.lease_owner, current.lease_expires_at = None, None
                await session.commit()
        except Exception as exc:
            async with SessionLocal() as session:
                current = await session.get(WorkflowRun, run_id)
                if current.status == "RUNNING":
                    current.status, current.waiting_reason, current.finished_at = "FAILED", str(exc)[:200], datetime.now(UTC)
                    await emit_event(session, current, "RUN_FAILED", status="FAILED", summary=current.waiting_reason)
                current.lease_owner, current.lease_expires_at = None, None
                await session.commit()
        finally:
            heartbeat_task.cancel()

    async def run_forever(self) -> None:
        await self.recover_unknown()
        running: set[asyncio.Task] = set()
        maintenance_at = 0.0
        while True:
            now = asyncio.get_running_loop().time()
            if now >= maintenance_at:
                await self.recover_unknown()
                await self.cleanup_retention()
                maintenance_at = now + 30
            running = {task for task in running if not task.done()}
            if len(running) < settings.worker_concurrency:
                run_id = await self.claim()
                if run_id:
                    task = asyncio.create_task(self.execute(run_id))
                    running.add(task)
                    continue
            await asyncio.sleep(1)

    async def cleanup_retention(self) -> None:
        now = datetime.now(UTC)
        cutoff = now - timedelta(days=settings.result_retention_days)
        async with SessionLocal() as session:
            await session.execute(delete(EncryptedArtifact).where(EncryptedArtifact.expires_at <= now))
            await session.execute(delete(RunCredential).where(RunCredential.expires_at <= now))
            result = await session.execute(select(WorkflowRun).where(WorkflowRun.finished_at.is_not(None), WorkflowRun.finished_at <= cutoff))
            expired_runs = list(result.scalars())
            for run in expired_runs:
                run.run_inputs = {}
                run.output_refs = {}
            await session.commit()
        for run in expired_runs:
            try:
                await self.engine.checkpointer.adelete_thread(run.id)
            except Exception:
                pass


async def main() -> None:
    settings.validate_production()
    await initialize_database()
    if settings.langgraph_database_url:
        os.environ.setdefault("LANGGRAPH_STRICT_MSGPACK", "true")
        serializer = EncryptedSerializer.from_pycryptodome_aes(key=settings.encryption_key())
        async with AsyncPostgresSaver.from_conn_string(settings.langgraph_database_url, serde=serializer) as checkpointer:
            await checkpointer.setup()
            await WorkflowWorker(checkpointer).run_forever()
    else:
        await WorkflowWorker(InMemorySaver()).run_forever()


if __name__ == "__main__":
    asyncio.run(main())
