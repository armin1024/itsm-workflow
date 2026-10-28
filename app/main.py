from __future__ import annotations

import json
import secrets
import time
import uuid
import asyncio
import re
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

from fastapi import Cookie, Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app import __version__
from app.api_metadata import localize_openapi, validation_message
from app.log_filters import LogFilters, OptionalDate, time_bounds
from app.journal_reader import read_journal, redact_raw
from app import call_logs as log_module
from app.call_logs import emit, log_context, hide_credentials, LogCursorExpired
from app.cli import CliExecutionError, inspect_cli
from app.config import settings
from app.extraction import compile_ticket_evidence, fetch_ticket_evidence
from app.runtime import NODE_REGISTRY
from app.runtime.compiler_service import compiler_service
from app.runtime.executor import render_sql
from app.runtime.planner import render_plan, validate_workflow
from app.runtime.push_executor import push_executor
from app.schemas import (
    CompilerPreviewRequest, CompilerTicketRequest, ExecutionCommandRequest,
    ExecutionDispatchRequest, ExtractionSubmitRequest, RuntimePlanRequest,
    RuntimeValidateRequest, StudioExecutionRequest, StudioExtractionRequest,
    StudioLoginRequest, StudioTicketExtractionRequest,
)
from app.studio.auth import COOKIE_NAME, create_session, require_studio_admin, valid_session, verify_admin_token
from app.studio.validation import reject_credentials
from app.studio.events import studio_events
from app.workflow import WorkflowDefinition
from app.resources import ResourceError, cpu_run, data_budget, shutdown_resources
from app.request_limits import BoundedJsonRoute
from app.http_clients import start_http_clients, close_http_clients
from app.compiler_errors import failure_details, log_compiler_failure


@asynccontextmanager
async def lifespan(_: FastAPI):
    settings.validate_service()
    log_module.initialize()
    await start_http_clients()
    try:
        await inspect_cli()
    except (CliExecutionError, TimeoutError, OSError):
        # Extraction-only/local deployments may start without a CLI. The health
        # response reports it; SQL execution still validates before launching.
        pass
    async def maintenance():
        while True:
            await asyncio.sleep(60)
            compiler_service._cleanup()
            studio_events.cleanup()
    maintenance_task = asyncio.create_task(maintenance(), name="runtime-cache-cleanup")
    try:
        yield
    finally:
        try:
            await push_executor.shutdown()
            await compiler_service.shutdown()
            for knowledge_id in list(compiler_service.records):
                compiler_service._evict(knowledge_id)
        finally:
            maintenance_task.cancel()
            await asyncio.gather(maintenance_task, return_exceptions=True)
            studio_events.clear()
            await close_http_clients()
            await shutdown_resources()
            if log_module.call_logs:
                await asyncio.to_thread(log_module.call_logs.stop)


app = FastAPI(title="ITSM Workflow Runtime", version=__version__, lifespan=lifespan, docs_url=None, redoc_url=None)
app.router.route_class = BoundedJsonRoute


@app.middleware("http")
async def log_requests(request: Request, call_next):
    start = time.monotonic()
    tracked = request.url.path.startswith(("/internal/", "/api/")) and not request.url.path.startswith("/api/v1/studio/logs")
    polling = request.method == "GET" and (request.url.path in {"/api/v1/health", "/api/v1/studio/session"} or request.url.path.startswith(("/api/v1/studio/events/", "/api/v1/studio/executions/", "/internal/v1/executions/")))
    route_path = "[unmatched-route]"
    for candidate in app.router.routes:
        match, _ = candidate.matches(request.scope)
        if match.name == "FULL":
            route_path = getattr(candidate, "path", "[matched-route]")
            break
    request_id = request.headers.get("X-Request-Id", "")
    request_id = request_id if re.fullmatch(r"[A-Za-z0-9_.:-]{1,160}", request_id) else None
    with hide_credentials(request.headers.get("X-AOPS-Api-Key"), request.headers.get("Authorization"), *request.cookies.values()), log_context(traceId=uuid.uuid4().hex, spanId=uuid.uuid4().hex, requestId=request_id, direction="INBOUND", peer="TEC01" if request.url.path.startswith("/internal/") else "STUDIO", method=request.method, url=route_path):
        if tracked and not polling:
            emit("REQUEST_STARTED", operation="http_request")
        try:
            response = await call_next(request)
        except Exception:
            emit("REQUEST_FAILED", "ERROR", operation="http_request", errorCode="UNHANDLED_ERROR", httpStatus=500, durationMs=round((time.monotonic() - start) * 1000, 2))
            detail = {"code": "INTERNAL_ERROR", "message": "请求处理失败，请根据关联日志排查", "retryable": False}
            return JSONResponse(detail if request.url.path.startswith("/internal/") else {"detail": detail}, status_code=500)
        route = request.scope.get("route")
        route_path = getattr(route, "path", "[unmatched-route]")
        if (tracked and not polling) or response.status_code >= 400:
            emit("REQUEST_COMPLETED" if response.status_code < 400 else "REQUEST_FAILED", "INFO" if response.status_code < 400 else "WARNING", operation=getattr(request.state, "log_operation", "http_request"), runId=getattr(request.state, "log_run_id", None), url=route_path, httpStatus=response.status_code, durationMs=round((time.monotonic() - start) * 1000, 2))
        return response


