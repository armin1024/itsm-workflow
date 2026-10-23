import time

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.runtime.planner import validate_workflow
from app.studio.events import studio_events


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr("app.main.settings.runtime_service_token", "runtime-test")
    monkeypatch.setattr("app.main.settings.studio_admin_token", "studio-test")
    studio_events.streams.clear()
    with TestClient(app) as http:
        yield http


def login(client):
    assert client.post("/api/v1/studio/session", json={"token": "studio-test"}).status_code == 200


def workflow():
    return {"entryNodeId": "done", "nodes": [{"id": "done", "type": "end", "title": "完成"}], "edges": []}


def test_studio_token_and_no_database(client):
    assert client.get("/api/v1/studio/catalog").status_code == 401
    login(client)
    assert client.get("/api/v1/studio/catalog").status_code == 200
    assert client.get("/api/v1/health").json()["storage"] == "browser"
    for removed in ("/api/v1/auth/me", "/api/v1/knowledge", "/api/v1/runs", "/mcp", "/api/v1/studio/workspaces"):
        assert client.get(removed).status_code == 404


def test_internal_contract_auth_etag_and_error_shape(client):
    assert client.get("/internal/v1/runtime/catalog").json()["code"] == "RUNTIME_AUTH_FAILED"
    headers = {"Authorization": "Bearer runtime-test"}
    catalog = client.get("/internal/v1/runtime/catalog", headers=headers)
    assert catalog.json()["testOnly"] is False
    assert client.get("/internal/v1/runtime/catalog", headers={**headers, "If-None-Match": catalog.headers["etag"]}).status_code == 304
    invalid = client.post("/internal/v1/executions/dispatch", headers=headers, json={"secret": "must-not-echo"})
    assert invalid.status_code == 422 and "must-not-echo" not in invalid.text
    assert "code" in invalid.json() and "detail" not in invalid.json()


def test_local_dispatch_uses_same_runtime_without_tec01(client):
    login(client)
    result = validate_workflow(workflow(), "TEST")
    response = client.post("/api/v1/studio/executions/dispatch", json={"dispatchId": "local-dispatch", "runId": "local-run", "ticketId": 100173, "workflowContentHash": result["workflowContentHash"], "workflowSnapshot": result["normalizedDefinition"], "syncTec01": False})
    assert response.status_code == 200, response.text
    for _ in range(50):
        data = client.get("/api/v1/studio/events/local-run").json()
        if not data["active"]:
            break
        time.sleep(0.01)
    assert data["active"] is False
    final = data["events"][-1]
    assert final["type"] == "RUN_RELEASED" and final["status"] == "SUCCEEDED"
    assert final["snapshot"]["nodeStates"]["done"] == "SUCCEEDED"


def test_plan_preview_and_workflow_hitl_resume(client):
    login(client)
    plan = client.post("/api/v1/studio/workflows/plan", json={"workflowDefinition": workflow(), "ticketId": 100173, "inputs": {}})
    assert plan.status_code == 200 and plan.json()["valid"]
    node = {"id": "form", "type": "hitl_form", "title": "输入编号", "config": {"title": "输入编号", "fields": [{"name": "id", "label": "客户编号", "type": "integer", "required": True, "minimum": 1}]}}
    flow = {"entryNodeId": "form", "nodes": [node, {"id": "done", "type": "end", "title": "完成"}], "edges": [{"id": "next", "source": "form", "target": "done"}]}
    validated = validate_workflow(flow, "TEST")
    request = {"dispatchId": "local-hitl-open", "runId": "local-hitl", "ticketId": 100173, "workflowContentHash": validated["workflowContentHash"], "workflowSnapshot": validated["normalizedDefinition"], "syncTec01": False}
    assert client.post("/api/v1/studio/executions/dispatch", json=request).status_code == 200

    def completed_snapshot():
        for _ in range(100):
            events = client.get("/api/v1/studio/events/local-hitl").json()
            if not events["active"]:
                return events["events"][-1]["snapshot"]
            time.sleep(0.01)
        pytest.fail("本地流程未释放")

    waiting = completed_snapshot()
    assert waiting["status"] == "WAITING_INPUT" and waiting["interaction"]["kind"] == "HITL_FORM"
    invalid = {**waiting, "dispatchId": "local-hitl-invalid", "syncTec01": False, "resumePayload": {"nodeId": "form", "action": "SUBMIT", "values": {"id": 0}}}
    assert client.post("/api/v1/studio/executions/dispatch", json=invalid).status_code == 200
    assert completed_snapshot()["status"] == "FAILED"
    resume = {**waiting, "dispatchId": "local-hitl-valid", "syncTec01": False, "resumePayload": {"nodeId": "form", "action": "SUBMIT", "values": {"id": 3}}}
    assert client.post("/api/v1/studio/executions/dispatch", json=resume).status_code == 200
    result = completed_snapshot()
    assert result["status"] == "SUCCEEDED" and result["nodeOutputs"]["form"]["values"] == {"id": 3}


def test_sql_requires_ephemeral_header(client):
    login(client)
    flow = {"entryNodeId": "sql", "nodes": [{"id": "sql", "type": "sql_read", "title": "查询", "config": {"databaseRef": "db/test", "sqlTemplate": "SELECT 1"}}], "edges": []}
    validated = validate_workflow(flow, "TEST")
    response = client.post("/api/v1/studio/executions/dispatch", json={"dispatchId": "local-missing-key", "runId": "local-sql", "ticketId": 100173, "workflowSnapshot": validated["normalizedDefinition"], "workflowContentHash": validated["workflowContentHash"]})
    assert response.status_code == 422 and "X-AOPS-Api-Key" in response.text


def test_removed_node_management_and_debug_interfaces_are_not_exposed(client):
    login(client)
    paths = client.get("/openapi.json").json()["paths"]
    removed = [
        ("GET", "/api/v1/studio/nodes"),
        ("PATCH", "/api/v1/studio/nodes/sql_read/1"),
        ("POST", "/api/v1/studio/node-debug-runs"),
        ("POST", "/api/v1/studio/node-debug-runs/reply"),
        ("POST", "/internal/v1/runtime/nodes/execute"),
    ]
    for method, path in removed:
        assert path not in paths
        assert client.request(method, path, json={} if method != "GET" else None).status_code in {404, 405}
    assert "/api/v1/studio/nodes/{node_type}/{schema_version}" not in paths
    assert "/api/v1/studio/executions/dispatch" in paths


def test_optional_sync_requires_config_and_missing_stream(client, monkeypatch):
    login(client)
    monkeypatch.setattr("app.main.settings.tec01_base_url", "")
    result = validate_workflow(workflow(), "TEST")
    response = client.post("/api/v1/studio/executions/dispatch", json={"dispatchId": "sync-dispatch", "runId": "sync-run", "ticketId": 100173, "workflowContentHash": result["workflowContentHash"], "workflowSnapshot": result["normalizedDefinition"], "syncTec01": True})
    assert response.status_code == 503
    assert client.get("/api/v1/studio/events/absent").status_code == 404
