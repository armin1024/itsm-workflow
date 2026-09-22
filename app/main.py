from __future__ import annotations

import asyncio
import json
import secrets
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import delete, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import __version__
from app.auth import Principal, create_session_cookie, current_principal, identity, require_admin, require_operator
from app.cli import CliExecutionError, inspect_cli
from app.config import settings
from app.crypto import SecretBox, sha256_bytes
from app.db import SessionLocal, get_session, initialize_database
from app.events import emit_event, event_dict, stream_events
from app.idempotency import IdempotencyConflict, IdempotencyInProgress, abandon as abandon_idempotency, claim as claim_idempotency, complete as complete_idempotency
from app.listing import KNOWLEDGE_STATUSES, RUN_STATUSES, RUN_STATUS_GROUPS, paginated_knowledge, paginated_runs, paginated_versions, split_values, validate_page_size
from app.knowledge import authorized_knowledge, create_knowledge, delete_knowledge as soft_delete_knowledge, import_legacy_package, match_knowledge, publish_knowledge, reject_knowledge_review, serialize_knowledge, submit_knowledge_review, update_knowledge
from app.extraction import extract_ticket_draft
from app.hitl import HitlError, validate_form
from app.node_types import NODE_TYPES
from app.models import EncryptedArtifact, InterruptRecord, Knowledge, KnowledgeLifecycleEvent, KnowledgeTransferAudit, NodeAttempt, RunCredential, WorkflowEvent, WorkflowRun, WorkflowVersion
from app.runs import approve_plan, create_plan, get_run_for_user, serialize_run, serialize_run_facts, update_credential
from app.schemas import ApproveRequest, CredentialRequest, KnowledgeCreate, KnowledgeExtractRequest, KnowledgeUpdate, LegacyImportRequest, MatchRequest, PlanRequest, ResumeRequest, RetryRequest, ReviewRejectRequest, SessionRequest
from app.transfer import export_package, export_preview, import_package, import_preview, replace_paths, replace_preview


@asynccontextmanager
async def lifespan(_: FastAPI):
    settings.validate_production()
    await initialize_database()
    yield


app = FastAPI(title="ITSM Workflow", version=__version__, lifespan=lifespan)


def _operator(principal: Principal = Depends(current_principal)) -> Principal:
    return require_operator(principal)


def _admin(principal: Principal = Depends(current_principal)) -> Principal:
    return require_admin(principal)


async def _reviewer(
    request: Request,
    authorization: str | None = Header(default=None),
    x_aops_api_key: str | None = Header(default=None, alias="X-AOPS-Api-Key"),
) -> Principal:
    expected = "Bearer " + settings.workflow_review_token
    if settings.workflow_review_token and authorization and secrets.compare_digest(authorization, expected):
        return Principal(settings.workflow_review_actor_uid, "", True, False)
    principal = await current_principal(
        request=request,
        authorization=authorization,
        x_aops_api_key=x_aops_api_key,
        workflow_session=request.cookies.get("workflow_session"),
    )
    return require_admin(principal)


async def _idempotency(
    session: AsyncSession,
    *,
    key: str | None,
    action: str,
    principal: Principal,
    run_id: str | None,
    payload: dict[str, Any],
):
    try:
        return await claim_idempotency(session, key=key, action=action, actor_uid=principal.uid, run_id=run_id, payload=payload)
    except IdempotencyConflict as exc:
        raise HTTPException(409, f"IDEMPOTENCY_CONFLICT：{exc}") from exc
    except IdempotencyInProgress as exc:
        raise HTTPException(409, f"IDEMPOTENCY_IN_PROGRESS：{exc}") from exc


@app.get("/api/v1/health")
async def health() -> dict[str, Any]:
    cli: dict[str, Any]
    try:
        metadata = await inspect_cli()
        cli = {"status": "ok", "version": metadata.version, "sha256": metadata.sha256}
    except CliExecutionError as exc:
        cli = {"status": "unavailable", "errorCode": exc.code, "message": str(exc)}
    return {"status": "ok", "version": __version__, "environment": settings.environment, "cli": cli}


@app.get("/metrics", response_class=PlainTextResponse)
async def metrics(session: AsyncSession = Depends(get_session)) -> str:
    result = await session.execute(select(WorkflowRun.status, func.count()).group_by(WorkflowRun.status))
    rows = list(result.all())
    lines = ["# HELP itsm_workflow_runs Workflow runs by status", "# TYPE itsm_workflow_runs gauge"]
    lines.extend(f'itsm_workflow_runs{{status="{status}"}} {count}' for status, count in rows)
    queued = sum(int(count) for status, count in rows if status == "QUEUED")
    waiting = sum(int(count) for status, count in rows if str(status).startswith("WAITING") or status in {"PAUSED", "UNKNOWN"})
    lines.extend(["# HELP itsm_workflow_queue_depth Runs waiting for a worker", "# TYPE itsm_workflow_queue_depth gauge", f"itsm_workflow_queue_depth {queued}", "# HELP itsm_workflow_waiting Runs waiting for human action", "# TYPE itsm_workflow_waiting gauge", f"itsm_workflow_waiting {waiting}"])
    return "\n".join(lines) + "\n"


@app.post("/api/v1/auth/session")
async def login(body: SessionRequest, response: Response) -> dict[str, Any]:
    profile = await identity.resolve(body.apiKey)
    uid = profile["uid"]
    if uid not in settings.operator_uids and uid not in settings.admin_uids:
        raise HTTPException(403, "当前 UID 不在平台 allowlist")
    response.set_cookie("workflow_session", create_session_cookie(uid, body.apiKey), max_age=settings.credential_ttl_hours * 3600, httponly=True, secure=settings.session_cookie_secure, samesite="strict")
    return {"uid": uid, "isAdmin": uid in settings.admin_uids, "isOperator": uid in settings.operator_uids}


