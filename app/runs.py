from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.crypto import SecretBox, canonical_hash
from app.events import emit_event, event_dict
from app.knowledge import authorized_knowledge
from app.models import Approval, InterruptRecord, Knowledge, NodeAttempt, RunCredential, WorkflowEvent, WorkflowRun, WorkflowVersion


def _id(prefix: str) -> str:
    return prefix + uuid.uuid4().hex


async def create_plan(session: AsyncSession, *, knowledge_id: str, ticket_id: int, parameters: dict[str, Any], uid: str, api_key: str) -> WorkflowRun:
    knowledge = await authorized_knowledge(session, knowledge_id, uid)
    version = await session.get(WorkflowVersion, knowledge.published_version_id)
    if not version:
        raise ValueError("经验缺少已发布工作流版本")
    snapshot = version.definition
    plan_source = {"workflowVersionId": version.id, "ticketId": ticket_id, "parameters": parameters, "workflow": snapshot}
    plan_hash = canonical_hash(plan_source)
    run_id = _id("run_")
    statuses = {node["id"]: "PENDING" for node in snapshot["nodes"]}
    run = WorkflowRun(id=run_id, knowledge_id=knowledge_id, workflow_version_id=version.id, ticket_id=ticket_id, initiated_by=uid, status="WAITING_PLAN_APPROVAL", plan_hash=plan_hash, workflow_snapshot=snapshot, run_inputs=parameters, node_statuses=statuses, output_refs={})
    session.add(run)
    # RunCredential has only a scalar run_id and no ORM relationship. Flush the
    # parent explicitly so PostgreSQL never attempts the FK child first.
    await session.flush([run])
    expires = datetime.now(UTC) + timedelta(hours=settings.credential_ttl_hours)
    session.add(RunCredential(id=_id("cred_"), run_id=run_id, uid=uid, ciphertext=SecretBox().seal({"apiKey": api_key}, purpose="run-credential:" + run_id), expires_at=expires))
    await session.flush()
    await emit_event(session, run, "RUN_PLANNED", status=run.status, summary="执行计划已生成，等待确认", payload={"planHash": plan_hash})
    await session.commit()
    return run


async def approve_plan(session: AsyncSession, run: WorkflowRun, uid: str, plan_hash: str) -> None:
    if run.status != "WAITING_PLAN_APPROVAL":
        raise ValueError("当前运行不在计划确认状态")
    if plan_hash != run.plan_hash:
        raise ValueError("计划已变化，请重新查看后确认")
    run.status = "QUEUED"
    session.add(Approval(id=_id("apr_"), run_id=run.id, kind="PLAN", decision="APPROVED", plan_hash=plan_hash, decided_by=uid))
    await emit_event(session, run, "PLAN_APPROVED", status=run.status, summary=f"{uid} 已确认执行计划")
    await session.commit()


async def get_run_for_user(session: AsyncSession, run_id: str, uid: str, is_admin: bool = False) -> WorkflowRun:
    run = await session.get(WorkflowRun, run_id)
    if not run or not is_admin and run.initiated_by != uid:
        raise KeyError(run_id)
    return run


