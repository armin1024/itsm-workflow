from __future__ import annotations

import secrets
from typing import Any

import httpx
from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import JSONResponse

from app import __version__
from app.config import settings


INSTRUCTIONS = """ITSM生产工作流工具。先匹配经验并创建计划，完整展示计划后等待用户明确确认，
只有确认后才能批准。运行期间使用最长10秒的短等待获取事实事件；每次wait返回后，必须先把displayText
反馈给用户，再发起下一次wait。运行成功后调用workflow_node_result_get读取本次真实节点结果，禁止用记忆、
历史结果或直接aops-cli代替。FAILED和UNKNOWN禁止自动重试；UNKNOWN必须提示外部请求可能已到达AOPS。
用户可随时暂停或取消。不得直接执行aops-cli。"""

mcp = MCPServer("itsm-workflow", description="AOPS生产工作流匹配、计划、执行控制与事实观察", instructions=INSTRUCTIONS, version=__version__)


@mcp.custom_route("/health", methods=["GET"])
async def health(_: Request) -> JSONResponse:
    return JSONResponse({"status": "ok", "service": "itsm-workflow-mcp", "version": __version__})


def _headers(ctx: Context) -> dict[str, str]:
    incoming = ctx.headers or {}
    authorization = incoming.get("authorization") or incoming.get("Authorization")
    api_key = incoming.get("x-aops-api-key") or incoming.get("X-AOPS-Api-Key")
    if not authorization or not api_key:
        raise ValueError("MCP_IDENTITY_REQUIRED：连接必须提供 Authorization 和 X-AOPS-Api-Key")
    return {"Authorization": authorization, "X-AOPS-Api-Key": api_key, "Content-Type": "application/json"}


def _enrich(payload: Any) -> Any:
    if isinstance(payload, dict) and payload.get("runPath"):
        return {**payload, "runUrl": settings.workflow_public_url.rstrip("/") + str(payload["runPath"])}
    return payload


def _ticket_id(value: int) -> int:
    if value <= 0:
        raise ValueError("TICKET_ID_REQUIRED：工单ID必须为正整数")
    return value


def _idempotency_key(value: str) -> str:
    value = value.strip()
    if not value or len(value) > 120:
        raise ValueError("IDEMPOTENCY_KEY_REQUIRED：幂等键长度必须为1至120")
    return value


def _progress_text(result: dict[str, Any]) -> str:
    progress = result.get("progress") or {}
    prefix = f"工作流进度 {progress.get('current', 0)}/{progress.get('total', 0)} · {result.get('status', 'UNKNOWN')}"
    lines = [prefix]
    for event in result.get("events") or []:
        marker = "✓" if event.get("status") == "SUCCEEDED" else "–" if event.get("status") == "SKIPPED" else "✕" if event.get("status") in {"FAILED", "CANCELLED", "UNKNOWN"} else "●"
        node = f" [{event.get('nodeId')}]" if event.get("nodeId") else ""
        lines.append(f"{marker}{node} {event.get('safeSummary') or event.get('type')}")
    if len(lines) == 1:
        lines.append("本次等待没有观察到新的已提交事件。")
    if result.get("terminal") and result.get("resultAvailableNodes"):
        lines.append("运行已结束；请读取本次运行的节点结果后再向用户总结业务结果。")
    return "\n".join(lines)


async def _request(ctx: Context, method: str, path: str, *, body: dict[str, Any] | None = None, params: dict[str, Any] | None = None, idempotency_key: str | None = None) -> dict[str, Any]:
    headers = _headers(ctx)
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(20, connect=5)) as client:
            response = await client.request(method, settings.mcp_internal_api_url.rstrip("/") + path, headers=headers, json=body, params=params)
    except httpx.HTTPError as exc:
        raise ValueError(f"WORKFLOW_API_UNAVAILABLE：{type(exc).__name__}") from exc
    if response.is_error:
        try:
            detail = response.json().get("detail")
        except (ValueError, AttributeError):
            detail = None
        raise ValueError(f"WORKFLOW_API_ERROR_{response.status_code}：{detail or '请求失败'}")
    try:
        return _enrich(response.json())
    except ValueError as exc:
        raise ValueError("WORKFLOW_API_INVALID_RESPONSE：响应不是JSON") from exc


@mcp.tool(description="按当前用户权限匹配已发布工作流经验。多候选时必须让用户选择。")
async def knowledge_match(ticket_id: int, query: str, ctx: Context, target_systems: list[str] | None = None, limit: int = 3) -> dict[str, Any]:
    ticket_id = _ticket_id(ticket_id)
    result = await _request(ctx, "POST", "/knowledge/match", body={"query": query, "targetSystems": target_systems or [], "limit": limit})
    return {**result, "ticketId": ticket_id}


