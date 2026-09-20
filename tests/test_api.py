from collections.abc import AsyncIterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.auth import Principal, current_principal
from app.db import Base, get_session
from app.main import _reviewer, app
from app.models import WorkflowRun
from app.studio.store import studio_store


@pytest.mark.asyncio
async def test_api_creates_versioned_plan_and_requires_hash(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'api.db'}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    async def session_override() -> AsyncIterator[AsyncSession]:
        async with sessions() as session:
            yield session

    async def principal_override() -> Principal:
        return Principal("S000001", "secret-key", True, True)

    app.dependency_overrides[get_session] = session_override
    app.dependency_overrides[current_principal] = principal_override
    app.dependency_overrides[_reviewer] = principal_override
    monkeypatch.setattr("app.main.SessionLocal", sessions)
    monkeypatch.setattr("app.main.settings.studio_enabled", True)
    monkeypatch.setattr("app.main.settings.runtime_internal_enabled", True)
    monkeypatch.setattr(studio_store, "path", tmp_path / "studio.db")
    await studio_store.initialize()
    client = TestClient(app)
    workflow = {
        "schemaVersion": 1,
        "entryNodeId": "sql-1",
        "nodes": [{"id": "sql-1", "type": "sql_read", "title": "查询客户", "config": {"databaseRef": "1/db/db/read/svc", "sqlTemplate": "SELECT id FROM users WHERE name={{name}}"}, "inputs": [{"name": "name", "type": "string", "source": {"kind": "RUN_INPUT", "key": "name"}}]}],
        "edges": [],
    }
    knowledge_body = {"name": "客户查询", "summary": "按姓名查询客户", "matchPhrases": ["客户查询"], "workflowDefinition": workflow}
    catalog = client.get("/internal/v1/runtime/catalog")
    assert catalog.status_code == 200 and any(item["type"] == "llm_extract" for item in catalog.json()["nodes"])
    validated = client.post("/internal/v1/runtime/workflows/validate", json={"workflowDefinition": workflow, "validationMode": "DRAFT"})
    assert validated.status_code == 200 and validated.json()["valid"] is True
    debugged = client.post("/api/v1/studio/node-debug-runs", json={"node": {"id": "sql", "type": "sql_read", "title": "测试查询", "config": {"databaseRef": "test/db", "sqlTemplate": "SELECT 1"}, "inputs": []}, "inputs": {}, "mode": "SIMULATION", "simulation": {"fixtureOutput": {"status": 0, "data": [{"value": 1}], "rowCount": 1}}})
    assert debugged.status_code == 200 and debugged.json()["testOnly"] is True
    created = client.post("/api/v1/knowledge", json=knowledge_body)
    assert created.status_code == 200
    knowledge_id = created.json()["knowledgeId"]
    submitted = client.post(f"/api/v1/knowledge/{knowledge_id}/submit-review", json={})
    assert submitted.status_code == 200 and submitted.json()["status"] == "PENDING_REVIEW"
    published = client.post(f"/api/v1/knowledge/{knowledge_id}/publish", json={})
    assert published.status_code == 200 and published.json()["version"] == 1
    versions = client.get(f"/api/v1/workflow-versions?knowledgeId={knowledge_id}")
    assert versions.status_code == 200 and versions.json()["items"][0]["current"] is True
    detail = client.get(f"/api/v1/knowledge/{knowledge_id}").json()
    assert detail["lastPublishedAt"] and any(item["eventType"] == "PUBLISHED" for item in detail["lifecycle"])
    exact = client.get(f"/api/v1/knowledge?knowledgeId={knowledge_id}&creatorUid=S000001")
    assert exact.status_code == 200 and exact.json()["total"] == 1
    plan_body = {"knowledgeId": knowledge_id, "ticketId": 100173, "parameters": {"name": "王五"}}
    planned = client.post("/api/v1/runs/plan", json=plan_body, headers={"Idempotency-Key": "plan-1"})
    assert planned.status_code == 200 and planned.json()["status"] == "WAITING_PLAN_APPROVAL"
    run_id, plan_hash = planned.json()["runId"], planned.json()["planHash"]
    duplicate = client.post("/api/v1/runs/plan", json=plan_body, headers={"Idempotency-Key": "plan-1"})
    assert duplicate.status_code == 200 and duplicate.json()["runId"] == run_id
    conflict = client.post("/api/v1/runs/plan", json={**plan_body, "ticketId": 100174}, headers={"Idempotency-Key": "plan-1"})
    assert conflict.status_code == 409 and "IDEMPOTENCY_CONFLICT" in conflict.json()["detail"]
    assert planned.json()["source"] == "POSTGRES_COMMITTED_STATE"
    assert planned.json()["revision"] >= 2 and planned.json()["lastEventId"] > 0
    rejected = client.post(f"/api/v1/runs/{run_id}/approve", json={"planHash": "0" * 64})
    assert rejected.status_code == 409
    approved = client.post(f"/api/v1/runs/{run_id}/approve", json={"planHash": plan_hash})
    assert approved.status_code == 200 and approved.json()["status"] == "QUEUED"
    waited = client.get(f"/api/v1/runs/{run_id}/wait?afterEventId=0&timeoutSeconds=0")
    assert waited.status_code == 200 and len(waited.json()["events"]) >= 2
    assert waited.json()["terminal"] is False
    assert "workflow" not in waited.json() and "attempts" not in waited.json()
    async with sessions() as session:
        failed_run = await session.get(WorkflowRun, run_id)
        failed_run.status = "FAILED"
        failed_run.node_statuses = {"sql-1": "FAILED"}
        failed_run.output_refs = {"sql-1": "stale-artifact"}
        await session.commit()
    retried = client.post(f"/api/v1/runs/{run_id}/nodes/sql-1/retry", json={"decision": "retry"})
    assert retried.status_code == 200
    assert retried.json()["status"] == "QUEUED"
    assert retried.json()["nodeStatuses"]["sql-1"] == "PENDING"
    edited = client.patch(f"/api/v1/knowledge/{knowledge_id}", json={**knowledge_body, "summary": "新草稿版本"})
    assert edited.status_code == 200 and edited.json()["status"] == "DRAFT"
    assert edited.json()["publishedVersionId"] == published.json()["workflowVersionId"]
    assert client.post(f"/api/v1/knowledge/{knowledge_id}/submit-review", json={}).status_code == 200
    republished = client.post(f"/api/v1/knowledge/{knowledge_id}/publish", json={})
    assert republished.status_code == 200 and republished.json()["version"] == 2
    deleted = client.delete(f"/api/v1/knowledge/{knowledge_id}")
    assert deleted.status_code == 200 and deleted.json()["status"] == "DELETED"
    assert client.get(f"/api/v1/knowledge/{knowledge_id}").status_code == 404
    assert client.get(f"/api/v1/runs/{run_id}").json()["knowledgeName"] == "客户查询"
    assert all(item["knowledgeId"] != knowledge_id for item in client.get("/api/v1/knowledge").json()["items"])
    assert client.get("/api/v1/knowledge?pageSize=10").status_code == 422
    assert client.get("/api/v1/knowledge?status=DELETED").status_code == 422
    assert client.get("/api/v1/runs?statusGroup=INVALID").status_code == 422
    app.dependency_overrides.clear()
    await engine.dispose()
