from __future__ import annotations

import json
import secrets
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

from fastapi import Cookie, Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app import __version__
from app.cli import CliExecutionError, inspect_cli
from app.config import settings
from app.extraction import compile_ticket_evidence, compile_ticket_id, fetch_ticket_evidence
from app.runtime import NODE_REGISTRY
from app.runtime.compiler_service import compiler_service
from app.runtime.executor import render_sql
from app.runtime.planner import render_plan, validate_workflow
from app.runtime.push_executor import push_executor
from app.runtime.single_executor import single_executor
from app.schemas import (
    CompilerPreviewRequest, CompilerTicketRequest, ExecutionCommandRequest,
    ExecutionDispatchRequest, ExtractionSubmitRequest, RuntimePlanRequest,
    RuntimeValidateRequest, StudioExecutionRequest, StudioExtractionRequest,
    SingleNodeRequest, SingleNodeResponse,
    StudioInterruptReply, StudioLoginRequest, StudioNodeDebugRequest, StudioNodeUpdate,
    StudioTicketExtractionRequest, StudioWorkflowDebugRequest,
)
from app.studio.auth import COOKIE_NAME, create_session, require_studio_admin, valid_session, verify_admin_token
from app.studio.debug import reject_credentials, run_single_node
from app.studio.events import studio_events
from app.workflow import WorkflowDefinition


@asynccontextmanager
async def lifespan(_: FastAPI):
    settings.validate_service()
    yield
    await push_executor.shutdown()
    await compiler_service.shutdown()
    await single_executor.shutdown()


app = FastAPI(title="ITSM Workflow Runtime", version=__version__, lifespan=lifespan, docs_url=None, redoc_url=None)


@app.exception_handler(HTTPException)
async def http_error(request: Request, exc: HTTPException):
    if request.url.path.startswith("/internal/"):
        detail = exc.detail if isinstance(exc.detail, dict) else {"code": f"HTTP_{exc.status_code}", "message": str(exc.detail), "retryable": exc.status_code >= 500}
        return JSONResponse(detail, status_code=exc.status_code, headers=exc.headers)
    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code, headers=exc.headers)


@app.exception_handler(RequestValidationError)
async def request_error(request: Request, exc: RequestValidationError):
    # ValidationError input/context can contain a submitted credential. Return
    # field locations and messages only, never echo request contents or headers.
    fields = [{"field": ".".join(str(item) for item in error["loc"]), "message": error["msg"]} for error in exc.errors()]
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
    text = str(exc)
    conflict = any(code in text for code in ("CONFLICT", "ALREADY_ACTIVE"))
    return HTTPException(409 if conflict else 422, {"code": text.split(":")[0] if conflict else "WORKFLOW_VALIDATION_FAILED", "message": text, "retryable": False})


def validated(body: RuntimeValidateRequest) -> dict[str, Any]:
    result = validate_workflow(body.workflowDefinition, body.validationMode)
    if body.targetCatalogDigest and body.targetCatalogDigest != result["catalogDigest"]:
        raise HTTPException(409, {"code": "CATALOG_DIGEST_CONFLICT", "message": "目标Catalog已变化", "retryable": True})
    return result


@app.get("/api/v1/health")
async def health() -> dict[str, Any]:
    try:
        metadata = await inspect_cli()
        cli = {"status": "ok", "version": metadata.version, "sha256": metadata.sha256}
    except (CliExecutionError, TimeoutError):
        cli = {"status": "unavailable", "message": "aops-cli不可用，请检查路径与执行权限"}
    return {"status": "ok", "version": __version__, "role": "runtime-compiler-studio", "authentication": "studio-token", "storage": "browser", "cli": cli, "executorId": settings.executor_id, "activeRuns": len(push_executor.active)}


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
    return {**catalog, "runtimeVersion": __version__, "testOnly": True, "nodes": [{**node, "studioEnabled": True} for node in catalog["nodes"]]}


@app.get("/api/v1/studio/nodes", dependencies=[Depends(require_studio_admin)])
async def studio_nodes():
    catalog = await studio_catalog()
    return {"items": catalog["nodes"], "total": len(catalog["nodes"]), "catalogDigest": catalog["catalogDigest"]}


