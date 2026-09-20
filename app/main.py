from __future__ import annotations

import json
import secrets
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

from fastapi import Cookie, Depends, FastAPI, Header, HTTPException, Response
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

from app import __version__
from app.cli import CliExecutionError, inspect_cli
from app.config import settings
from app.extraction import compile_ticket_evidence, compile_ticket_id
from app.runtime import NODE_REGISTRY
from app.runtime.planner import render_plan, validate_workflow
from app.runtime.workflow_debugger import debug_workflow
from app.schemas import CompilerPreviewRequest, CompilerTicketRequest, RuntimePlanRequest, RuntimeValidateRequest, StudioInterruptReply, StudioLoginRequest, StudioNodeDebugRequest, StudioNodeUpdate, StudioWorkflowDebugRequest, StudioWorkspaceCreate
from app.studio.auth import COOKIE_NAME, create_session, require_studio_admin, valid_session, verify_admin_token
from app.studio.debug import reject_credentials, run_single_node
from app.studio.store import studio_store


@asynccontextmanager
async def lifespan(_: FastAPI):
    settings.validate_service()
    await studio_store.initialize()
    await studio_store.cleanup()
    yield


app = FastAPI(
    title="ITSM Workflow Runtime",
    description="Node Registry、Workflow Compiler、执行适配器和TEST_ONLY Studio",
    version=__version__,
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
)


async def runtime_service(authorization: str | None = Header(default=None)) -> None:
    if not settings.runtime_service_token:
        return
    expected = "Bearer " + settings.runtime_service_token
    if not authorization or not secrets.compare_digest(authorization, expected):
        raise HTTPException(401, "Runtime服务身份无效")


def _compile_result(proposal: dict[str, Any], diagnostics: dict[str, Any], target_digest: str | None) -> dict[str, Any]:
    catalog = NODE_REGISTRY.catalog()
    if target_digest and target_digest != catalog["catalogDigest"]:
        raise HTTPException(409, {"code": "CATALOG_DIGEST_CONFLICT", "message": "目标Catalog已变化", "retryable": True})
    try:
        validated = validate_workflow(proposal["workflowDefinition"], "DRAFT")
    except ValueError as exc:
        raise HTTPException(422, {"code": "WORKFLOW_VALIDATION_FAILED", "message": str(exc), "retryable": False}) from exc
    proposal["workflowDefinition"] = validated["normalizedDefinition"]
    return {"compilerVersion": __version__, "catalogDigest": catalog["catalogDigest"], "proposal": proposal, "diagnostics": diagnostics}


async def _studio_catalog() -> dict[str, Any]:
    catalog = NODE_REGISTRY.catalog()
    settings_by_node = await studio_store.node_settings()
    nodes = []
    for source in catalog["nodes"]:
        node = dict(source)
        override = settings_by_node.get((node["type"], int(node["schemaVersion"])))
        node["studioEnabled"] = True if override is None else override["enabled"]
        if override:
            node["name"] = override["name"]
            node["description"] = override["description"]
            ui = dict(node.get("uiSchema") or {})
            ui.update({key: override[key] for key in ("debugConfig", "debugInputs", "debugFixture")})
            node["uiSchema"] = ui
            node["studioUpdatedAt"] = override["updatedAt"]
        nodes.append(node)
    return {**catalog, "nodes": nodes, "runtimeVersion": __version__, "testOnly": True}


async def _ensure_studio_nodes_enabled(nodes: list[dict[str, Any]]) -> None:
    overrides = await studio_store.node_settings()
    disabled = [str(node.get("type")) for node in nodes if not overrides.get((str(node.get("type")), int(node.get("schemaVersion") or 1)), {"enabled": True})["enabled"]]
    if disabled:
        raise ValueError("Studio已停用节点：" + "、".join(sorted(set(disabled))))


@app.get("/api/v1/health")
async def health() -> dict[str, Any]:
    try:
        metadata = await inspect_cli()
        cli = {"status": "ok", "version": metadata.version, "sha256": metadata.sha256}
    except CliExecutionError as exc:
        cli = {"status": "unavailable", "errorCode": exc.code, "message": str(exc)}
    return {
        "status": "ok",
        "version": __version__,
        "environment": settings.environment,
        "role": "runtime-compiler-studio",
        "authentication": "none",
        "cli": cli,
        "tec01WorkerEnabled": settings.tec01_enabled,
    }