@mcp.tool(description="读取一个已发布经验的完整工作流、运行参数、条件和数据绑定。")
async def knowledge_get(knowledge_id: str, ticket_id: int, ctx: Context) -> dict[str, Any]:
    ticket_id = _ticket_id(ticket_id)
    result = await _request(ctx, "GET", "/knowledge/" + knowledge_id)
    if result.get("status") != "PUBLISHED":
        raise ValueError("KNOWLEDGE_NOT_FOUND：经验不存在或未发布")
    definition = result.get("workflowDefinition") or {}
    run_inputs: dict[str, dict[str, Any]] = {}
    for node in definition.get("nodes", []):
        for item in node.get("inputs", []):
            source = item.get("source") or {}
            if source.get("kind") == "RUN_INPUT" and source.get("key"):
                run_inputs.setdefault(str(source["key"]), {"key": source["key"], "type": item.get("type", "string"), "description": item.get("description", ""), "required": item.get("required", True), "usedBy": []})["usedBy"].append(node.get("id"))
    return {**result, "ticketId": ticket_id, "comment": f"#uatu-{ticket_id}", "runInputs": list(run_inputs.values())}


@mcp.tool(description="创建不可变执行计划但不执行。创建后必须向用户展示完整计划并等待确认。")
async def workflow_plan(knowledge_id: str, ticket_id: int, parameters: dict[str, Any], idempotency_key: str, ctx: Context) -> dict[str, Any]:
    return await _request(ctx, "POST", "/runs/plan", body={"knowledgeId": knowledge_id, "ticketId": _ticket_id(ticket_id), "parameters": parameters}, idempotency_key=_idempotency_key(idempotency_key))


@mcp.tool(description="在用户明确确认完整计划后，使用原planHash批准并进入执行队列。")
async def workflow_run_approve(run_id: str, plan_hash: str, idempotency_key: str, ctx: Context) -> dict[str, Any]:
    return await _request(ctx, "POST", f"/runs/{run_id}/approve", body={"planHash": plan_hash}, idempotency_key=_idempotency_key(idempotency_key))


@mcp.tool(description="读取运行、节点attempt、中断和事件的最新已提交事实快照。")
async def workflow_run_get(run_id: str, ctx: Context) -> dict[str, Any]:
    return await _request(ctx, "GET", f"/runs/{run_id}")


@mcp.tool(description="短暂等待运行的新事实事件；最多15秒。每次返回后必须先向用户展示displayText，再调用下一次等待。")
async def workflow_run_wait(run_id: str, after_event_id: int, ctx: Context, wait_seconds: float = 10) -> dict[str, Any]:
    wait_seconds = min(max(float(wait_seconds), 0), float(settings.mcp_wait_max_seconds))
    result = await _request(ctx, "GET", f"/runs/{run_id}/wait", params={"afterEventId": after_event_id, "timeoutSeconds": wait_seconds})
    required_calls = [{"tool": "workflow_node_result_get", "arguments": {"run_id": run_id, "node_id": node_id, "offset": 0, "limit": 100}} for node_id in result.get("resultAvailableNodes", [])] if result.get("terminal") and result.get("status") == "SUCCEEDED" else []
    return {**result, "displayText": _progress_text(result), "agentDirective": "FETCH_CURRENT_RUN_RESULTS" if required_calls else "REPORT_DISPLAY_TEXT_BEFORE_NEXT_WAIT", "requiredNextToolCalls": required_calls}


@mcp.tool(description="请求在下一个节点安全边界暂停；RUNNING时可能先返回PAUSE_REQUESTED。")
async def workflow_run_pause(run_id: str, idempotency_key: str, ctx: Context) -> dict[str, Any]:
    return await _request(ctx, "POST", f"/runs/{run_id}/pause", body={}, idempotency_key=_idempotency_key(idempotency_key))


@mcp.tool(description="经用户确认后继续一个已经PAUSED的运行。")
async def workflow_run_resume(run_id: str, idempotency_key: str, ctx: Context) -> dict[str, Any]:
    return await _request(ctx, "POST", f"/runs/{run_id}/resume", body={}, idempotency_key=_idempotency_key(idempotency_key))


@mcp.tool(description="经用户确认后请求取消运行；不承诺回滚已经到达AOPS的请求。")
async def workflow_run_cancel(run_id: str, idempotency_key: str, ctx: Context) -> dict[str, Any]:
    return await _request(ctx, "POST", f"/runs/{run_id}/cancel", body={}, idempotency_key=_idempotency_key(idempotency_key))


@mcp.tool(description="提交人工补参、节点批准或暂停恢复响应。调用前必须取得用户输入或确认。")
async def workflow_interrupt_reply(run_id: str, interrupt_id: str, payload: dict[str, Any], idempotency_key: str, ctx: Context) -> dict[str, Any]:
    return await _request(ctx, "POST", f"/runs/{run_id}/interrupts/{interrupt_id}/resume", body={"payload": payload}, idempotency_key=_idempotency_key(idempotency_key))