@app.delete("/api/v1/auth/session", status_code=204)
async def logout(response: Response) -> None:
    response.delete_cookie("workflow_session")


@app.get("/api/v1/auth/me")
async def me(principal: Principal = Depends(current_principal)) -> dict[str, Any]:
    return {"uid": principal.uid, "isAdmin": principal.is_admin, "isOperator": principal.is_operator}


@app.post("/api/v1/knowledge")
async def knowledge_create(body: KnowledgeCreate, principal: Principal = Depends(_admin), session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    record = await create_knowledge(session, body, principal.uid)
    return serialize_knowledge(record, body.uids)


@app.post("/api/v1/knowledge/extract")
async def knowledge_extract(body: KnowledgeExtractRequest, principal: Principal = Depends(_operator), session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    try:
        record, diagnostics = await extract_ticket_draft(session, ticket_id=body.ticketId, uids=body.uids, creator_uid=principal.uid, api_key=principal.api_key)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return {"knowledge": serialize_knowledge(record, body.uids), "extraction": diagnostics}


@app.patch("/api/v1/knowledge/{knowledge_id}")
async def knowledge_update(knowledge_id: str, body: KnowledgeUpdate, principal: Principal = Depends(_operator), session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    record = await session.get(Knowledge, knowledge_id)
    if not record or record.status == "DELETED" or not principal.is_admin and record.creator_uid != principal.uid:
        raise HTTPException(404, "经验不存在")
    if not principal.is_admin and record.status != "DRAFT":
        raise HTTPException(409, "已提交审核的经验不能再编辑")
    record = await update_knowledge(session, record, body, principal.uid)
    return serialize_knowledge(record)


@app.delete("/api/v1/knowledge/{knowledge_id}")
async def knowledge_delete(knowledge_id: str, principal: Principal = Depends(_operator), session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    record = await session.get(Knowledge, knowledge_id)
    if not record or record.status == "DELETED":
        raise HTTPException(404, "经验不存在")
    if not principal.is_admin and (record.creator_uid != principal.uid or record.status != "DRAFT"):
        raise HTTPException(403, "只能删除自己尚未提交的草稿")
    record = await soft_delete_knowledge(session, record, principal.uid)
    return {"knowledgeId": record.id, "status": record.status, "deletedAt": record.deleted_at.isoformat(), "deletedBy": record.deleted_by}


@app.get("/api/v1/node-types")
async def node_type_list(principal: Principal = Depends(current_principal)) -> dict[str, Any]:
    return {"items": [{"type": item.type, "schemaVersion": item.schema_version, "riskLevel": item.risk_level, "configSchema": item.config_schema, "inputTypes": list(item.input_types), "outputSchema": item.output_schema, "enabledForAuthoring": item.type in {"sql_read", "condition", "hitl_select", "hitl_form", "end"}} for item in NODE_TYPES.values()]}


@app.get("/api/v1/knowledge")
async def knowledge_list(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, alias="pageSize"),
    keyword: str = Query(default="", max_length=200),
    status: list[str] = Query(default=[]),
    knowledge_id: list[str] = Query(default=[], alias="knowledgeId"),
    name_exact: list[str] = Query(default=[], alias="nameExact"),
    summary_exact: list[str] = Query(default=[], alias="summaryExact"),
    match_phrase: list[str] = Query(default=[], alias="matchPhrase"),
    negative_phrase: list[str] = Query(default=[], alias="negativePhrase"),
    creator_uid: list[str] = Query(default=[], alias="creatorUid"),
    authorized_uid: list[str] = Query(default=[], alias="authorizedUid"),
    visibility: list[str] = Query(default=[]),
    source_type: list[str] = Query(default=[], alias="sourceType"),
    source_ticket_id: list[str] = Query(default=[], alias="sourceTicketId"),
    source_ticket_no: list[str] = Query(default=[], alias="sourceTicketNo"),
    published_version_id: list[str] = Query(default=[], alias="publishedVersionId"),
    system_key: list[str] = Query(default=[], alias="systemKey"),
    created_from: datetime | None = Query(default=None, alias="createdFrom"), created_to: datetime | None = Query(default=None, alias="createdTo"),
    updated_from: datetime | None = Query(default=None, alias="updatedFrom"), updated_to: datetime | None = Query(default=None, alias="updatedTo"),
    submitted_from: datetime | None = Query(default=None, alias="submittedFrom"), submitted_to: datetime | None = Query(default=None, alias="submittedTo"),
    published_from: datetime | None = Query(default=None, alias="publishedFrom"), published_to: datetime | None = Query(default=None, alias="publishedTo"),
    principal: Principal = Depends(current_principal),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    try:
        page_size = validate_page_size(page_size)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    statuses = [value.upper() for value in split_values(status)]
    if any(value not in KNOWLEDGE_STATUSES for value in statuses):
        raise HTTPException(422, "status只允许DRAFT、PENDING_REVIEW或PUBLISHED")
    if authorized_uid and not principal.is_admin:
        raise HTTPException(403, "authorizedUid仅管理员可查询")
    filters = {"status": statuses, "knowledgeId": knowledge_id, "nameExact": name_exact, "summaryExact": summary_exact, "matchPhrase": match_phrase, "negativePhrase": negative_phrase, "creatorUid": creator_uid, "authorizedUid": authorized_uid, "visibility": visibility, "sourceType": source_type, "sourceTicketId": source_ticket_id, "sourceTicketNo": source_ticket_no, "publishedVersionId": published_version_id, "systemKey": system_key, "createdFrom": created_from, "createdTo": created_to, "updatedFrom": updated_from, "updatedTo": updated_to, "submittedFrom": submitted_from, "submittedTo": submitted_to, "publishedFrom": published_from, "publishedTo": published_to}
    try:
        return await paginated_knowledge(session, uid=principal.uid, is_admin=principal.is_admin, page=page, page_size=page_size, keyword=keyword, filters=filters)
    except ValueError as exc:
        raise HTTPException(422, f"精确查询参数无效：{exc}") from exc


@app.get("/api/v1/knowledge/{knowledge_id}")
async def knowledge_get(knowledge_id: str, principal: Principal = Depends(current_principal), session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    try:
        record = await authorized_knowledge(session, knowledge_id, principal.uid, published_only=not principal.is_admin, bypass_visibility=principal.is_admin, allow_owned_draft=True)
    except KeyError as exc:
        raise HTTPException(404, "经验不存在") from exc
    result = await session.execute(select(KnowledgeLifecycleEvent).where(KnowledgeLifecycleEvent.knowledge_id == knowledge_id).order_by(KnowledgeLifecycleEvent.created_at.desc()))
    lifecycle = [{"eventId": item.id, "eventType": item.event_type, "workflowVersionId": item.workflow_version_id, "actorUid": item.actor_uid, "summary": item.safe_summary, "source": item.source, "createdAt": item.created_at.isoformat()} for item in result.scalars()]
    return {**serialize_knowledge(record), "lifecycle": lifecycle}


@app.post("/api/v1/knowledge/{knowledge_id}/submit-review")
async def knowledge_submit_review(knowledge_id: str, principal: Principal = Depends(_operator), session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    record = await session.get(Knowledge, knowledge_id)
    if not record or not principal.is_admin and record.creator_uid != principal.uid:
        raise HTTPException(404, "经验不存在")
    try:
        record = await submit_knowledge_review(session, record, principal.uid)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return serialize_knowledge(record)


@app.post("/api/v1/knowledge/{knowledge_id}/publish")
async def knowledge_publish(knowledge_id: str, principal: Principal = Depends(_reviewer), session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    try:
        version = await publish_knowledge(session, knowledge_id, principal.uid)
    except KeyError as exc:
        raise HTTPException(404, "经验不存在") from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"knowledgeId": knowledge_id, "workflowVersionId": version.id, "version": version.version_number, "contentHash": version.content_hash, "publishedAt": version.published_at.isoformat()}


@app.get("/api/v1/reviews/knowledge")
async def knowledge_review_list(
    page: int = Query(default=1, ge=1), page_size: int = Query(default=20, alias="pageSize"), knowledge_id: list[str] = Query(default=[], alias="knowledgeId"),
    creator_uid: list[str] = Query(default=[], alias="creatorUid"), source_ticket_id: list[str] = Query(default=[], alias="sourceTicketId"),
    source_ticket_no: list[str] = Query(default=[], alias="sourceTicketNo"), submitted_from: datetime | None = Query(default=None, alias="submittedFrom"),
    submitted_to: datetime | None = Query(default=None, alias="submittedTo"), principal: Principal = Depends(_reviewer), session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    try:
        page_size = validate_page_size(page_size)
        result = await paginated_knowledge(session, uid=principal.uid, is_admin=True, page=page, page_size=page_size, keyword="", filters={"status": ["PENDING_REVIEW"], "knowledgeId": knowledge_id, "creatorUid": creator_uid, "sourceTicketId": source_ticket_id, "sourceTicketNo": source_ticket_no, "submittedFrom": submitted_from, "submittedTo": submitted_to})
    except ValueError as exc:
        raise HTTPException(422, f"精确查询参数无效：{exc}") from exc
    return {**result, "reviewerUid": principal.uid}


@app.get("/api/v1/reviews/knowledge/{knowledge_id}")
async def knowledge_review_get(knowledge_id: str, principal: Principal = Depends(_reviewer), session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    record = await session.get(Knowledge, knowledge_id)
    if not record or record.status != "PENDING_REVIEW":
        raise HTTPException(404, "待审核经验不存在")
    return {**serialize_knowledge(record), "reviewerUid": principal.uid}


@app.post("/api/v1/reviews/knowledge/{knowledge_id}/reject")
async def knowledge_review_reject(knowledge_id: str, body: ReviewRejectRequest, principal: Principal = Depends(_reviewer), session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    record = await session.get(Knowledge, knowledge_id)
    if not record:
        raise HTTPException(404, "经验不存在")
    try:
        record = await reject_knowledge_review(session, record, principal.uid, body.reason)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return serialize_knowledge(record)


@app.post("/api/v1/knowledge/match")
async def knowledge_match(body: MatchRequest, principal: Principal = Depends(current_principal), session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    items, diagnostics = await match_knowledge(session, principal.uid, body.query, body.targetSystems, body.limit)
    return {"items": items, "selectionRequired": len(items) != 1 or diagnostics["embeddingUnavailable"] or diagnostics["rerankUnavailable"], "diagnostics": diagnostics}


@app.post("/api/v1/knowledge/import-legacy")
async def knowledge_import(body: LegacyImportRequest, principal: Principal = Depends(_admin), session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    ids = await import_legacy_package(session, body.package, principal.uid)
    return {"createdKnowledgeIds": ids, "status": "DRAFT"}


@app.post("/api/v1/transfers/export/preview")
async def transfer_export_preview(body: dict[str, Any], principal: Principal = Depends(_admin), session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    try:
        return await export_preview(session, body.get("knowledgeIds"))
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.post("/api/v1/transfers/export")
async def transfer_export(body: dict[str, Any], principal: Principal = Depends(_admin), session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    try:
        return await export_package(session, body, principal.uid)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.post("/api/v1/transfers/import/preview")
async def transfer_import_preview(body: dict[str, Any], principal: Principal = Depends(_admin), session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    try:
        return await import_preview(session, body)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.post("/api/v1/transfers/import")
async def transfer_import(body: dict[str, Any], principal: Principal = Depends(_admin), session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    try:
        return await import_package(session, body, principal.uid)
    except ValueError as exc:
        await session.rollback()
        raise HTTPException(422, str(exc)) from exc


@app.post("/api/v1/database-paths/replace/preview")
async def database_replace_preview(body: dict[str, Any], principal: Principal = Depends(_admin), session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    try:
        return await replace_preview(session, body)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.post("/api/v1/database-paths/replace")
async def database_replace(body: dict[str, Any], principal: Principal = Depends(_admin), session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    try:
        return await replace_paths(session, body, principal.uid)
    except ValueError as exc:
        await session.rollback()
        raise HTTPException(422, str(exc)) from exc


@app.get("/api/v1/transfer-audits")
async def transfer_audit_list(page: int = Query(default=1, ge=1), page_size: int = Query(default=20, alias="pageSize"), action: str = Query(default=""), package_id: str = Query(default="", alias="packageId"), operator_uid: str = Query(default="", alias="operatorUid"), principal: Principal = Depends(_admin), session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    try:
        page_size = validate_page_size(page_size)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    conditions = []
    if action:
        conditions.append(KnowledgeTransferAudit.action == action.strip().upper())
    if package_id:
        conditions.append(KnowledgeTransferAudit.package_id == package_id.strip())
    if operator_uid:
        conditions.append(KnowledgeTransferAudit.operator_uid == operator_uid.strip())
    total = int(await session.scalar(select(func.count()).select_from(KnowledgeTransferAudit).where(*conditions)) or 0)
    total_pages = (total + page_size - 1) // page_size if total else 0
    actual_page = min(page, total_pages) if total_pages else 1
    rows = await session.execute(select(KnowledgeTransferAudit).where(*conditions).order_by(KnowledgeTransferAudit.created_at.desc()).offset((actual_page - 1) * page_size).limit(page_size))
    items = [{"auditId": item.id, "action": item.action, "operatorUid": item.operator_uid, "packageId": item.package_id, "result": item.result, "details": item.details, "createdAt": item.created_at.isoformat()} for item in rows.scalars()]
    return {"items": items, "page": actual_page, "pageSize": page_size, "total": total, "totalPages": total_pages, "appliedFilters": {key: value for key, value in {"action": action, "packageId": package_id, "operatorUid": operator_uid}.items() if value}}


@app.get("/api/v1/workflow-versions/{version_id}")
async def workflow_version_get(version_id: str, principal: Principal = Depends(current_principal), session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    version = await session.get(WorkflowVersion, version_id)
    if not version:
        raise HTTPException(404, "工作流版本不存在")
    try:
        await authorized_knowledge(session, version.knowledge_id, principal.uid, published_only=not principal.is_admin, bypass_visibility=principal.is_admin)
    except KeyError as exc:
        raise HTTPException(404, "工作流版本不存在") from exc
    return {"workflowVersionId": version.id, "knowledgeId": version.knowledge_id, "version": version.version_number, "contentHash": version.content_hash, "definition": version.definition, "publishedBy": version.published_by, "publishedAt": version.published_at.isoformat()}


@app.get("/api/v1/workflow-versions")
async def workflow_version_list(
    page: int = Query(default=1, ge=1), page_size: int = Query(default=20, alias="pageSize"),
    workflow_version_id: list[str] = Query(default=[], alias="workflowVersionId"), knowledge_id: list[str] = Query(default=[], alias="knowledgeId"),
    version_number: list[str] = Query(default=[], alias="versionNumber"), content_hash: list[str] = Query(default=[], alias="contentHash"),
    published_by: list[str] = Query(default=[], alias="publishedBy"), published_from: datetime | None = Query(default=None, alias="publishedFrom"),
    published_to: datetime | None = Query(default=None, alias="publishedTo"), principal: Principal = Depends(current_principal), session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    try:
        page_size = validate_page_size(page_size)
        return await paginated_versions(session, uid=principal.uid, is_admin=principal.is_admin, page=page, page_size=page_size, filters={"workflowVersionId": workflow_version_id, "knowledgeId": knowledge_id, "versionNumber": version_number, "contentHash": content_hash, "publishedBy": published_by, "publishedFrom": published_from, "publishedTo": published_to})
    except ValueError as exc:
        raise HTTPException(422, f"精确查询参数无效：{exc}") from exc


@app.post("/api/v1/runs/plan")
async def run_plan(body: PlanRequest, idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"), principal: Principal = Depends(_operator), session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    ledger, cached = await _idempotency(session, key=idempotency_key, action="RUN_PLAN", principal=principal, run_id=None, payload=body.model_dump(mode="json"))
    if cached is not None:
        return cached
    try:
        run = await create_plan(session, knowledge_id=body.knowledgeId, ticket_id=body.ticketId, parameters=body.parameters, uid=principal.uid, api_key=principal.api_key)
    except KeyError as exc:
        raise HTTPException(404, "已发布经验不存在或无权访问") from exc
    response = await serialize_run(session, run)
    return await complete_idempotency(session, ledger, response)


@app.post("/api/v1/runs/{run_id}/approve")
async def run_approve(run_id: str, body: ApproveRequest, idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"), principal: Principal = Depends(_operator), session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    ledger, cached = await _idempotency(session, key=idempotency_key, action="RUN_APPROVE", principal=principal, run_id=run_id, payload={"runId": run_id, **body.model_dump(mode="json")})
    if cached is not None:
        return cached
    try:
        run = await get_run_for_user(session, run_id, principal.uid, principal.is_admin)
        await approve_plan(session, run, principal.uid, body.planHash)
    except KeyError as exc:
        raise HTTPException(404, "运行不存在") from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    response = await serialize_run(session, run)
    return await complete_idempotency(session, ledger, response)


@app.get("/api/v1/runs")
async def run_list(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, alias="pageSize"),
    keyword: str = Query(default="", max_length=200),
    status_group: str = Query(default="ALL", alias="statusGroup", max_length=20),
    status: list[str] = Query(default=[]), run_id: list[str] = Query(default=[], alias="runId"), knowledge_id: list[str] = Query(default=[], alias="knowledgeId"),
    workflow_version_id: list[str] = Query(default=[], alias="workflowVersionId"), ticket_id: list[str] = Query(default=[], alias="ticketId"),
    initiated_by: list[str] = Query(default=[], alias="initiatedBy"), current_node_id: list[str] = Query(default=[], alias="currentNodeId"),
    created_from: datetime | None = Query(default=None, alias="createdFrom"), created_to: datetime | None = Query(default=None, alias="createdTo"),
    started_from: datetime | None = Query(default=None, alias="startedFrom"), started_to: datetime | None = Query(default=None, alias="startedTo"),
    finished_from: datetime | None = Query(default=None, alias="finishedFrom"), finished_to: datetime | None = Query(default=None, alias="finishedTo"),
    principal: Principal = Depends(_operator),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    try:
        page_size = validate_page_size(page_size)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    status_group = status_group.upper()
    if status_group not in RUN_STATUS_GROUPS:
        raise HTTPException(422, "statusGroup只允许ALL、ACTIVE、WAITING、SUCCEEDED或FAILED")
    statuses = [value.upper() for value in split_values(status)]
    if any(value not in RUN_STATUSES for value in statuses):
        raise HTTPException(422, "status包含不支持的运行状态")
    if initiated_by and not principal.is_admin and any(value != principal.uid for value in split_values(initiated_by)):
        raise HTTPException(403, "普通用户只能查询自己的运行")
    filters = {"status": statuses, "runId": run_id, "knowledgeId": knowledge_id, "workflowVersionId": workflow_version_id, "ticketId": ticket_id, "initiatedBy": initiated_by, "currentNodeId": current_node_id, "createdFrom": created_from, "createdTo": created_to, "startedFrom": started_from, "startedTo": started_to, "finishedFrom": finished_from, "finishedTo": finished_to}
    try:
        return await paginated_runs(session, uid=principal.uid, is_admin=principal.is_admin, page=page, page_size=page_size, keyword=keyword, status_group=status_group, filters=filters)
    except ValueError as exc:
        raise HTTPException(422, f"精确查询参数无效：{exc}") from exc


@app.get("/api/v1/runs/{run_id}")
async def run_get(run_id: str, principal: Principal = Depends(_operator), session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    try:
        run = await get_run_for_user(session, run_id, principal.uid, principal.is_admin)
    except KeyError as exc:
        raise HTTPException(404, "运行不存在") from exc
    return await serialize_run(session, run, include_events=True)


@app.get("/api/v1/runs/{run_id}/wait")
async def run_wait(
    run_id: str,
    request: Request,
    after_event_id: int = Query(default=0, ge=0, alias="afterEventId"),
    timeout_seconds: float = Query(default=10, ge=0, le=15, alias="timeoutSeconds"),
    principal: Principal = Depends(_operator),
) -> dict[str, Any]:
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    while True:
        async with SessionLocal() as session:
            try:
                run = await get_run_for_user(session, run_id, principal.uid, principal.is_admin)
            except KeyError as exc:
                raise HTTPException(404, "运行不存在") from exc
            result = await session.execute(
                select(WorkflowEvent)
                .where(WorkflowEvent.run_id == run_id, WorkflowEvent.sequence > after_event_id)
                .order_by(WorkflowEvent.sequence)
                .limit(200)
            )
            events = list(result.scalars())
            if events or run.status in {"SUCCEEDED", "FAILED", "CANCELLED"} or asyncio.get_running_loop().time() >= deadline:
                snapshot = await serialize_run_facts(session, run, events)
                if events:
                    snapshot["lastEventId"] = events[-1].sequence
                return snapshot
        if await request.is_disconnected():
            return {"runId": run_id, "stale": True, "source": "CLIENT_DISCONNECTED", "events": []}
        await asyncio.sleep(0.25)


@app.get("/api/v1/runs/{run_id}/events")
async def run_events(run_id: str, request: Request, principal: Principal = Depends(_operator), last_event_id: str | None = Header(default=None, alias="Last-Event-ID")):
    async with SessionLocal() as session:
        try:
            await get_run_for_user(session, run_id, principal.uid, principal.is_admin)
        except KeyError as exc:
            raise HTTPException(404, "运行不存在") from exc
    after = int(last_event_id) if last_event_id and last_event_id.isdecimal() else 0
    return StreamingResponse(stream_events(SessionLocal, run_id, after), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


async def _control_run(session: AsyncSession, run_id: str, principal: Principal) -> WorkflowRun:
    try:
        return await get_run_for_user(session, run_id, principal.uid, principal.is_admin)
    except KeyError as exc:
        raise HTTPException(404, "运行不存在") from exc


@app.post("/api/v1/runs/{run_id}/pause")
async def run_pause(run_id: str, idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"), principal: Principal = Depends(_operator), session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    ledger, cached = await _idempotency(session, key=idempotency_key, action="RUN_PAUSE", principal=principal, run_id=run_id, payload={"runId": run_id})
    if cached is not None:
        return cached
    run = await _control_run(session, run_id, principal)
    if run.status not in {"QUEUED", "RUNNING"}:
        raise HTTPException(409, "当前状态不能暂停")
    run.status = "PAUSED" if run.status == "QUEUED" else "PAUSE_REQUESTED"
    await emit_event(session, run, "PAUSE_REQUESTED", status=run.status, summary=f"{principal.uid} 请求暂停")
    await session.commit()
    response = await serialize_run(session, run)
    return await complete_idempotency(session, ledger, response)


@app.post("/api/v1/runs/{run_id}/resume")
async def run_resume(run_id: str, idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"), principal: Principal = Depends(_operator), session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    ledger, cached = await _idempotency(session, key=idempotency_key, action="RUN_RESUME", principal=principal, run_id=run_id, payload={"runId": run_id})
    if cached is not None:
        return cached
    run = await _control_run(session, run_id, principal)
    if run.status != "PAUSED":
        raise HTTPException(409, "当前运行不是暂停状态")
    run.status, run.resume_payload = "QUEUED", {"action": "continue"}
    await emit_event(session, run, "RESUME_REQUESTED", status=run.status, summary=f"{principal.uid} 请求继续")
    await session.commit()
    response = await serialize_run(session, run)
    return await complete_idempotency(session, ledger, response)


@app.post("/api/v1/runs/{run_id}/cancel")
async def run_cancel(run_id: str, idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"), principal: Principal = Depends(_operator), session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    ledger, cached = await _idempotency(session, key=idempotency_key, action="RUN_CANCEL", principal=principal, run_id=run_id, payload={"runId": run_id})
    if cached is not None:
        return cached
    run = await _control_run(session, run_id, principal)
    if run.status in {"SUCCEEDED", "FAILED", "CANCELLED"}:
        raise HTTPException(409, "运行已经结束")
    if run.status == "RUNNING":
        run.status = "CANCEL_REQUESTED"
    else:
        run.status, run.finished_at = "CANCELLED", datetime.now(UTC)
        open_interrupts = list((await session.execute(select(InterruptRecord).where(InterruptRecord.run_id == run.id, InterruptRecord.status.in_({"OPEN", "RESUME_PENDING"})))).scalars())
        statuses = dict(run.node_statuses)
        for record in open_interrupts:
            record.status, record.response_payload, record.resolved_by, record.resolved_at = "CANCELLED", {"action": "CANCEL", "source": "WORKFLOW_RUN_CANCEL"}, principal.uid, datetime.now(UTC)
            if record.node_id:
                statuses[record.node_id] = "CANCELLED"
        run.node_statuses, run.waiting_reason = statuses, None
        credential = await session.scalar(select(RunCredential).where(RunCredential.run_id == run.id))
        if credential:
            await session.delete(credential)
    await emit_event(session, run, "CANCEL_REQUESTED", status=run.status, summary=f"{principal.uid} 请求取消")
    await session.commit()
    response = await serialize_run(session, run)
    return await complete_idempotency(session, ledger, response)


@app.post("/api/v1/runs/{run_id}/interrupts/{interrupt_id}/resume")
async def interrupt_resume(run_id: str, interrupt_id: str, body: ResumeRequest, idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"), principal: Principal = Depends(_operator), session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    ledger, cached = await _idempotency(session, key=idempotency_key, action="INTERRUPT_RESUME", principal=principal, run_id=run_id, payload={"runId": run_id, "interruptId": interrupt_id, **body.model_dump(mode="json")})
    if cached is not None:
        return cached
    async def reject(status_code: int, detail: str) -> None:
        await abandon_idempotency(session, ledger)
        raise HTTPException(status_code, detail)
    try:
        run = await _control_run(session, run_id, principal)
    except HTTPException:
        await abandon_idempotency(session, ledger)
        raise
    record = await session.scalar(select(InterruptRecord).where(InterruptRecord.id == interrupt_id).with_for_update())
    if not record or record.run_id != run_id:
        await reject(404, "待处理的中断不存在")
    if record.status != "OPEN":
        await reject(409, "人工交互已经处理或正在恢复")
    payload = body.payload
    action = str(payload.get("action") or "")
    if action == "CANCEL" and record.kind in {"HITL_SELECT", "HITL_FORM"}:
        record.status, record.response_payload, record.resolved_by, record.resolved_at = "CANCELLED", {"action": "CANCEL"}, principal.uid, datetime.now(UTC)
        statuses = dict(run.node_statuses)
        statuses[str(record.node_id)] = "CANCELLED"
        run.node_statuses, run.status, run.waiting_reason, run.finished_at = statuses, "CANCELLED", None, datetime.now(UTC)
        credential = await session.scalar(select(RunCredential).where(RunCredential.run_id == run.id))
        if credential:
            await session.delete(credential)
        await emit_event(session, run, "INTERRUPT_CANCELLED", node_id=record.node_id, status="CANCELLED", summary=f"{principal.uid} 取消人工交互", payload={"interruptId": interrupt_id, "kind": record.kind})
        await session.commit()
        response = await serialize_run(session, run)
        return await complete_idempotency(session, ledger, response)
    node = next((item for item in (run.workflow_snapshot.get("nodes") or []) if item.get("id") == record.node_id), None)
    if not node:
        await reject(409, "中断对应节点不存在")
    safe_response: dict[str, Any]
    secret_response: dict[str, Any]
    resume_payload: dict[str, Any]
    if record.kind == "HITL_SELECT" and action == "SELECT":
        selected_ids = payload.get("candidateIds")
        if not isinstance(selected_ids, list) or not all(isinstance(value, str) for value in selected_ids) or len(selected_ids) != len(set(selected_ids)):
            await reject(422, "candidateIds必须是无重复字符串数组")
        option_artifact = await session.get(EncryptedArtifact, record.option_artifact_id)
        if not option_artifact:
            await reject(409, "候选数据不存在或已清理")
        candidates = SecretBox().open(option_artifact.ciphertext, purpose="artifact:" + option_artifact.id).get("candidates") or []
        known = {item.get("candidateId") for item in candidates}
        config = node.get("config") or {}
        minimum = int(config.get("minimumSelections") or 1)
        maximum = int(config.get("maximumSelections") or (1 if config.get("selectionMode") == "SINGLE" else 100))
        if any(value not in known for value in selected_ids) or not minimum <= len(selected_ids) <= maximum:
            await reject(422, "候选不存在或选择数量无效")
        safe_response = {"action": "SELECT", "candidateIds": selected_ids}
        secret_response = safe_response
        resume_payload = {"interactionId": record.id, **safe_response}
    elif record.kind == "HITL_FORM" and action == "SUBMIT":
        try:
            values = validate_form(list((node.get("config") or {}).get("fields") or []), payload.get("values"))
        except HitlError as exc:
            await abandon_idempotency(session, ledger)
            raise HTTPException(422, str(exc)) from exc
        safe_response = {"action": "SUBMIT", "fieldNames": sorted(values)}
        secret_response = {"action": "SUBMIT", "values": values}
        resume_payload = {"interactionId": record.id, "action": "SUBMIT"}
    elif record.kind not in {"HITL_SELECT", "HITL_FORM"}:
        run.status, run.resume_payload = "QUEUED", payload
        record.resolved_by = principal.uid
        await emit_event(session, run, "INTERRUPT_RESUME_REQUESTED", node_id=record.node_id, status="QUEUED", summary=f"{principal.uid} 提交恢复输入", payload={"interruptId": interrupt_id})
        await session.commit()
        response = await serialize_run(session, run)
        return await complete_idempotency(session, ledger, response)
    else:
        await reject(422, "HITL回复操作与中断类型不匹配")
    artifact_id = "art_" + secrets.token_hex(16)
    encoded = json.dumps(secret_response, ensure_ascii=False, separators=(",", ":")).encode()
    session.add(EncryptedArtifact(id=artifact_id, run_id=run.id, node_id=str(record.node_id), artifact_type="HITL_RESPONSE", ciphertext=SecretBox().seal(secret_response, purpose="artifact:" + artifact_id), content_hash=sha256_bytes(encoded), size_bytes=len(encoded), expires_at=datetime.now(UTC) + timedelta(days=settings.result_retention_days)))
    record.status, record.response_payload, record.response_artifact_id, record.resolved_by = "RESUME_PENDING", safe_response, artifact_id, principal.uid
    if record.kind == "HITL_FORM":
        resume_payload["responseArtifactId"] = artifact_id
    run.status, run.waiting_reason, run.resume_payload = "QUEUED", None, resume_payload
    await emit_event(session, run, "INTERRUPT_RESUME_REQUESTED", node_id=record.node_id, status="QUEUED", summary=f"{principal.uid} 提交人工交互回复", payload={"interruptId": interrupt_id, "kind": record.kind, **safe_response})
    await session.commit()
    response = await serialize_run(session, run)
    return await complete_idempotency(session, ledger, response)


@app.get("/api/v1/runs/{run_id}/interrupts/{interrupt_id}/options")
async def interrupt_options(run_id: str, interrupt_id: str, offset: int = Query(default=0, ge=0), limit: int = Query(default=20, ge=1, le=100), keyword: str = Query(default="", max_length=200), principal: Principal = Depends(_operator), session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    await _control_run(session, run_id, principal)
    record = await session.get(InterruptRecord, interrupt_id)
    if not record or record.run_id != run_id or record.kind != "HITL_SELECT" or record.status != "OPEN" or not record.option_artifact_id:
        raise HTTPException(404, "待选择的HITL候选不存在")
    artifact = await session.get(EncryptedArtifact, record.option_artifact_id)
    if not artifact:
        raise HTTPException(409, "候选数据不存在或已清理")
    candidates = SecretBox().open(artifact.ciphertext, purpose="artifact:" + artifact.id).get("candidates") or []
    needle = keyword.strip().casefold()
    if needle:
        candidates = [item for item in candidates if needle in (str(item.get("label") or "") + " " + json.dumps(item.get("display") or {}, ensure_ascii=False)).casefold()]
    total, page = len(candidates), candidates[offset:offset + limit]
    request = record.request_payload or {}
    return {"runId": run_id, "interruptId": record.id, "kind": record.kind, "title": request.get("title"), "selectionMode": request.get("selectionMode"), "displayFields": request.get("displayFields"), "minimumSelections": request.get("minimumSelections"), "maximumSelections": request.get("maximumSelections"), "items": [{"candidateId": item.get("candidateId"), "label": item.get("label"), "display": item.get("display")} for item in page], "total": total, "offset": offset, "limit": limit, "hasMore": offset + len(page) < total, "keyword": keyword}


@app.post("/api/v1/runs/{run_id}/credential")
async def run_credential(run_id: str, body: CredentialRequest, principal: Principal = Depends(_operator), session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    run = await _control_run(session, run_id, principal)
    profile = await identity.resolve(body.apiKey)
    if profile["uid"] != principal.uid and not principal.is_admin:
        raise HTTPException(403, "只能更新为当前操作者的凭据")
    await update_credential(session, run, profile["uid"], body.apiKey)
    return await serialize_run(session, run)


@app.post("/api/v1/runs/{run_id}/nodes/{node_id}/retry")
async def node_retry(run_id: str, node_id: str, body: RetryRequest, idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"), principal: Principal = Depends(_operator), session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    ledger, cached = await _idempotency(session, key=idempotency_key, action="NODE_RETRY", principal=principal, run_id=run_id, payload={"runId": run_id, "nodeId": node_id, **body.model_dump(mode="json")})
    if cached is not None:
        return cached
    run = await _control_run(session, run_id, principal)
    if run.node_statuses.get(node_id) not in {"FAILED", "UNKNOWN"}:
        raise HTTPException(409, "节点当前不能重试")
    if body.decision == "mark_failed":
        if run.node_statuses.get(node_id) != "UNKNOWN":
            raise HTTPException(409, "只有结果未知的节点可以人工标记失败")
        run.status, run.finished_at = "FAILED", datetime.now(UTC)
        await emit_event(session, run, "UNKNOWN_MARKED_FAILED", node_id=node_id, status="FAILED", summary=f"{principal.uid} 将未知结果标记为失败")
    else:
        statuses = dict(run.node_statuses)
        statuses[node_id] = "PENDING"
        refs = dict(run.output_refs or {})
        refs.pop(node_id, None)
        run.node_statuses, run.output_refs, run.status, run.finished_at = statuses, refs, "QUEUED", None
        run.current_node_id, run.waiting_reason = node_id, None
        run.lease_owner, run.lease_expires_at = None, None
        await emit_event(session, run, "NODE_RETRY_REQUESTED", node_id=node_id, status="QUEUED", summary=f"{principal.uid} 确认重新执行")
    await session.commit()
    response = await serialize_run(session, run)
    return await complete_idempotency(session, ledger, response)


@app.get("/api/v1/runs/{run_id}/nodes/{node_id}/artifact")
async def node_artifact(run_id: str, node_id: str, principal: Principal = Depends(_operator), session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    await _control_run(session, run_id, principal)
    artifact = await session.scalar(select(EncryptedArtifact).where(EncryptedArtifact.run_id == run_id, EncryptedArtifact.node_id == node_id, EncryptedArtifact.artifact_type == "RESULT").order_by(EncryptedArtifact.created_at.desc()))
    comparison_now = datetime.now(UTC) if artifact and artifact.expires_at.tzinfo else datetime.now(UTC).replace(tzinfo=None)
    if not artifact or artifact.expires_at <= comparison_now:
        raise HTTPException(404, "节点结果不存在或已清理")
    return {"artifactId": artifact.id, "contentHash": artifact.content_hash, "data": SecretBox().open(artifact.ciphertext, purpose="artifact:" + artifact.id)}


@app.get("/api/v1/runs/{run_id}/attempts/{attempt_id}/diagnostic")
async def attempt_diagnostic(run_id: str, attempt_id: str, principal: Principal = Depends(_operator), session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    await _control_run(session, run_id, principal)
    attempt = await session.get(NodeAttempt, attempt_id)
    if not attempt or attempt.run_id != run_id or not attempt.diagnostic_artifact_id:
        raise HTTPException(404, "节点诊断不存在")
    artifact = await session.get(EncryptedArtifact, attempt.diagnostic_artifact_id)
    comparison_now = datetime.now(UTC) if artifact and artifact.expires_at.tzinfo else datetime.now(UTC).replace(tzinfo=None)
    if not artifact or artifact.artifact_type != "DIAGNOSTIC" or artifact.expires_at <= comparison_now:
        raise HTTPException(404, "节点诊断不存在或已清理")
    return {"attemptId": attempt.id, "runId": run_id, "nodeId": attempt.node_id, "errorCode": attempt.error_code, "errorMessage": attempt.error_message, "exitCode": attempt.exit_code, "diagnosticTruncated": attempt.diagnostic_truncated, "data": SecretBox().open(artifact.ciphertext, purpose="artifact:" + artifact.id)}


@app.post("/api/v1/admin/retention/run")
async def retention_cleanup(principal: Principal = Depends(_admin), session: AsyncSession = Depends(get_session)) -> dict[str, int]:
    now = datetime.now(UTC)
    protected = exists(select(InterruptRecord.id).where(InterruptRecord.status.in_({"OPEN", "RESUME_PENDING"}), or_(InterruptRecord.option_artifact_id == EncryptedArtifact.id, InterruptRecord.response_artifact_id == EncryptedArtifact.id)))
    artifact_result = await session.execute(delete(EncryptedArtifact).where(EncryptedArtifact.expires_at <= now, ~protected))
    credential_result = await session.execute(delete(RunCredential).where(RunCredential.expires_at <= now))
    await session.commit()
    return {"artifactsDeleted": artifact_result.rowcount or 0, "credentialsDeleted": credential_result.rowcount or 0}


static_dir = settings.static_dir.resolve()
if static_dir.is_dir():
    assets = static_dir / "assets"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=assets), name="assets")

    def spa_index() -> HTMLResponse:
        base_path = settings.workflow_base_path
        base_href = (base_path + "/") if base_path else "/"
        runtime = json.dumps({"basePath": base_path}, ensure_ascii=False, separators=(",", ":"))
        content = (static_dir / "index.html").read_text(encoding="utf-8")
        content = content.replace("<head>", f'<head><base href="{base_href}"><script>window.__ITSM_WORKFLOW_CONFIG__={runtime}</script>', 1)
        return HTMLResponse(content, headers={"Cache-Control": "no-cache"})

    @app.get("/{path:path}", include_in_schema=False)
    async def spa(path: str):
        candidate = static_dir / path
        if path and candidate.is_file() and candidate.name != "index.html" and static_dir in candidate.resolve().parents:
            return FileResponse(candidate)
        return spa_index()
else:
    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    async def placeholder():
        return "<main><h1>ITSM Workflow</h1><p>Frontend has not been built.</p></main>"