@app.post("/api/v1/studio/session")
async def studio_login(body: StudioLoginRequest, response: Response) -> dict[str, Any]:
    if not verify_admin_token(body.token):
        raise HTTPException(401, "管理Token无效")
    response.set_cookie(COOKIE_NAME, create_session(), max_age=settings.studio_session_hours * 3600, httponly=True, secure=settings.studio_cookie_secure, samesite="strict", path="/")
    return {"authenticated": True, "expiresInSeconds": settings.studio_session_hours * 3600}


@app.get("/api/v1/studio/session")
async def studio_session(itsm_runtime_studio: str | None = Cookie(default=None, alias=COOKIE_NAME)) -> dict[str, Any]:
    return {"authenticated": valid_session(itsm_runtime_studio)}


@app.delete("/api/v1/studio/session", status_code=204)
async def studio_logout(response: Response) -> None:
    response.delete_cookie(COOKIE_NAME, path="/")


@app.get("/api/v1/studio/catalog", dependencies=[Depends(require_studio_admin)])
async def studio_catalog() -> dict[str, Any]:
    return await _studio_catalog()


@app.get("/api/v1/studio/nodes", dependencies=[Depends(require_studio_admin)])
async def studio_nodes() -> dict[str, Any]:
    catalog = await _studio_catalog()
    return {"items": catalog["nodes"], "total": len(catalog["nodes"]), "catalogDigest": catalog["catalogDigest"]}


@app.patch("/api/v1/studio/nodes/{node_type}/{schema_version}", dependencies=[Depends(require_studio_admin)])
async def studio_node_update(node_type: str, schema_version: int, body: StudioNodeUpdate) -> dict[str, Any]:
    try:
        NODE_REGISTRY.get(node_type, schema_version)
        reject_credentials(body.model_dump(mode="json"))
        return await studio_store.update_node_setting(node_type, schema_version, body.model_dump(mode="json"))
    except (ValueError, KeyError) as exc:
        raise HTTPException(422, str(exc)) from exc


@app.get("/api/v1/studio/workspaces", dependencies=[Depends(require_studio_admin)])
async def studio_workspace_list() -> dict[str, Any]:
    await studio_store.cleanup()
    items = await studio_store.list_workspaces("local-studio")
    return {"items": items, "total": len(items)}


@app.post("/api/v1/studio/workspaces", dependencies=[Depends(require_studio_admin)])
async def studio_workspace_create(body: StudioWorkspaceCreate) -> dict[str, Any]:
    try:
        reject_credentials(body.draft)
        return await studio_store.create_workspace(body.name.strip(), "local-studio", body.draft)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.post("/api/v1/studio/node-debug-runs", dependencies=[Depends(require_studio_admin)])
async def studio_node_debug_create(body: StudioNodeDebugRequest, x_aops_api_key: str | None = Header(default=None, alias="X-AOPS-Api-Key")) -> dict[str, Any]:
    try:
        await _ensure_studio_nodes_enabled([body.node])
        result = await run_single_node(node=body.node, inputs=body.inputs, mode=body.mode, simulation=body.simulation, api_key=x_aops_api_key, ticket_id=body.ticketId)
        manifest = NODE_REGISTRY.get(str(body.node.get("type")), int(body.node.get("schemaVersion") or 1)).manifest
        return await studio_store.create_debug_run(creator_uid="local-studio", workspace_id=body.workspaceId, node=body.node, inputs=body.inputs, mode=body.mode, handler_version=manifest.handler_version, status=result["status"], output=result["output"], diagnostic=result["diagnostic"], interrupt=result["interrupt"])
    except (ValueError, KeyError) as exc:
        raise HTTPException(422, str(exc)) from exc


@app.get("/api/v1/studio/node-debug-runs/{debug_run_id}", dependencies=[Depends(require_studio_admin)])
async def studio_node_debug_get(debug_run_id: str) -> dict[str, Any]:
    try:
        return await studio_store.get_debug_run(debug_run_id, "local-studio", True)
    except KeyError as exc:
        raise HTTPException(404, "调试记录不存在或已过期") from exc


@app.post("/api/v1/studio/node-debug-runs/{debug_run_id}/interrupts/reply", dependencies=[Depends(require_studio_admin)])
async def studio_node_debug_reply(debug_run_id: str, body: StudioInterruptReply) -> dict[str, Any]:
    try:
        reject_credentials(body.response)
        return await studio_store.resolve_interrupt(debug_run_id, "local-studio", body.response)
    except KeyError as exc:
        raise HTTPException(404, "调试记录不存在或已过期") from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.post("/api/v1/studio/compiler/preview", dependencies=[Depends(require_studio_admin)])
