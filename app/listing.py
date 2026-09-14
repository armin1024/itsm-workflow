from __future__ import annotations

import math
from datetime import datetime
from typing import Any

from sqlalchemy import String, and_, cast, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Knowledge, KnowledgeUser, WorkflowRun, WorkflowVersion


PAGE_SIZES = {20, 50, 100}
KNOWLEDGE_STATUSES = {"DRAFT", "PENDING_REVIEW", "PUBLISHED"}
RUN_STATUS_GROUPS = {
    "ALL": set(),
    "ACTIVE": {"QUEUED", "RUNNING", "PAUSE_REQUESTED", "CANCEL_REQUESTED"},
    "WAITING": {"WAITING_PLAN_APPROVAL", "WAITING_INPUT", "WAITING_NODE_APPROVAL", "WAITING_CREDENTIAL", "PAUSED", "UNKNOWN"},
    "SUCCEEDED": {"SUCCEEDED"},
    "FAILED": {"FAILED", "CANCELLED"},
}


def validate_page_size(value: int) -> int:
    if value not in PAGE_SIZES:
        raise ValueError("pageSize只允许20、50或100")
    return value


def _like(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _page(total: int, requested: int, page_size: int) -> tuple[int, int, int]:
    total_pages = math.ceil(total / page_size) if total else 0
    page = min(requested, total_pages) if total_pages else 1
    return page, total_pages, (page - 1) * page_size


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _knowledge_summary(record: Knowledge, published_at: datetime | None) -> dict[str, Any]:
    definition = record.draft_definition or {}
    return {
        "knowledgeId": record.id,
        "status": record.status,
        "name": record.name,
        "summary": record.summary,
        "sourceType": record.source_type,
        "sourceTicketId": record.source_ticket_id,
        "sourceTicketNo": record.source_ticket_no,
        "creatorUid": record.creator_uid,
        "visibility": "PUBLIC" if record.public else "RESTRICTED",
        "nodeCount": len(definition.get("nodes") or []),
        "createdAt": record.created_at.isoformat(),
        "updatedAt": record.updated_at.isoformat(),
        "submittedAt": _iso(record.submitted_at),
        "publishedAt": _iso(published_at),
    }


async def paginated_knowledge(
    session: AsyncSession,
    *,
    uid: str,
    is_admin: bool,
    page: int,
    page_size: int,
    keyword: str,
    status: str,
) -> dict[str, Any]:
    conditions = [Knowledge.status != "DELETED"]
    if not is_admin:
        explicitly_allowed = exists(
            select(KnowledgeUser.uid).where(KnowledgeUser.knowledge_id == Knowledge.id, KnowledgeUser.uid == uid)
        )
        conditions.append(
            or_(
                and_(Knowledge.creator_uid == uid, Knowledge.status.in_({"DRAFT", "PENDING_REVIEW"})),
                and_(Knowledge.status == "PUBLISHED", or_(Knowledge.public.is_(True), explicitly_allowed)),
            )
        )
    if status:
        conditions.append(Knowledge.status == status)
    keyword = keyword.strip()
    if keyword:
        pattern = _like(keyword)
        keyword_conditions = [
            Knowledge.id == keyword,
            Knowledge.source_ticket_no == keyword,
            Knowledge.name.ilike(pattern, escape="\\"),
            Knowledge.summary.ilike(pattern, escape="\\"),
            Knowledge.creator_uid.ilike(pattern, escape="\\"),
            cast(Knowledge.match_phrases, String).ilike(pattern, escape="\\"),
        ]
        if keyword.isdecimal():
            keyword_conditions.append(Knowledge.source_ticket_id == int(keyword))
        conditions.append(or_(*keyword_conditions))
    total = int(await session.scalar(select(func.count()).select_from(Knowledge).where(*conditions)) or 0)
    page, total_pages, offset = _page(total, page, page_size)
    query = (
        select(Knowledge, WorkflowVersion.published_at)
        .outerjoin(WorkflowVersion, WorkflowVersion.id == Knowledge.published_version_id)
        .where(*conditions)
        .order_by(Knowledge.updated_at.desc(), Knowledge.id.desc())
        .offset(offset)
        .limit(page_size)
    )
    result = await session.execute(query)
    return {
        "items": [_knowledge_summary(record, published_at) for record, published_at in result.all()],
        "page": page,
        "pageSize": page_size,
        "total": total,
        "totalPages": total_pages,
    }


def _run_summary(run: WorkflowRun, knowledge_name: str | None) -> dict[str, Any]:
    terminal = {"SUCCEEDED", "FAILED", "SKIPPED", "CANCELLED", "UNKNOWN"}
    statuses = run.node_statuses or {}
    return {
        "runId": run.id,
        "knowledgeId": run.knowledge_id,
        "knowledgeName": knowledge_name or run.knowledge_id,
        "ticketId": run.ticket_id,
        "initiatedBy": run.initiated_by,
        "status": run.status,
        "currentNodeId": run.current_node_id,
        "waitingReason": run.waiting_reason,
        "progress": {"current": sum(value in terminal for value in statuses.values()), "total": len(statuses)},
        "revision": run.revision,
        "createdAt": run.created_at.isoformat(),
        "startedAt": _iso(run.started_at),
        "finishedAt": _iso(run.finished_at),
    }


async def paginated_runs(
    session: AsyncSession,
    *,
    uid: str,
    is_admin: bool,
    page: int,
    page_size: int,
    keyword: str,
    status_group: str,
) -> dict[str, Any]:
    conditions = []
    if not is_admin:
        conditions.append(WorkflowRun.initiated_by == uid)
    statuses = RUN_STATUS_GROUPS[status_group]
    if statuses:
        conditions.append(WorkflowRun.status.in_(statuses))
    keyword = keyword.strip()
    if keyword:
        pattern = _like(keyword)
        if keyword.isdecimal():
            conditions.append(WorkflowRun.ticket_id == int(keyword))
        elif keyword.startswith("run_"):
            prefix = keyword.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            conditions.append(or_(WorkflowRun.id == keyword, WorkflowRun.id.ilike(prefix, escape="\\")))
        else:
            conditions.append(or_(WorkflowRun.id.ilike(pattern, escape="\\"), Knowledge.name.ilike(pattern, escape="\\"), WorkflowRun.initiated_by.ilike(pattern, escape="\\")))
    count_query = select(func.count()).select_from(WorkflowRun).outerjoin(Knowledge, Knowledge.id == WorkflowRun.knowledge_id).where(*conditions)
    total = int(await session.scalar(count_query) or 0)
    page, total_pages, offset = _page(total, page, page_size)
    query = (
        select(WorkflowRun, Knowledge.name)
        .outerjoin(Knowledge, Knowledge.id == WorkflowRun.knowledge_id)
        .where(*conditions)
        .order_by(WorkflowRun.created_at.desc(), WorkflowRun.id.desc())
        .offset(offset)
        .limit(page_size)
    )
    result = await session.execute(query)
    return {
        "items": [_run_summary(run, knowledge_name) for run, knowledge_name in result.all()],
        "page": page,
        "pageSize": page_size,
        "total": total,
        "totalPages": total_pages,
    }
