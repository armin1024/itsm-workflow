from __future__ import annotations

import math
from datetime import datetime
from typing import Any

from sqlalchemy import String, and_, cast, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Knowledge, KnowledgeUser, WorkflowRun, WorkflowVersion

PAGE_SIZES = {20, 50, 100}
KNOWLEDGE_STATUSES = {"DRAFT", "PENDING_REVIEW", "PUBLISHED"}
RUN_STATUSES = {"WAITING_PLAN_APPROVAL", "QUEUED", "RUNNING", "PAUSE_REQUESTED", "PAUSED", "WAITING_INPUT", "WAITING_NODE_APPROVAL", "WAITING_CREDENTIAL", "SUCCEEDED", "FAILED", "CANCEL_REQUESTED", "CANCELLED", "UNKNOWN"}
RUN_STATUS_GROUPS = {
    "ALL": set(), "ACTIVE": {"QUEUED", "RUNNING", "PAUSE_REQUESTED", "CANCEL_REQUESTED"},
    "WAITING": {"WAITING_PLAN_APPROVAL", "WAITING_INPUT", "WAITING_NODE_APPROVAL", "WAITING_CREDENTIAL", "PAUSED", "UNKNOWN"},
    "SUCCEEDED": {"SUCCEEDED"}, "FAILED": {"FAILED", "CANCELLED"},
}


def validate_page_size(value: int) -> int:
    if value not in PAGE_SIZES:
        raise ValueError("pageSize只允许20、50或100")
    return value


def split_values(values: list[str] | None) -> list[str]:
    return list(dict.fromkeys(part.strip() for value in (values or []) for part in value.split(",") if part.strip()))


def _like(value: str) -> str:
    return "%" + value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def _page(total: int, requested: int, page_size: int) -> tuple[int, int, int]:
    total_pages = math.ceil(total / page_size) if total else 0
    page = min(requested, total_pages) if total_pages else 1
    return page, total_pages, (page - 1) * page_size


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _range(conditions: list[Any], column: Any, start: datetime | None, end: datetime | None) -> None:
    if start:
        conditions.append(column >= start)
    if end:
        conditions.append(column < end)


def _json_element(column: Any, value: str) -> Any:
    escaped = value.replace('"', '\\"').replace("%", "\\%").replace("_", "\\_")
    return cast(column, String).like(f'%"{escaped}"%', escape="\\")


def _knowledge_summary(record: Knowledge, published_at: datetime | None) -> dict[str, Any]:
    definition = record.draft_definition or {}
    return {
        "knowledgeId": record.id, "status": record.status, "name": record.name, "summary": record.summary,
        "sourceType": record.source_type, "sourceTicketId": record.source_ticket_id, "sourceTicketNo": record.source_ticket_no,
        "creatorUid": record.creator_uid, "visibility": "PUBLIC" if record.public else "RESTRICTED",
        "nodeCount": len(definition.get("nodes") or []), "publishedVersionId": record.published_version_id,
        "createdAt": _iso(record.created_at), "updatedAt": _iso(record.updated_at), "submittedAt": _iso(record.submitted_at),
        "reviewedAt": _iso(record.reviewed_at), "lastPublishedAt": _iso(record.last_published_at or published_at),
        "publishedAt": _iso(record.last_published_at or published_at),
    }