@mcp.tool(description="用户更新MCP连接中的X-AOPS-Api-Key后，用该请求头刷新运行凭据；API Key不是工具参数。")
async def workflow_run_credential_refresh(run_id: str, ctx: Context) -> dict[str, Any]:
    api_key = _headers(ctx)["X-AOPS-Api-Key"]
    return await _request(ctx, "POST", f"/runs/{run_id}/credential", body={"apiKey": api_key})


@mcp.tool(description="分页读取当前用户有权查看的本次运行SQL节点真实结果。运行成功后必须使用它总结结果，禁止引用记忆代替。")
async def workflow_node_result_get(run_id: str, node_id: str, ctx: Context, offset: int = 0, limit: int = 100) -> dict[str, Any]:
    if offset < 0 or limit < 1 or limit > 200:
        raise ValueError("RESULT_PAGE_INVALID：offset必须大于等于0，limit必须为1至200")
    artifact_response = await _request(ctx, "GET", f"/runs/{run_id}/nodes/{node_id}/artifact")
    artifact = artifact_response.get("data") or {}
    output = artifact.get("output") if isinstance(artifact, dict) else {}
    output = output if isinstance(output, dict) else {"value": output}
    rows = output.get("data")
    row_list = rows if isinstance(rows, list) else []
    total = len(row_list)
    page = row_list[offset:offset + limit]
    stream = output.get("stream") if isinstance(output.get("stream"), dict) else {}
    input_data = artifact.get("input") if isinstance(artifact, dict) and isinstance(artifact.get("input"), dict) else {}
    return {
        "runId": run_id,
        "nodeId": node_id,
        "databaseRef": input_data.get("databaseRef"),
        "sql": input_data.get("sql"),
        "columns": stream.get("title") or (list(page[0].keys()) if page and isinstance(page[0], dict) else []),
        "rows": page,
        "totalRows": total,
        "offset": offset,
        "limit": limit,
        "hasMore": offset + len(page) < total,
        "stream": {"uuid": stream.get("uuid"), "done": stream.get("done")},
        "source": "ENCRYPTED_RUN_ARTIFACT",
        "agentDirective": "SUMMARIZE_ONLY_THIS_RUN_RESULT",
    }


@mcp.tool(description="经用户确认后重试FAILED/UNKNOWN节点，或将UNKNOWN节点标记失败。禁止自动调用。")
async def workflow_node_retry(run_id: str, node_id: str, decision: str, idempotency_key: str, ctx: Context) -> dict[str, Any]:
    if decision not in {"retry", "mark_failed"}:
        raise ValueError("decision只能为retry或mark_failed")
    return await _request(ctx, "POST", f"/runs/{run_id}/nodes/{node_id}/retry", body={"decision": decision}, idempotency_key=_idempotency_key(idempotency_key))


class McpAuthenticationMiddleware:
    def __init__(self, wrapped):
        self.wrapped = wrapped

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.wrapped(scope, receive, send)
            return
        if scope.get("path") == "/health":
            await self.wrapped(scope, receive, send)
            return
        headers = {key.decode("latin1").lower(): value.decode("latin1") for key, value in scope.get("headers", [])}
        expected = "Bearer " + settings.workflow_api_token
        if not secrets.compare_digest(headers.get("authorization", ""), expected) or not headers.get("x-aops-api-key"):
            await JSONResponse({"error": "MCP_IDENTITY_REQUIRED"}, status_code=401)(scope, receive, send)
            return
        # Tool calls immediately pass both headers to the REST authorization
        # layer. Avoid resolving the same AOPS identity twice on every 10-second
        # run wait. Discovery/list requests still receive an identity preflight.
        if headers.get("mcp-method") == "tools/call":
            await self.wrapped(scope, receive, send)
            return
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(10, connect=3)) as client:
                identity_response = await client.get(
                    settings.mcp_internal_api_url.rstrip("/") + "/auth/me",
                    headers={"Authorization": headers["authorization"], "X-AOPS-Api-Key": headers["x-aops-api-key"]},
                )
            identity_body = identity_response.json() if identity_response.status_code == 200 else {}
        except (httpx.HTTPError, ValueError):
            await JSONResponse({"error": "WORKFLOW_API_UNAVAILABLE"}, status_code=503)(scope, receive, send)
            return
        if identity_response.status_code != 200 or not (identity_body.get("isOperator") or identity_body.get("isAdmin")):
            await JSONResponse({"error": "MCP_IDENTITY_FAILED"}, status_code=401)(scope, receive, send)
            return
        await self.wrapped(scope, receive, send)


transport_security = TransportSecuritySettings(
    enable_dns_rebinding_protection=True,
    allowed_hosts=settings.allowed_mcp_hosts,
    allowed_origins=[],
)
app = McpAuthenticationMiddleware(
    mcp.streamable_http_app(
        streamable_http_path=settings.mcp_path,
        stateless_http=True,
        json_response=True,
        transport_security=transport_security,
        host=settings.mcp_bind_host,
    )
)
