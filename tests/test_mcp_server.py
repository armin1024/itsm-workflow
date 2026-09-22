import pytest
from mcp import Client
from starlette.testclient import TestClient

from app import mcp_server


def test_mcp_http_health_and_auth_boundary():
    with TestClient(mcp_server.app) as client:
        health = client.get("/health")
        assert health.status_code == 200 and health.json()["service"] == "itsm-workflow-mcp"
        unauthorized = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "server/discover"})
        assert unauthorized.status_code == 401


@pytest.mark.asyncio
async def test_mcp_exposes_complete_agent_workflow_toolset():
    async with Client(mcp_server.mcp) as client:
        result = await client.list_tools()
    assert [item.name for item in result.tools] == [
        "knowledge_match", "knowledge_get", "workflow_plan", "workflow_run_approve",
        "workflow_run_get", "workflow_run_wait", "workflow_run_pause",
        "workflow_run_resume", "workflow_run_cancel", "workflow_interrupt_reply",
        "workflow_hitl_form_reply", "workflow_hitl_select_reply",
        "workflow_interaction_options",
        "workflow_run_credential_refresh", "workflow_node_result_get", "workflow_node_retry",
    ]


@pytest.mark.asyncio
async def test_mcp_match_returns_structured_content(monkeypatch):
    async def fake_request(_ctx, method, path, **kwargs):
        assert method == "POST" and path == "/knowledge/match"
        assert kwargs["body"]["query"] == "客户查询"
        return {"items": [{"knowledgeId": "knw_1", "name": "客户信息查询"}], "selectionRequired": False}

    monkeypatch.setattr(mcp_server, "_request", fake_request)
    async with Client(mcp_server.mcp) as client:
        result = await client.call_tool("knowledge_match", {"ticket_id": 100173, "query": "客户查询"})
    assert result.is_error is False
    assert result.structured_content["ticketId"] == 100173
    assert result.structured_content["items"][0]["knowledgeId"] == "knw_1"


@pytest.mark.asyncio
async def test_mcp_wait_is_compact_and_result_is_paginated(monkeypatch):
    async def fake_request(_ctx, _method, path, **_kwargs):
        if path.endswith("/wait"):
            return {"runId": "run_1", "status": "RUNNING", "terminal": False, "progress": {"current": 1, "total": 2}, "events": [{"nodeId": "sql-1", "status": "SUCCEEDED", "safeSummary": "查询完成"}]}
        return {"data": {"input": {"databaseRef": "db", "sql": "SELECT 1"}, "output": {"data": [{"id": 1}, {"id": 2}], "stream": {"title": ["id"], "done": "!ok"}}}}

    monkeypatch.setattr(mcp_server, "_request", fake_request)
    async with Client(mcp_server.mcp) as client:
        waited = await client.call_tool("workflow_run_wait", {"run_id": "run_1", "after_event_id": 0, "wait_seconds": 0})
        result = await client.call_tool("workflow_node_result_get", {"run_id": "run_1", "node_id": "sql-1", "offset": 1, "limit": 1})
    assert "查询完成" in waited.structured_content["displayText"]
    assert waited.structured_content["agentDirective"] == "REPORT_DISPLAY_TEXT_BEFORE_NEXT_WAIT"
    assert result.structured_content["rows"] == [{"id": 2}]
    assert result.structured_content["hasMore"] is False


@pytest.mark.asyncio
async def test_mcp_wait_directs_agent_to_hitl_options(monkeypatch):
    async def fake_request(_ctx, _method, path, **_kwargs):
        assert path.endswith("/wait")
        return {"runId": "run_1", "status": "WAITING_INPUT", "terminal": False, "progress": {"current": 1, "total": 3}, "events": [], "interaction": {"interruptId": "int_1", "kind": "HITL_SELECT", "title": "选择客户", "optionCount": 2}}
    monkeypatch.setattr(mcp_server, "_request", fake_request)
    async with Client(mcp_server.mcp) as client:
        waited = await client.call_tool("workflow_run_wait", {"run_id": "run_1", "after_event_id": 0, "wait_seconds": 0})
    assert waited.structured_content["agentDirective"] == "FETCH_INTERACTION_OPTIONS"
    assert waited.structured_content["requiredNextToolCalls"][0]["tool"] == "workflow_interaction_options"


@pytest.mark.asyncio
async def test_typed_hitl_tools_wrap_payload_without_guessing(monkeypatch):
    requests = []
    async def fake_request(_ctx, method, path, **kwargs):
        requests.append((method, path, kwargs))
        if method == "GET":
            return {"interrupts": [{"interruptId": "int_1", "kind": "HITL_FORM", "status": "OPEN"}, {"interruptId": "int_2", "kind": "HITL_SELECT", "status": "OPEN"}]}
        return {"status": "QUEUED"}
    monkeypatch.setattr(mcp_server, "_request", fake_request)
    async with Client(mcp_server.mcp) as client:
        form = await client.call_tool("workflow_hitl_form_reply", {"run_id": "run_1", "values": {"bot_id": "bot-1"}})
        selected = await client.call_tool("workflow_hitl_select_reply", {"run_id": "run_1", "candidate_ids": ["candidate_1"]})
    assert form.is_error is False and selected.is_error is False
    posts = [item for item in requests if item[0] == "POST"]
    assert posts[0][1].endswith("/interrupts/int_1/resume") and posts[0][2]["body"] == {"payload": {"action": "SUBMIT", "values": {"bot_id": "bot-1"}}}
    assert posts[1][1].endswith("/interrupts/int_2/resume") and posts[1][2]["body"] == {"payload": {"action": "SELECT", "candidateIds": ["candidate_1"]}}
    assert posts[0][2]["idempotency_key"].startswith("mcp-hitl-form-submit-")