async def paginated_knowledge(session: AsyncSession, *, uid: str, is_admin: bool, page: int, page_size: int, keyword: str, status: str = "", filters: dict[str, Any] | None = None) -> dict[str, Any]:
    filters = dict(filters or {})
    conditions = [Knowledge.status != "DELETED"]
    if not is_admin:
        allowed = exists(select(KnowledgeUser.uid).where(KnowledgeUser.knowledge_id == Knowledge.id, KnowledgeUser.uid == uid))
        conditions.append(or_(and_(Knowledge.creator_uid == uid, Knowledge.status.in_({"DRAFT", "PENDING_REVIEW"})), and_(Knowledge.status == "PUBLISHED", or_(Knowledge.public.is_(True), allowed))))
    statuses = split_values(filters.get("status") or ([status] if status else []))
    if statuses:
        conditions.append(Knowledge.status.in_(statuses))
    exact = {"knowledgeId": Knowledge.id, "nameExact": Knowledge.name, "summaryExact": Knowledge.summary, "creatorUid": Knowledge.creator_uid, "sourceType": Knowledge.source_type, "sourceTicketNo": Knowledge.source_ticket_no, "publishedVersionId": Knowledge.published_version_id}
    for key, column in exact.items():
        values = split_values(filters.get(key))
        if values:
            conditions.append(column.in_(values))
    tickets = split_values(filters.get("sourceTicketId"))
    if tickets:
        conditions.append(Knowledge.source_ticket_id.in_([int(value) for value in tickets]))
    visibility = split_values(filters.get("visibility"))
    if visibility:
        conditions.append(Knowledge.public.in_({value.upper() == "PUBLIC" for value in visibility}))
    authorized = split_values(filters.get("authorizedUid"))
    if authorized:
        conditions.append(exists(select(KnowledgeUser.uid).where(KnowledgeUser.knowledge_id == Knowledge.id, KnowledgeUser.uid.in_(authorized))))
    for key, column in (("matchPhrase", Knowledge.match_phrases), ("negativePhrase", Knowledge.negative_phrases), ("systemKey", Knowledge.system_keys)):
        values = split_values(filters.get(key))
        if values:
            conditions.append(or_(*[_json_element(column, value) for value in values]))
    _range(conditions, Knowledge.created_at, filters.get("createdFrom"), filters.get("createdTo"))
    _range(conditions, Knowledge.updated_at, filters.get("updatedFrom"), filters.get("updatedTo"))
    _range(conditions, Knowledge.submitted_at, filters.get("submittedFrom"), filters.get("submittedTo"))
    _range(conditions, Knowledge.last_published_at, filters.get("publishedFrom"), filters.get("publishedTo"))
    keyword = keyword.strip()
    if keyword:
        pattern = _like(keyword)
        keyword_conditions = [Knowledge.id == keyword, Knowledge.source_ticket_no == keyword, Knowledge.name.ilike(pattern, escape="\\"), Knowledge.summary.ilike(pattern, escape="\\"), Knowledge.creator_uid.ilike(pattern, escape="\\"), cast(Knowledge.match_phrases, String).ilike(pattern, escape="\\")]
        if keyword.isdecimal():
            keyword_conditions.append(Knowledge.source_ticket_id == int(keyword))
        conditions.append(or_(*keyword_conditions))
    total = int(await session.scalar(select(func.count()).select_from(Knowledge).where(*conditions)) or 0)
    page, total_pages, offset = _page(total, page, page_size)
    result = await session.execute(select(Knowledge, WorkflowVersion.published_at).outerjoin(WorkflowVersion, WorkflowVersion.id == Knowledge.published_version_id).where(*conditions).order_by(Knowledge.updated_at.desc(), Knowledge.id.desc()).offset(offset).limit(page_size))
    applied = {key: value for key, value in {"keyword": keyword, **filters, "status": statuses}.items() if value not in (None, "", [], {})}
    return {"items": [_knowledge_summary(record, published_at) for record, published_at in result.all()], "page": page, "pageSize": page_size, "total": total, "totalPages": total_pages, "appliedFilters": applied}


def _run_summary(run: WorkflowRun, knowledge_name: str | None) -> dict[str, Any]:
    terminal = {"SUCCEEDED", "FAILED", "SKIPPED", "CANCELLED", "UNKNOWN"}
    statuses = run.node_statuses or {}
    return {"runId": run.id, "knowledgeId": run.knowledge_id, "knowledgeName": knowledge_name or run.knowledge_id, "workflowVersionId": run.workflow_version_id, "ticketId": run.ticket_id, "initiatedBy": run.initiated_by, "status": run.status, "currentNodeId": run.current_node_id, "waitingReason": run.waiting_reason, "progress": {"current": sum(value in terminal for value in statuses.values()), "total": len(statuses)}, "revision": run.revision, "createdAt": _iso(run.created_at), "startedAt": _iso(run.started_at), "finishedAt": _iso(run.finished_at)}


