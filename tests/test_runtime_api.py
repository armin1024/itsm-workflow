from fastapi.testclient import TestClient

from app.main import app


def test_studio_is_open_and_removed_monolith_routes_are_absent(tmp_path, monkeypatch):
    monkeypatch.setattr("app.main.studio_store.path", tmp_path / "studio.db")
    with TestClient(app) as client:
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
        response = client.post("/api/v1/studio/node-debug-runs", json=body)
        assert response.status_code == 422
        assert "AOPS_API_KEY" in response.json()["detail"]
