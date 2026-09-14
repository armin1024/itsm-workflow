from collections.abc import AsyncIterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.auth import Principal, current_principal
from app.config import settings
from app.db import Base, get_session
from app.knowledge import create_knowledge
from app.main import app
from app.schemas import KnowledgeCreate


@pytest.mark.asyncio
async def test_operator_owns_draft_and_service_token_can_review(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'review.db'}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    body = KnowledgeCreate.model_validate({
        "name": "客户查询", "summary": "查询客户信息", "matchPhrases": ["客户查询"], "uids": ["another-user"],
        "workflowDefinition": {"entryNodeId": "sql-1", "nodes": [{"id": "sql-1", "type": "sql_read", "title": "查询", "config": {"databaseRef": "1/db/db/read/svc", "sqlTemplate": "SELECT 1"}, "inputs": []}], "edges": []},
    })
    async with sessions() as session:
        record = await create_knowledge(session, body, "operator-1")
        knowledge_id = record.id

    async def session_override() -> AsyncIterator[AsyncSession]:
        async with sessions() as session:
            yield session

    async def operator_override() -> Principal:
        return Principal("operator-1", "key", False, True)

    app.dependency_overrides[get_session] = session_override
    app.dependency_overrides[current_principal] = operator_override
    client = TestClient(app)
    async def fake_extract(_session, *, ticket_id, uids, creator_uid, api_key):
        assert ticket_id == 100173 and creator_uid == "operator-1" and api_key == "key"
        return record, {"ticketId": ticket_id, "acceptedOperationCount": 1}

    monkeypatch.setattr("app.main.extract_ticket_draft", fake_extract)
    extracted = client.post("/api/v1/knowledge/extract", json={"ticketId": 100173, "uids": []})
    assert extracted.status_code == 200
    detail = client.get(f"/api/v1/knowledge/{knowledge_id}")
    assert detail.status_code == 200 and detail.json()["creatorUid"] == "operator-1"
    submitted = client.post(f"/api/v1/knowledge/{knowledge_id}/submit-review", json={})
    assert submitted.status_code == 200 and submitted.json()["status"] == "PENDING_REVIEW"

    monkeypatch.setattr(settings, "workflow_review_token", "review-secret")
    monkeypatch.setattr(settings, "workflow_review_actor_uid", "review-platform")
    review_headers = {"Authorization": "Bearer review-secret"}
    queue = client.get("/api/v1/reviews/knowledge", headers=review_headers)
    assert queue.status_code == 200 and queue.json()["items"][0]["knowledgeId"] == knowledge_id
    published = client.post(f"/api/v1/knowledge/{knowledge_id}/publish", json={}, headers=review_headers)
    assert published.status_code == 200
    reviewed = client.get(f"/api/v1/knowledge/{knowledge_id}")
    assert reviewed.status_code == 404  # creator is not automatically authorized to use a private published workflow
    app.dependency_overrides.clear()
    await engine.dispose()