async def paginated_runs(session: AsyncSession, *, uid: str, is_admin: bool, page: int, page_size: int, keyword: str, status_group: str, filters: dict[str, Any] | None = None) -> dict[str, Any]:
    filters = dict(filters or {})
    conditions: list[Any] = []
    if not is_admin:
        conditions.append(WorkflowRun.initiated_by == uid)
    statuses = split_values(filters.get("status"))
    if statuses:
        conditions.append(WorkflowRun.status.in_(statuses))
    elif RUN_STATUS_GROUPS[status_group]:
        conditions.append(WorkflowRun.status.in_(RUN_STATUS_GROUPS[status_group]))
    exact = {"runId": WorkflowRun.id, "knowledgeId": WorkflowRun.knowledge_id, "workflowVersionId": WorkflowRun.workflow_version_id, "initiatedBy": WorkflowRun.initiated_by, "currentNodeId": WorkflowRun.current_node_id}
    for key, column in exact.items():
        values = split_values(filters.get(key))
        if values:
            conditions.append(column.in_(values))
    tickets = split_values(filters.get("ticketId"))
    if tickets:
        conditions.append(WorkflowRun.ticket_id.in_([int(value) for value in tickets]))
    _range(conditions, WorkflowRun.created_at, filters.get("createdFrom"), filters.get("createdTo"))
    _range(conditions, WorkflowRun.started_at, filters.get("startedFrom"), filters.get("startedTo"))
    _range(conditions, WorkflowRun.finished_at, filters.get("finishedFrom"), filters.get("finishedTo"))
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
    total = int(await session.scalar(select(func.count()).select_from(WorkflowRun).outerjoin(Knowledge, Knowledge.id == WorkflowRun.knowledge_id).where(*conditions)) or 0)
    page, total_pages, offset = _page(total, page, page_size)
    result = await session.execute(select(WorkflowRun, Knowledge.name).outerjoin(Knowledge, Knowledge.id == WorkflowRun.knowledge_id).where(*conditions).order_by(WorkflowRun.created_at.desc(), WorkflowRun.id.desc()).offset(offset).limit(page_size))
    applied = {key: value for key, value in {"keyword": keyword, "statusGroup": status_group, **filters}.items() if value not in (None, "", [], {}, "ALL")}
    return {"items": [_run_summary(run, name) for run, name in result.all()], "page": page, "pageSize": page_size, "total": total, "totalPages": total_pages, "appliedFilters": applied}


async def paginated_versions(session: AsyncSession, *, uid: str, is_admin: bool, page: int, page_size: int, filters: dict[str, Any]) -> dict[str, Any]:
    conditions: list[Any] = [Knowledge.status != "DELETED"]
    if not is_admin:
        allowed = exists(select(KnowledgeUser.uid).where(KnowledgeUser.knowledge_id == Knowledge.id, KnowledgeUser.uid == uid))
        conditions.extend([Knowledge.status == "PUBLISHED", or_(Knowledge.public.is_(True), allowed)])
    exact = {"workflowVersionId": WorkflowVersion.id, "knowledgeId": WorkflowVersion.knowledge_id, "contentHash": WorkflowVersion.content_hash, "publishedBy": WorkflowVersion.published_by}
    for key, column in exact.items():
        values = split_values(filters.get(key))
        if values:
            conditions.append(column.in_(values))
    numbers = split_values(filters.get("versionNumber"))
    if numbers:
        conditions.append(WorkflowVersion.version_number.in_([int(value) for value in numbers]))
    _range(conditions, WorkflowVersion.published_at, filters.get("publishedFrom"), filters.get("publishedTo"))
    total = int(await session.scalar(select(func.count()).select_from(WorkflowVersion).join(Knowledge, Knowledge.id == WorkflowVersion.knowledge_id).where(*conditions)) or 0)
    page, total_pages, offset = _page(total, page, page_size)
    result = await session.execute(select(WorkflowVersion, Knowledge.name, Knowledge.published_version_id).join(Knowledge, Knowledge.id == WorkflowVersion.knowledge_id).where(*conditions).order_by(WorkflowVersion.published_at.desc()).offset(offset).limit(page_size))
    items = [{"workflowVersionId": version.id, "knowledgeId": version.knowledge_id, "knowledgeName": name, "versionNumber": version.version_number, "contentHash": version.content_hash, "publishedBy": version.published_by, "publishedAt": _iso(version.published_at), "current": current_id == version.id} for version, name, current_id in result.all()]
    return {"items": items, "page": page, "pageSize": page_size, "total": total, "totalPages": total_pages, "appliedFilters": {key: value for key, value in filters.items() if value not in (None, "", [], {})}}
