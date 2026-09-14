from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm.attributes import set_committed_value
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import WorkflowEvent, WorkflowRun


TERMINAL_NODE_STATES = {"SUCCEEDED", "FAILED", "SKIPPED", "CANCELLED", "UNKNOWN"}
TERMINAL_RUN_STATES = {"SUCCEEDED", "FAILED", "CANCELLED"}


async def emit_event(
    session: AsyncSession,
    run: WorkflowRun,
    event_type: str,
    *,
    node_id: str | None = None,
    attempt_id: str | None = None,
    status: str | None = None,
    summary: str = "",
    payload: dict[str, Any] | None = None,
) -> WorkflowEvent:
    revision = await session.scalar(
        update(WorkflowRun)
        .where(WorkflowRun.id == run.id)
        .values(revision=WorkflowRun.revision + 1)
        .returning(WorkflowRun.revision)
    )
    if revision is None:
        raise RuntimeError("运行不存在，无法写入事件")
    set_committed_value(run, "revision", int(revision))
    statuses = run.node_statuses or {}
    current = sum(value in TERMINAL_NODE_STATES for value in statuses.values())
    event = WorkflowEvent(
        id="evt_" + uuid.uuid4().hex,
        run_id=run.id,
        node_id=node_id,
        attempt_id=attempt_id,
        type=event_type,
        status=status,
        run_revision=int(revision),
        safe_summary=summary[:2000],
        progress_current=current,
        progress_total=len(statuses),
        payload=payload or {},
    )
    session.add(event)
    await session.flush()
    return event


def event_dict(event: WorkflowEvent) -> dict[str, Any]:
    return {
        "eventId": event.id,
        "sequence": event.sequence,
        "runId": event.run_id,
        "nodeId": event.node_id,
        "attemptId": event.attempt_id,
        "type": event.type,
        "status": event.status,
        "revision": event.run_revision,
        "safeSummary": event.safe_summary,
        "progressCurrent": event.progress_current,
        "progressTotal": event.progress_total,
        "payload": event.payload,
        "timestamp": event.created_at.isoformat(),
    }


async def stream_events(
    session_factory: async_sessionmaker[AsyncSession],
    run_id: str,
    after_sequence: int = 0,
) -> AsyncIterator[str]:
    cursor = after_sequence
    idle = 0
    while True:
        async with session_factory() as session:
            result = await session.execute(select(WorkflowEvent).where(WorkflowEvent.run_id == run_id, WorkflowEvent.sequence > cursor).order_by(WorkflowEvent.sequence).limit(200))
            events = list(result.scalars())
            for event in events:
                cursor = event.sequence
                yield f"id: {cursor}\nevent: {event.type}\ndata: {json.dumps(event_dict(event), ensure_ascii=False)}\n\n"
            run = await session.get(WorkflowRun, run_id)
            terminal = run is None or run.status in TERMINAL_RUN_STATES
        if terminal and not events:
            return
        if events:
            idle = 0
        else:
            idle += 1
            if idle % 15 == 0:
                yield ": heartbeat\n\n"
        await asyncio.sleep(1)
