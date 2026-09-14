from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.crypto import canonical_hash
from app.models import WorkflowControlRequest


class IdempotencyConflict(ValueError):
    pass


class IdempotencyInProgress(ValueError):
    pass


async def claim(
    session: AsyncSession,
    *,
    key: str | None,
    action: str,
    actor_uid: str,
    run_id: str | None,
    payload: dict[str, Any],
) -> tuple[WorkflowControlRequest | None, dict[str, Any] | None]:
    if not key:
        return None, None
    key = key.strip()
    if not key or len(key) > 120:
        raise IdempotencyConflict("Idempotency-Key 长度必须为 1 至 120")
    request_hash = canonical_hash(payload)
    existing = await session.scalar(
        select(WorkflowControlRequest).where(
            WorkflowControlRequest.actor_uid == actor_uid,
            WorkflowControlRequest.idempotency_key == key,
            WorkflowControlRequest.action == action,
        )
    )
    if existing:
        if existing.request_hash != request_hash:
            raise IdempotencyConflict("相同幂等键对应了不同请求")
        if existing.response_status == 102:
            raise IdempotencyInProgress("相同请求正在处理中")
        return existing, existing.response_payload
    record = WorkflowControlRequest(
        id="ctl_" + uuid.uuid4().hex,
        idempotency_key=key,
        run_id=run_id,
        action=action,
        actor_uid=actor_uid,
        request_hash=request_hash,
        response_status=102,
        response_payload={},
    )
    session.add(record)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        existing = await session.scalar(
            select(WorkflowControlRequest).where(
                WorkflowControlRequest.actor_uid == actor_uid,
                WorkflowControlRequest.idempotency_key == key,
                WorkflowControlRequest.action == action,
            )
        )
        if not existing or existing.request_hash != request_hash:
            raise IdempotencyConflict("并发请求使用了相同幂等键")
        if existing.response_status == 102:
            raise IdempotencyInProgress("相同请求正在处理中")
        return existing, existing.response_payload
    return record, None


async def complete(session: AsyncSession, record: WorkflowControlRequest | None, response: dict[str, Any]) -> dict[str, Any]:
    if record:
        record.response_status = 200
        record.response_payload = response
        if not record.run_id:
            record.run_id = str(response.get("runId") or "") or None
        await session.commit()
    return response