async def serialize_run(session: AsyncSession, run: WorkflowRun, *, include_events: bool = False) -> dict[str, Any]:
    knowledge = await session.get(Knowledge, run.knowledge_id)
    attempts_result = await session.execute(select(NodeAttempt).where(NodeAttempt.run_id == run.id).order_by(NodeAttempt.started_at))
    interrupt_result = await session.execute(select(InterruptRecord).where(InterruptRecord.run_id == run.id).order_by(InterruptRecord.created_at))
    events = []
    if include_events:
        event_result = await session.execute(select(WorkflowEvent).where(WorkflowEvent.run_id == run.id).order_by(WorkflowEvent.sequence.desc()).limit(100))
        events = [event_dict(item) for item in reversed(list(event_result.scalars()))]
    terminal = {"SUCCEEDED", "FAILED", "SKIPPED", "CANCELLED", "UNKNOWN"}
    current = sum(value in terminal for value in (run.node_statuses or {}).values())
    last_event_id = await session.scalar(select(func.max(WorkflowEvent.sequence)).where(WorkflowEvent.run_id == run.id))
    terminal_run = run.status in {"SUCCEEDED", "FAILED", "CANCELLED"}
    return {
        "runId": run.id, "knowledgeId": run.knowledge_id, "knowledgeName": knowledge.name if knowledge else run.knowledge_id, "workflowVersionId": run.workflow_version_id,
        "ticketId": run.ticket_id, "initiatedBy": run.initiated_by, "status": run.status,
        "revision": run.revision, "lastEventId": int(last_event_id or 0), "observedAt": datetime.now(UTC).isoformat(),
        "source": "POSTGRES_COMMITTED_STATE", "stale": False, "terminal": terminal_run, "runPath": "/runs/" + run.id,
        "planHash": run.plan_hash, "workflow": run.workflow_snapshot, "parameters": run.run_inputs,
        "nodeStatuses": run.node_statuses, "currentNodeId": run.current_node_id, "waitingReason": run.waiting_reason,
        "progress": {"current": current, "total": len(run.node_statuses or {})},
        "attempts": [{"attemptId": item.id, "nodeId": item.node_id, "attempt": item.attempt, "status": item.status, "commandSummary": item.command_summary, "cliVersion": item.cli_version, "cliSha256": item.cli_sha256, "exitCode": item.exit_code, "errorCode": item.error_code, "artifactId": item.artifact_id, "startedAt": item.started_at.isoformat(), "finishedAt": item.finished_at.isoformat() if item.finished_at else None} for item in attempts_result.scalars()],
        "interrupts": [{"interruptId": item.id, "nodeId": item.node_id, "kind": item.kind, "status": item.status, "request": item.request_payload, "response": item.response_payload, "createdAt": item.created_at.isoformat()} for item in interrupt_result.scalars()],
        "events": events,
        "createdAt": run.created_at.isoformat(), "startedAt": run.started_at.isoformat() if run.started_at else None,
        "finishedAt": run.finished_at.isoformat() if run.finished_at else None,
    }


async def serialize_run_facts(session: AsyncSession, run: WorkflowRun, events: list[WorkflowEvent] | None = None) -> dict[str, Any]:
    """Compact committed facts for frequent Agent polling."""
    knowledge = await session.get(Knowledge, run.knowledge_id)
    last_event_id = await session.scalar(select(func.max(WorkflowEvent.sequence)).where(WorkflowEvent.run_id == run.id))
    terminal_nodes = {"SUCCEEDED", "FAILED", "SKIPPED", "CANCELLED", "UNKNOWN"}
    statuses = run.node_statuses or {}
    current = sum(value in terminal_nodes for value in statuses.values())
    interrupt_result = await session.execute(
        select(InterruptRecord).where(InterruptRecord.run_id == run.id, InterruptRecord.status == "OPEN").order_by(InterruptRecord.created_at)
    )
    return {
        "runId": run.id,
        "knowledgeId": run.knowledge_id,
        "knowledgeName": knowledge.name if knowledge else run.knowledge_id,
        "workflowVersionId": run.workflow_version_id,
        "ticketId": run.ticket_id,
        "status": run.status,
        "revision": run.revision,
        "lastEventId": int(last_event_id or 0),
        "observedAt": datetime.now(UTC).isoformat(),
        "source": "POSTGRES_COMMITTED_STATE",
        "stale": False,
        "terminal": run.status in {"SUCCEEDED", "FAILED", "CANCELLED"},
        "runPath": "/runs/" + run.id,
        "currentNodeId": run.current_node_id,
        "waitingReason": run.waiting_reason,
        "progress": {"current": current, "total": len(statuses)},
        "nodeStatuses": statuses,
        "resultAvailableNodes": sorted((run.output_refs or {}).keys()),
        "interrupts": [{"interruptId": item.id, "nodeId": item.node_id, "kind": item.kind, "status": item.status, "request": item.request_payload, "createdAt": item.created_at.isoformat()} for item in interrupt_result.scalars()],
        "events": [event_dict(item) for item in (events or [])],
    }


async def update_credential(session: AsyncSession, run: WorkflowRun, uid: str, api_key: str) -> None:
    credential = await session.scalar(select(RunCredential).where(RunCredential.run_id == run.id))
    expires = datetime.now(UTC) + timedelta(hours=settings.credential_ttl_hours)
    ciphertext = SecretBox().seal({"apiKey": api_key}, purpose="run-credential:" + run.id)
    if credential:
        credential.uid, credential.ciphertext, credential.expires_at = uid, ciphertext, expires
    else:
        session.add(RunCredential(id=_id("cred_"), run_id=run.id, uid=uid, ciphertext=ciphertext, expires_at=expires))
    if run.status == "WAITING_CREDENTIAL":
        run.status, run.waiting_reason, run.resume_payload = "QUEUED", None, {"action": "credential_updated"}
    await emit_event(session, run, "CREDENTIAL_UPDATED", status=run.status, summary=f"{uid} 已更新运行凭据")
    await session.commit()
