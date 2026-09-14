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