@app.patch("/api/v1/studio/nodes/{node_type}/{schema_version}", dependencies=[Depends(require_studio_admin)])
async def studio_node_update(node_type: str, schema_version: int, body: StudioNodeUpdate):
    try:
        NODE_REGISTRY.get(node_type, schema_version)
        reject_credentials(body.model_dump(mode="json"))
        return {"type": node_type, "schemaVersion": schema_version, **body.model_dump(mode="json"), "storage": "browser"}
    except ValueError as exc:
        raise invalid(exc) from exc


@app.post("/api/v1/studio/node-debug-runs", dependencies=[Depends(require_studio_admin)])
async def single_node(body: StudioNodeDebugRequest, x_aops_api_key: str | None = Header(default=None, alias="X-AOPS-Api-Key")):
    try:
        result = await run_single_node(node=body.node, inputs=body.inputs, mode=body.mode, simulation=body.simulation, ticket_id=body.ticketId, api_key=x_aops_api_key)
        return {"debugRunId": "test_node_" + uuid.uuid4().hex, "node": body.node, "inputs": body.inputs, "mode": body.mode, "testOnly": True, **result}
    except (ValueError, KeyError) as exc:
        raise invalid(exc) from exc


@app.post("/api/v1/studio/node-debug-runs/reply", dependencies=[Depends(require_studio_admin)])
async def node_reply(body: StudioInterruptReply):
    try:
        reject_credentials(body.model_dump(mode="json"))
        if body.node.get("type") != "hitl_form":
            raise ValueError("该节点不支持人工回复")
        if body.response.get("action") == "CANCEL":
            return {"status": "CANCELLED", "output": None, "interrupt": None}
        result = await run_single_node(node=body.node, inputs=body.inputs, mode="TEST", resume_payload={**body.response, "nodeId": body.node.get("id")})
        return {"node": body.node, "inputs": body.inputs, "mode": "TEST", "testOnly": True, **result}
    except ValueError as exc:
        raise invalid(exc) from exc


@app.post("/api/v1/studio/compiler/preview", dependencies=[Depends(require_studio_admin)])
async def compiler_preview(body: CompilerPreviewRequest):
    try:
        proposal, diagnostics = await compile_ticket_evidence(body.ticketInfo, body.auditTimeline)
        result = validate_workflow(proposal["workflowDefinition"], "DRAFT")
        proposal["workflowDefinition"] = result["normalizedDefinition"]
        return {"proposal": proposal, "diagnostics": diagnostics, "validation": result, "catalogDigest": result["catalogDigest"]}
    except ValueError as exc:
        raise invalid(exc) from exc


@app.post("/api/v1/studio/compiler/from-ticket", dependencies=[Depends(require_studio_admin)])
async def compiler_ticket(body: CompilerTicketRequest, x_aops_api_key: str | None = Header(default=None, alias="X-AOPS-Api-Key")):
    if not x_aops_api_key:
        raise HTTPException(422, "请提供X-AOPS-Api-Key")
    proposal, diagnostics = await compile_ticket_id(body.ticketId, x_aops_api_key)
    return {"proposal": proposal, "diagnostics": diagnostics}


def submit_extraction(payload: dict[str, Any], sync: bool, local: bool):
    if sync:
        configured_callbacks()
    knowledge_id = payload["knowledgeId"]
    async def sink(event):
        await studio_events.publish(knowledge_id, event)
    created = compiler_service.submit(payload, event_sink=sink if local else None, sync_tec01=sync)
    if created and local:
        studio_events.open(knowledge_id, reset=True)
    return {"knowledgeId": knowledge_id, "accepted": True, "duplicate": not created}


@app.post("/api/v1/studio/extractions", status_code=202, dependencies=[Depends(require_studio_admin)])
async def studio_extract(body: StudioExtractionRequest):
    try:
        return submit_extraction(body.model_dump(mode="json", exclude={"syncTec01"}), body.syncTec01, True)
    except ValueError as exc:
        raise invalid(exc) from exc