@app.exception_handler(HTTPException)
async def http_error(request: Request, exc: HTTPException):
    if request.url.path.startswith("/internal/"):
        detail = exc.detail if isinstance(exc.detail, dict) else {"code": f"HTTP_{exc.status_code}", "message": str(exc.detail), "retryable": exc.status_code >= 500}
        return JSONResponse(detail, status_code=exc.status_code, headers=exc.headers)
    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code, headers=exc.headers)


@app.exception_handler(ResourceError)
async def resource_error(request: Request, exc: ResourceError):
    detail = {"code": exc.code, "message": str(exc), "retryable": exc.retryable}
    headers = {"Retry-After": "3"} if exc.status_code == 429 else None
    return JSONResponse(detail if request.url.path.startswith("/internal/") else {"detail": detail}, status_code=exc.status_code, headers=headers)


@app.exception_handler(RequestValidationError)
async def request_error(request: Request, exc: RequestValidationError):
    # ValidationError input/context can contain a submitted credential. Return
    # field locations and messages only, never echo request contents or headers.
    fields = [{"field": ".".join(str(item) for item in error["loc"]), "message": validation_message(error)} for error in exc.errors()]
    detail = {"code": "REQUEST_VALIDATION_FAILED", "message": "请求字段校验失败", "retryable": False, "details": {"fields": fields}}
    return JSONResponse(detail if request.url.path.startswith("/internal/") else {"detail": detail}, status_code=422)


async def runtime_service(authorization: str | None = Header(default=None)) -> None:
    if not settings.runtime_service_token:
        raise HTTPException(401, {"code": "RUNTIME_AUTH_REQUIRED", "message": "请配置RUNTIME_SERVICE_TOKEN", "retryable": False})
    if not authorization or not secrets.compare_digest(authorization, "Bearer " + settings.runtime_service_token):
        raise HTTPException(401, {"code": "RUNTIME_AUTH_FAILED", "message": "Runtime服务身份无效", "retryable": False})


def configured_callbacks() -> None:
    if not settings.tec01_base_url or not settings.tec01_service_token:
        raise HTTPException(503, {"code": "TEC01_NOT_CONFIGURED", "message": "请配置TEC01_BASE_URL和TEC01_SERVICE_TOKEN", "retryable": False})


def invalid(exc: Exception) -> HTTPException:
    if isinstance(exc, ResourceError):
        return HTTPException(exc.status_code, {"code": exc.code, "message": str(exc), "retryable": exc.retryable}, headers={"Retry-After": "3"} if exc.status_code == 429 else None)
    text = str(exc)
    conflict = any(code in text for code in ("CONFLICT", "ALREADY_ACTIVE"))
    return HTTPException(409 if conflict else 422, {"code": text.split(":")[0] if conflict else "WORKFLOW_VALIDATION_FAILED", "message": text, "retryable": False})


def compiler_http_failure(exc: Exception, stage="ACCEPTED", *, knowledge_id="", request_id="") -> HTTPException:
    error = failure_details(exc, stage)
    log_compiler_failure(error, knowledge_id=knowledge_id, request_id=request_id)
    status = exc.status_code if isinstance(exc, ResourceError) else 409 if error["code"] == "EXTRACTION_CONFLICT" else 422 if isinstance(exc, ValueError) else 500
    return HTTPException(status, error, headers={"Retry-After": "3"} if status == 429 else None)