async def studio_compiler_preview(body: CompilerPreviewRequest) -> dict[str, Any]:
    try:
        proposal, diagnostics = await compile_ticket_evidence(body.ticketInfo, body.auditTimeline)
        return _compile_result(proposal, diagnostics, body.targetCatalogDigest)
    except ValueError as exc:
        raise HTTPException(422, {"code": "COMPILER_FAILED", "message": str(exc), "retryable": False}) from exc


@app.post("/api/v1/studio/compiler/from-ticket", dependencies=[Depends(require_studio_admin)])
async def studio_compiler_from_ticket(body: CompilerTicketRequest, x_aops_api_key: str | None = Header(default=None, alias="X-AOPS-Api-Key")) -> dict[str, Any]:
    if not x_aops_api_key:
        raise HTTPException(422, "从工单提取必须通过X-AOPS-Api-Key请求头提供AOPS_API_KEY")
    try:
        proposal, diagnostics = await compile_ticket_id(body.ticketId, x_aops_api_key)
        return _compile_result(proposal, diagnostics, body.targetCatalogDigest)
    except ValueError as exc:
        raise HTTPException(422, {"code": "COMPILER_FAILED", "message": str(exc), "retryable": False}) from exc


@app.post("/api/v1/studio/workflows/validate", dependencies=[Depends(require_studio_admin)])
async def studio_workflow_validate(body: RuntimeValidateRequest) -> dict[str, Any]:
    try:
        return validate_workflow(body.workflowDefinition, body.validationMode)
    except ValueError as exc:
        raise HTTPException(422, {"code": "WORKFLOW_VALIDATION_FAILED", "message": str(exc), "retryable": False}) from exc


@app.post("/api/v1/studio/workflow-debug-runs", dependencies=[Depends(require_studio_admin)])
async def studio_workflow_debug(body: StudioWorkflowDebugRequest, x_aops_api_key: str | None = Header(default=None, alias="X-AOPS-Api-Key")) -> dict[str, Any]:
    try:
        reject_credentials({"workflowDefinition": body.workflowDefinition, "inputs": body.inputs, "simulation": body.simulation})
        await _ensure_studio_nodes_enabled(list(body.workflowDefinition.get("nodes") or []))
        return await debug_workflow(workflow=body.workflowDefinition, run_inputs=body.inputs, mode=body.mode, simulation=body.simulation, ticket_id=body.ticketId, api_key=x_aops_api_key)
    except (ValueError, KeyError) as exc:
        raise HTTPException(422, str(exc)) from exc


@app.get("/internal/v1/runtime/catalog", dependencies=[Depends(runtime_service)])
async def runtime_catalog(response: Response, if_none_match: str | None = Header(default=None, alias="If-None-Match")):
    catalog = {**NODE_REGISTRY.catalog(), "runtimeVersion": __version__, "generatedAt": datetime.now(UTC).isoformat()}
    etag = '"' + catalog["catalogDigest"] + '"'
    if if_none_match == etag:
        return Response(status_code=304, headers={"ETag": etag})
    response.headers["ETag"] = etag
    return catalog


@app.post("/internal/v1/runtime/workflows/validate", dependencies=[Depends(runtime_service)])
async def runtime_workflow_validate(body: RuntimeValidateRequest) -> dict[str, Any]:
    result = await studio_workflow_validate(body)
    if body.targetCatalogDigest and body.targetCatalogDigest != result["catalogDigest"]:
        raise HTTPException(409, {"code": "CATALOG_DIGEST_CONFLICT", "message": "目标Catalog已变化", "retryable": True})
    return result


@app.post("/internal/v1/runtime/workflows/plan", dependencies=[Depends(runtime_service)])
async def runtime_workflow_plan(body: RuntimePlanRequest) -> dict[str, Any]:
    try:
        return render_plan(workflow_version_id=body.workflowVersionId, workflow_content_hash=body.workflowContentHash, workflow_snapshot=body.workflowSnapshot, ticket_id=body.ticketId, parameters=body.parameters, actor_uid=body.actorUid)
    except ValueError as exc:
        raise HTTPException(422, {"code": "WORKFLOW_VALIDATION_FAILED", "message": str(exc), "retryable": False}) from exc


@app.post("/internal/v1/compiler/preview", dependencies=[Depends(runtime_service)])
async def runtime_compiler_preview(body: CompilerPreviewRequest) -> dict[str, Any]:
    return await studio_compiler_preview(body)


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