@app.post("/api/v1/studio/extractions/from-ticket", status_code=202, dependencies=[Depends(require_studio_admin)])
async def studio_extract_ticket(body: StudioTicketExtractionRequest, x_aops_api_key: str | None = Header(default=None, alias="X-AOPS-Api-Key")):
    if not x_aops_api_key:
        raise HTTPException(422, "请提供X-AOPS-Api-Key")
    try:
        ticket, timeline = await fetch_ticket_evidence(body.ticketId, x_aops_api_key)
        payload = {"knowledgeId": body.knowledgeId, "requestId": body.requestId, "ticketId": body.ticketId, "ticketInfo": ticket, "auditTimeline": timeline}
        return submit_extraction(payload, body.syncTec01, True)
    except ValueError as exc:
        raise invalid(exc) from exc


@app.post("/api/v1/studio/workflows/validate", dependencies=[Depends(require_studio_admin)])
async def studio_validate(body: RuntimeValidateRequest):
    try:
        return validated(body)
    except ValueError as exc:
        raise invalid(exc) from exc


@app.post("/api/v1/studio/workflows/plan", dependencies=[Depends(require_studio_admin)])
async def studio_plan(body: dict[str, Any]):
    try:
        ticket_id = body.get("ticketId")
        if isinstance(ticket_id, bool) or not isinstance(ticket_id, int) or ticket_id <= 0:
            raise ValueError("工单ID必须是正整数")
        inputs = body.get("inputs") or {}
        reject_credentials(inputs)
        result = validate_workflow(body["workflowDefinition"], "TEST")
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


def dispatch(body: ExecutionDispatchRequest, key: str | None, *, local: bool, sync: bool):
    if sync:
        configured_callbacks()
    payload = body.model_dump(mode="json", exclude={"syncTec01"})
    reject_credentials({"workflowSnapshot": payload["workflowSnapshot"], "runInputs": payload["runInputs"]})
    nodes = payload["workflowSnapshot"].get("nodes") or []
    if not isinstance(nodes, list) or not all(isinstance(node, dict) for node in nodes):
        raise ValueError("workflowSnapshot.nodes必须为节点对象数组")
    if any(node.get("type") == "sql_read" for node in nodes) and not key:
        raise ValueError("包含SQL读节点时必须通过X-AOPS-Api-Key提供当前用户凭据")
    async def sink(event):
        await studio_events.publish(body.runId, event)
    accepted, created = push_executor.dispatch(payload, key, event_sink=sink if local else None, sync_tec01=sync)
    if local and created:
        studio_events.open(body.runId, reset=True)
    return accepted


@app.post("/api/v1/studio/executions/dispatch", dependencies=[Depends(require_studio_admin)])
async def studio_dispatch(body: StudioExecutionRequest, x_aops_api_key: str | None = Header(default=None, alias="X-AOPS-Api-Key")):
    try:
        return dispatch(body, x_aops_api_key, local=True, sync=body.syncTec01)
    except (ValueError, RuntimeError) as exc:
        if "NO_CAPACITY" in str(exc):
            raise HTTPException(429, "Executor当前无可用容量", headers={"Retry-After": "3"}) from exc
        raise invalid(exc) from exc


@app.post("/api/v1/studio/executions/{run_id}/commands", dependencies=[Depends(require_studio_admin)])
async def studio_command(run_id: str, body: ExecutionCommandRequest):
    return push_executor.command(run_id, body.commandId, body.type)


@app.get("/api/v1/studio/events/{stream_id}", dependencies=[Depends(require_studio_admin)])
async def events(stream_id: str, after: int = Query(default=0, ge=0)):
    try:
        result = studio_events.get(stream_id, after)
        record = compiler_service.get(stream_id)
        if record:
            result.update({key: record[key] for key in ("syncStatus", "callbackError") if key in record})
        return result
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
        return validated(body)
    except ValueError as exc:
        raise invalid(exc) from exc