async def validated(body: RuntimeValidateRequest) -> dict[str, Any]:
    result = await cpu_run(validate_workflow, body.workflowDefinition, body.validationMode)
    if body.targetCatalogDigest and body.targetCatalogDigest != result["catalogDigest"]:
        raise HTTPException(409, {"code": "CATALOG_DIGEST_CONFLICT", "message": "目标Catalog已变化", "retryable": True})
    return result


async def data_response(value):
    """Serialize potentially large definitions/results off the API loop."""
    encoded = await cpu_run(lambda data: json.dumps(data, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8"), value)
    return Response(encoded, media_type="application/json")


@app.get("/api/v1/health")
async def health() -> dict[str, Any]:
    try:
        metadata = await inspect_cli()
        cli = {"status": "ok", "version": metadata.version, "sha256": metadata.sha256}
    except (CliExecutionError, TimeoutError):
        cli = {"status": "unavailable", "message": "aops-cli不可用，请检查路径与执行权限"}
    return {"status": "ok", "version": __version__, "role": "runtime-compiler-studio", "authentication": "studio-token", "storage": "browser", "cli": cli, "executorId": settings.executor_id, "activeRuns": len(push_executor.active), "activeExtractions": compiler_service.active_jobs, "dataBudget": data_budget.status()}


@app.post("/api/v1/studio/session")
async def studio_login(body: StudioLoginRequest, response: Response):
    if not verify_admin_token(body.token):
        raise HTTPException(401, "管理Token无效")
    response.set_cookie(COOKIE_NAME, create_session(), max_age=settings.studio_session_hours * 3600, httponly=True, secure=settings.studio_cookie_secure, samesite="strict", path="/")
    return {"authenticated": True}


@app.get("/api/v1/studio/session")
async def studio_session(itsm_runtime_studio: str | None = Cookie(default=None, alias=COOKIE_NAME)):
    return {"authenticated": valid_session(itsm_runtime_studio)}


@app.delete("/api/v1/studio/session", status_code=204)
async def studio_logout(response: Response):
    response.delete_cookie(COOKIE_NAME, path="/")


@app.get("/api/v1/studio/catalog", dependencies=[Depends(require_studio_admin)])
async def studio_catalog():
    catalog = NODE_REGISTRY.catalog()
    return {**catalog, "runtimeVersion": __version__, "testOnly": True}


@app.get("/api/v1/studio/config", dependencies=[Depends(require_studio_admin)])
async def studio_configuration():
    """只读运行配置；凭据只返回是否配置，不提供修改接口。"""
    return {**settings.studio_configuration(), "runtimeVersion": __version__}


@app.get("/api/v1/studio/logs/status", dependencies=[Depends(require_studio_admin)])
async def log_status():
    return await asyncio.to_thread(log_module.call_logs.status) if log_module.call_logs else {"available": False}


@app.get("/api/v1/studio/logs", dependencies=[Depends(require_studio_admin)])
async def log_query(filters: LogFilters = Depends()):
    if not log_module.call_logs:
        return {"items": [], "nextCursor": None, "scannedBytes": 0, "unavailable": True}
    try:
        return await asyncio.to_thread(log_module.call_logs.query, **filters.options)
    except LogCursorExpired as exc:
        raise HTTPException(409, str(exc)) from exc


@app.get("/api/v1/studio/logs/raw", dependencies=[Depends(require_studio_admin)])
async def raw_log_query(filters: LogFilters = Depends()):
    if not log_module.call_logs:
        return {"rawLines": [], "nextCursor": None, "unavailable": True}
    try:
        result = await asyncio.to_thread(log_module.call_logs.query, raw=True, **filters.options)
        for line in result.get("rawLines", []):
            line["text"] = redact_raw(line["text"])
        result.pop("items", None)
        return result
    except LogCursorExpired as exc:
        raise HTTPException(409, str(exc)) from exc


@app.get("/api/v1/studio/logs/journal", dependencies=[Depends(require_studio_admin)])
async def journal_logs(limit: int = Query(default=100, ge=1, le=200), keyword: str = Query(default="", max_length=200), fromTime: OptionalDate = None, toTime: OptionalDate = None):
    time_bounds(fromTime, toTime)
    return await read_journal(limit=limit, keyword=keyword.strip(), from_time=fromTime, to_time=toTime)


@app.post("/api/v1/studio/compiler/preview", dependencies=[Depends(require_studio_admin)])
async def compiler_preview(body: CompilerPreviewRequest):
    current_stage = "ACCEPTED"
    async def track(stage, *_):
        nonlocal current_stage
        current_stage = stage
    try:
        async with compiler_service.admission() as reservation:
            await compiler_service.charge_evidence(reservation, body.ticketInfo, body.auditTimeline)
            proposal, diagnostics = await compile_ticket_evidence(body.ticketInfo, body.auditTimeline, progress=track)
        result = await cpu_run(validate_workflow, proposal["workflowDefinition"], "DRAFT")
        proposal["workflowDefinition"] = result["normalizedDefinition"]
        from app.extraction import extraction_outcome
        return await data_response({"proposal": proposal, "diagnostics": diagnostics, "validation": result, "catalogDigest": result["catalogDigest"], **extraction_outcome(diagnostics)})
    except Exception as exc:
        raise compiler_http_failure(exc, current_stage) from exc


@app.post("/api/v1/studio/compiler/from-ticket", dependencies=[Depends(require_studio_admin)])
async def compiler_ticket(body: CompilerTicketRequest, x_aops_api_key: str | None = Header(default=None, alias="X-AOPS-Api-Key")):
    if not x_aops_api_key:
        raise HTTPException(422, "请提供X-AOPS-Api-Key")
    current_stage = "FETCHING_EVIDENCE"
    async def track(stage, *_):
        nonlocal current_stage
        current_stage = stage
    try:
        async with compiler_service.admission() as reservation:
            ticket, timeline = await fetch_ticket_evidence(body.ticketId, x_aops_api_key)
            await compiler_service.charge_evidence(reservation, ticket, timeline)
            proposal, diagnostics = await compile_ticket_evidence(ticket, timeline, progress=track)
        from app.extraction import extraction_outcome
        return await data_response({"proposal": proposal, "diagnostics": diagnostics, **extraction_outcome(diagnostics)})
    except Exception as exc:
        raise compiler_http_failure(exc, current_stage) from exc


async def submit_extraction(payload: dict[str, Any], sync: bool, local: bool, reservation=None):
    if sync:
        configured_callbacks()
    knowledge_id = payload["knowledgeId"]
    async def sink(event):
        await studio_events.publish(knowledge_id, event)
    previous, reserved = None, False
    try:
        if local:
            previous = studio_events.reserve(knowledge_id, settings.compiler_cache_max_mb * 1024 * 1024)
            reserved = True
        with log_context(knowledgeId=knowledge_id, requestId=payload["requestId"], ticketId=payload["ticketId"]):
            emit("EXTRACTION_SUBMITTED", operation="extraction")
            created = await compiler_service.submit_async(payload, reservation=reservation, event_sink=sink if local else None, sync_tec01=sync)
    except BaseException as exc:
        if reserved:
            studio_events.restore_reservation(knowledge_id, previous)
        if isinstance(exc, Exception):
            raise compiler_http_failure(exc, "ACCEPTED", knowledge_id=knowledge_id, request_id=payload["requestId"]) from exc
        raise
    if created and local:
        studio_events.open(knowledge_id, reset=True)
    elif local:
        studio_events.restore_reservation(knowledge_id, previous)
    return {"knowledgeId": knowledge_id, "accepted": True, "duplicate": not created}


@app.post("/api/v1/studio/extractions", status_code=202, dependencies=[Depends(require_studio_admin)])
async def studio_extract(body: StudioExtractionRequest):
    try:
        return await submit_extraction(await cpu_run(body.model_dump, mode="json", exclude={"syncTec01"}), body.syncTec01, True)
    except ValueError as exc:
        raise invalid(exc) from exc


@app.post("/api/v1/studio/extractions/from-ticket", status_code=202, dependencies=[Depends(require_studio_admin)])
async def studio_extract_ticket(body: StudioTicketExtractionRequest, x_aops_api_key: str | None = Header(default=None, alias="X-AOPS-Api-Key")):
    if not x_aops_api_key:
        raise HTTPException(422, "请提供X-AOPS-Api-Key")
    try:
        async with compiler_service.admission() as reservation:
            ticket, timeline = await fetch_ticket_evidence(body.ticketId, x_aops_api_key)
            payload = {"knowledgeId": body.knowledgeId, "requestId": body.requestId, "ticketId": body.ticketId, "ticketInfo": ticket, "auditTimeline": timeline}
            return await submit_extraction(payload, body.syncTec01, True, reservation=reservation)
    except HTTPException:
        raise
    except Exception as exc:
        raise compiler_http_failure(exc, "FETCHING_EVIDENCE", knowledge_id=body.knowledgeId, request_id=body.requestId) from exc


@app.post("/api/v1/studio/workflows/validate", dependencies=[Depends(require_studio_admin)])
async def studio_validate(body: RuntimeValidateRequest):
    try:
        return await data_response(await validated(body))
    except ValueError as exc:
        raise invalid(exc) from exc


def _studio_plan_data(body: dict[str, Any]):
    try:
        ticket_id = body.get("ticketId")
        if isinstance(ticket_id, bool) or not isinstance(ticket_id, int) or ticket_id <= 0:
            raise ValueError("工单ID必须是正整数")
        inputs = body.get("inputs") or {}
        reject_credentials(inputs)
        result = validate_workflow(body["workflowDefinition"], "EXECUTE" if body.get("preserveCatalog") is True else "TEST")
        plan = render_plan(workflow_version_id="local-browser", workflow_content_hash=result["workflowContentHash"], workflow_snapshot=result["normalizedDefinition"], ticket_id=ticket_id, parameters=inputs, actor_uid="studio")
        originals = {node["id"]: node for node in result["normalizedDefinition"]["nodes"]}
        for node in plan["renderedPlan"]["nodes"]:
            node["comment"] = f"#uatu-{ticket_id}"
            original = originals[node["nodeId"]]
            values = {}
            for item in original.get("inputs", []):
                source = item["source"]
                if source["kind"] == "RUN_INPUT" and source.get("key") in inputs:
                    values[item["name"]] = inputs[source["key"]]
                elif source["kind"] == "LITERAL":
                    values[item["name"]] = source.get("value")
            if node["type"] == "sql_read":
                try:
                    node["renderedSql"] = render_sql(original, values)
                except ValueError:
                    node["deferred"] = True
        return {**result, **plan}
    except (ValueError, KeyError, TypeError) as exc:
        raise invalid(exc) from exc


@app.post("/api/v1/studio/workflows/plan", dependencies=[Depends(require_studio_admin)])
async def studio_plan(body: dict[str, Any]):
    return await data_response(await cpu_run(_studio_plan_data, body))


async def dispatch(body: ExecutionDispatchRequest, key: str | None, *, local: bool, sync: bool):
    if sync:
        configured_callbacks()
    payload = await cpu_run(body.model_dump, mode="json", exclude={"syncTec01", "planMaterialHash"})
    await cpu_run(reject_credentials, {"workflowSnapshot": payload["workflowSnapshot"], "runInputs": payload["runInputs"]})
    nodes = payload["workflowSnapshot"].get("nodes") or []
    if not isinstance(nodes, list) or not all(isinstance(node, dict) for node in nodes):
        raise ValueError("workflowSnapshot.nodes必须为节点对象数组")
    if any(node.get("type") == "sql_read" for node in nodes) and not key:
        raise ValueError("包含SQL读节点时必须通过X-AOPS-Api-Key提供当前用户凭据")
    if local and getattr(body, "planMaterialHash", None):
        checked = await cpu_run(render_plan, workflow_version_id="local-browser", workflow_content_hash=body.workflowContentHash, workflow_snapshot=body.workflowSnapshot, ticket_id=body.ticketId, parameters=body.runInputs, actor_uid="studio")
        if not checked["valid"] or body.planMaterialHash != checked["planMaterialHash"]:
            raise HTTPException(409, {"code": "PLAN_CONFIRMATION_MISMATCH", "message": "计划定义、工单或参数已变化，请重新创建并确认计划", "retryable": False})
    run_id = body.runId
    async def sink(event):
        # Do not retain the request model (and all recovery outputs) in the
        # background event sink closure; the executor owns its prepared copy.
        await studio_events.publish(run_id, event)
    prepared = await push_executor.prepare_dispatch(payload)
    previous = studio_events.reserve(body.runId, settings.executor_run_memory_mb * 1024 * 1024) if local else None
    try:
        with log_context(runId=body.runId, dispatchId=body.dispatchId, ticketId=body.ticketId), hide_credentials(key):
            emit("EXECUTION_DISPATCHED", operation="dispatch")
            accepted, created = push_executor.dispatch(payload, key, event_sink=sink if local else None, sync_tec01=sync, prepared=prepared)
    except BaseException:
        if local:
            studio_events.restore_reservation(body.runId, previous)
        raise
    if local and created:
        studio_events.open(body.runId, reset=True)
    elif local:
        studio_events.restore_reservation(body.runId, previous)
    return accepted


@app.post("/api/v1/studio/executions/dispatch", dependencies=[Depends(require_studio_admin)])
async def studio_dispatch(body: StudioExecutionRequest, x_aops_api_key: str | None = Header(default=None, alias="X-AOPS-Api-Key")):
    try:
        return await dispatch(body, x_aops_api_key, local=True, sync=body.syncTec01)
    except (ValueError, RuntimeError) as exc:
        if "NO_CAPACITY" in str(exc):
            raise HTTPException(429, "Executor当前无可用容量", headers={"Retry-After": "3"}) from exc
        raise invalid(exc) from exc


@app.post("/api/v1/studio/executions/{run_id}/commands", dependencies=[Depends(require_studio_admin)])
async def studio_command(run_id: str, body: ExecutionCommandRequest, request: Request):
    request.state.log_operation = "pause_run" if body.type == "PAUSE" else "cancel_run"
    request.state.log_run_id = run_id
    return push_executor.command(run_id, body.commandId, body.type)


@app.get("/api/v1/studio/executions/{run_id}", dependencies=[Depends(require_studio_admin)])
async def studio_activity(run_id: str):
    return push_executor.activity(run_id)


@app.get("/api/v1/studio/events/{stream_id}", dependencies=[Depends(require_studio_admin)])
async def events(stream_id: str, after: int = Query(default=0, ge=0)):
    try:
        result = studio_events.get(stream_id, after)
        record = compiler_service.sync_state(stream_id)
        if record:
            result.update({key: record[key] for key in ("syncStatus", "callbackError") if key in record})
        return await data_response(result)
    except KeyError as exc:
        raise HTTPException(404, "服务中没有活动记录，请使用浏览器已保存的计划恢复") from exc


@app.get("/internal/v1/runtime/catalog", dependencies=[Depends(runtime_service)])
async def runtime_catalog(response: Response, if_none_match: str | None = Header(default=None, alias="If-None-Match")):
    catalog = NODE_REGISTRY.catalog()
    etag = '"' + catalog["catalogDigest"] + '"'
    if if_none_match in {etag, catalog["catalogDigest"]}:
        return Response(status_code=304, headers={"ETag": etag})
    response.headers["ETag"] = etag
    return {**catalog, "runtimeVersion": __version__, "generatedAt": datetime.now(UTC).isoformat(), "testOnly": False}


@app.post("/internal/v1/runtime/workflows/validate", dependencies=[Depends(runtime_service)])
async def runtime_validate(body: RuntimeValidateRequest):
    try:
        return await data_response(await validated(body))
    except ValueError as exc:
        raise invalid(exc) from exc


def schema_catalog():
    from app.schemas import ExtractionDiagnostics, ExtractionFinished
    models = {"workflowDefinition": WorkflowDefinition, "dispatch": ExecutionDispatchRequest, "extraction": ExtractionSubmitRequest,
              "extractionDiagnostics": ExtractionDiagnostics, "extractionFinished": ExtractionFinished}
    dialect = "https://json-schema.org/draft/2020-12/schema"
    schemas = {name: {"$schema": dialect, **model.model_json_schema()} for name, model in models.items()}
    schemas["extraction"]["properties"]["auditTimeline"]["maxItems"] = settings.compiler_max_audit_operations
    definitions = schemas["workflowDefinition"].get("$defs", {})
    definitions["WorkflowNode"]["properties"]["type"]["enum"] = [node["type"] for node in NODE_REGISTRY.catalog()["nodes"]]
    schemas["dispatch"]["$defs"] = {**definitions, "WorkflowDefinition": {key: value for key, value in schemas["workflowDefinition"].items() if key not in {"$defs", "$schema"}}}
    schemas["dispatch"]["properties"]["workflowSnapshot"] = {"$ref": "#/$defs/WorkflowDefinition"}
    return {"runtimeVersion": __version__, "jsonSchemaDialect": dialect, "schemas": schemas, "nodes": NODE_REGISTRY.catalog()["nodes"],
            "limits": {"requestMaxBytes": settings.request_max_body_mb * 1024 * 1024,
                       "evidenceMaxBytes": settings.compiler_evidence_max_mb * 1024 * 1024,
                       "auditOperations": settings.compiler_max_audit_operations,
                       "sqlTemplateMaxChars": settings.sql_template_max_chars,
                       "modelInputMaxChars": settings.compiler_model_input_max_chars,
                       "jsonMaxDepth": 128, "jsonMaxScalarBytes": 4096}}


@app.get("/internal/v1/runtime/schemas", dependencies=[Depends(runtime_service)])
async def runtime_schemas():
    return schema_catalog()


@app.get("/internal/v1/runtime/schemas/{schema_name}", dependencies=[Depends(runtime_service)])
async def runtime_schema(schema_name: str):
    schema = schema_catalog()["schemas"].get(schema_name)
    if schema is None:
        raise HTTPException(404, {"code": "SCHEMA_NOT_FOUND", "message": "该约束名称不存在", "retryable": False})
    return schema


@app.post("/internal/v1/runtime/workflows/plan", dependencies=[Depends(runtime_service)])
async def runtime_plan(body: RuntimePlanRequest):
    try:
        return await data_response(await cpu_run(render_plan, workflow_version_id=body.workflowVersionId, workflow_content_hash=body.workflowContentHash, workflow_snapshot=body.workflowSnapshot, ticket_id=body.ticketId, parameters=body.parameters, actor_uid=body.actorUid))
    except ValueError as exc:
        raise invalid(exc) from exc


@app.post("/internal/v1/extractions/{knowledge_id}", status_code=202, dependencies=[Depends(runtime_service)])
async def extraction(knowledge_id: str, body: ExtractionSubmitRequest):
    try:
        return await submit_extraction({"knowledgeId": knowledge_id, **(await cpu_run(body.model_dump, mode="json"))}, True, False)
    except ValueError as exc:
        raise invalid(exc) from exc


@app.post("/internal/v1/executions/dispatch", dependencies=[Depends(runtime_service)])
async def execution_dispatch(body: ExecutionDispatchRequest, x_aops_api_key: str | None = Header(default=None, alias="X-AOPS-Api-Key")):
    try:
        return await dispatch(body, x_aops_api_key, local=False, sync=True)
    except (ValueError, RuntimeError) as exc:
        if "NO_CAPACITY" in str(exc):
            raise HTTPException(429, {"code": "EXECUTOR_NO_CAPACITY", "message": "Executor当前无可用容量", "retryable": True}, headers={"Retry-After": "3"}) from exc
        raise invalid(exc) from exc


@app.post("/internal/v1/executions/{run_id}/commands", dependencies=[Depends(runtime_service)])
async def execution_command(run_id: str, body: ExecutionCommandRequest, request: Request):
    request.state.log_operation = "pause_run" if body.type == "PAUSE" else "cancel_run"
    request.state.log_run_id = run_id
    return push_executor.command(run_id, body.commandId, body.type)


@app.get("/internal/v1/executions/{run_id}", dependencies=[Depends(runtime_service)])
async def execution_activity(run_id: str):
    return push_executor.activity(run_id)


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
        if path.startswith("api/") or path.startswith("internal/") or path == "mcp":
            raise HTTPException(404, "接口不存在")
        candidate = static_dir / path
        if path and candidate.is_file() and candidate.name != "index.html" and static_dir in candidate.resolve().parents:
            return FileResponse(candidate)
        return spa_index()
else:
    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    async def placeholder():
        return "<main><h1>ITSM Workflow Runtime</h1><p>Frontend has not been built.</p></main>"


localize_openapi(app)
