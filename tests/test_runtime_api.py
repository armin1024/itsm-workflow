from fastapi.testclient import TestClient

from app.main import app


def _login(client: TestClient) -> None:
    response = client.post("/api/v1/studio/session", json={"token": "change-me"})
    assert response.status_code == 200


def test_studio_requires_admin_token_and_removed_monolith_routes_are_absent(tmp_path, monkeypatch):
    monkeypatch.setattr("app.main.studio_store.path", tmp_path / "studio.db")
    with TestClient(app) as client:
        assert client.get("/api/v1/studio/catalog").status_code == 401
        _login(client)
        assert client.get("/api/v1/studio/catalog").status_code == 200
        assert client.get("/api/v1/auth/me").status_code == 404
        assert client.get("/api/v1/knowledge").status_code == 404
        assert client.get("/api/v1/runs").status_code == 404
        assert client.get("/mcp").status_code == 404


def test_sql_test_requires_ephemeral_header(tmp_path, monkeypatch):
    monkeypatch.setattr("app.main.studio_store.path", tmp_path / "studio.db")
    body = {
        "ticketId": 100173,
        "mode": "TEST",
        "node": {"id": "sql", "type": "sql_read", "title": "查询", "config": {"databaseRef": "test/db", "sqlTemplate": "SELECT 1"}, "inputs": []},
        "inputs": {},
    }
    with TestClient(app) as client:
        _login(client)
        response = client.post("/api/v1/studio/node-debug-runs", json=body)
        assert response.status_code == 422
        assert "AOPS_API_KEY" in response.json()["detail"]


def test_node_management_and_full_workflow_debug(tmp_path, monkeypatch):
    monkeypatch.setattr("app.main.studio_store.path", tmp_path / "studio.db")
    with TestClient(app) as client:
        assert client.post("/api/v1/studio/session", json={"token": "wrong"}).status_code == 401
        _login(client)
        catalog = client.get("/api/v1/studio/nodes").json()
        sql = next(item for item in catalog["items"] if item["type"] == "sql_read")
        updated = client.patch("/api/v1/studio/nodes/sql_read/1", json={"enabled": True, "name": "只读SQL测试", "description": "测试环境只读查询", "debugConfig": sql["uiSchema"]["debugConfig"], "debugInputs": {}, "debugFixture": {"status": 0, "data": [{"value": 9}], "rowCount": 1}})
        assert updated.status_code == 200
        merged = client.get("/api/v1/studio/catalog").json()
        assert next(item for item in merged["nodes"] if item["type"] == "sql_read")["name"] == "只读SQL测试"
        workflow = {"entryNodeId": "sql", "nodes": [{"id": "sql", "type": "sql_read", "title": "查询", "config": {"databaseRef": "test/db", "sqlTemplate": "SELECT 1"}, "inputs": []}, {"id": "done", "type": "end", "title": "完成"}], "edges": [{"id": "e1", "source": "sql", "target": "done"}]}
        response = client.post("/api/v1/studio/workflow-debug-runs", json={"workflowDefinition": workflow, "inputs": {}, "mode": "SIMULATION", "simulation": {"nodes": {"sql": {"fixtureOutput": {"status": 0, "data": [{"value": 1}], "rowCount": 1}}}}})
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["status"] == "SUCCEEDED"
        assert payload["nodeStatuses"] == {"sql": "SUCCEEDED", "done": "SUCCEEDED"}