def schema_catalog():
    models = {"workflowDefinition": WorkflowDefinition, "dispatch": ExecutionDispatchRequest, "extraction": ExtractionSubmitRequest, "singleNode": SingleNodeRequest, "singleNodeResponse": SingleNodeResponse}
    dialect = "https://json-schema.org/draft/2020-12/schema"
    schemas = {name: {"$schema": dialect, **model.model_json_schema()} for name, model in models.items()}
    definitions = schemas["workflowDefinition"].get("$defs", {})
    definitions["WorkflowNode"]["properties"]["type"]["enum"] = [node["type"] for node in NODE_REGISTRY.catalog()["nodes"]]
    for name, field, reference in (("dispatch", "workflowSnapshot", "WorkflowDefinition"), ("singleNode", "node", "WorkflowNode")):
        schemas[name]["$defs"] = {**definitions, "WorkflowDefinition": {key: value for key, value in schemas["workflowDefinition"].items() if key not in {"$defs", "$schema"}}}
        schemas[name]["properties"][field] = {"$ref": f"#/$defs/{reference}"}
    return {"runtimeVersion": __version__, "jsonSchemaDialect": dialect, "schemas": schemas, "nodes": NODE_REGISTRY.catalog()["nodes"]}


@app.get("/internal/v1/runtime/schemas", dependencies=[Depends(runtime_service)])
async def runtime_schemas():
    return schema_catalog()


@app.get("/internal/v1/runtime/schemas/{schema_name}", dependencies=[Depends(runtime_service)])
async def runtime_schema(schema_name: str):
    schema = schema_catalog()["schemas"].get(schema_name)
    if schema is None:
        raise HTTPException(404, {"code": "SCHEMA_NOT_FOUND", "message": "该约束名称不存在", "retryable": False})
    return schema


@app.post("/internal/v1/runtime/nodes/execute", response_model=SingleNodeResponse, dependencies=[Depends(runtime_service)])
async def runtime_node_execute(body: SingleNodeRequest, x_aops_api_key: str | None = Header(default=None, alias="X-AOPS-Api-Key")):
    try:
        reject_credentials({"node": body.node, "inputs": body.inputs, "resumePayload": body.resumePayload})
        return await single_executor.execute(body.model_dump(mode="json"), x_aops_api_key)
    except (ValueError, RuntimeError) as exc:
        if "NO_CAPACITY" in str(exc):
            raise HTTPException(429, {"code": "EXECUTOR_NO_CAPACITY", "message": "Executor当前无可用容量", "retryable": True}, headers={"Retry-After": "3"}) from exc
        raise invalid(exc) from exc


@app.post("/internal/v1/runtime/workflows/plan", dependencies=[Depends(runtime_service)])
async def runtime_plan(body: RuntimePlanRequest):
    try:
        return render_plan(workflow_version_id=body.workflowVersionId, workflow_content_hash=body.workflowContentHash, workflow_snapshot=body.workflowSnapshot, ticket_id=body.ticketId, parameters=body.parameters, actor_uid=body.actorUid)
    except ValueError as exc:
        raise invalid(exc) from exc


@app.post("/internal/v1/extractions/{knowledge_id}", status_code=202, dependencies=[Depends(runtime_service)])
async def extraction(knowledge_id: str, body: ExtractionSubmitRequest):
    try:
        return submit_extraction({"knowledgeId": knowledge_id, **body.model_dump(mode="json")}, True, False)
    except ValueError as exc:
        raise invalid(exc) from exc


@app.post("/internal/v1/executions/dispatch", dependencies=[Depends(runtime_service)])
async def execution_dispatch(body: ExecutionDispatchRequest, x_aops_api_key: str | None = Header(default=None, alias="X-AOPS-Api-Key")):
    try:
        return dispatch(body, x_aops_api_key, local=False, sync=True)
    except (ValueError, RuntimeError) as exc:
        if "NO_CAPACITY" in str(exc):
            raise HTTPException(429, {"code": "EXECUTOR_NO_CAPACITY", "message": "Executor当前无可用容量", "retryable": True}, headers={"Retry-After": "3"}) from exc
        raise invalid(exc) from exc


@app.post("/internal/v1/executions/{run_id}/commands", dependencies=[Depends(runtime_service)])
async def execution_command(run_id: str, body: ExecutionCommandRequest):
    return push_executor.command(run_id, body.commandId, body.type)


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
