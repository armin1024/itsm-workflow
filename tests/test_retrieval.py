import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db import Base
from app.knowledge import create_knowledge, match_knowledge, publish_knowledge, submit_knowledge_review
from app import retrieval
from app.schemas import KnowledgeCreate


@pytest.mark.asyncio
async def test_published_knowledge_uses_embedding_and_rerank(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'retrieval.db'}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    monkeypatch.setattr(retrieval.settings, "embedding_base_url", "http://embedding")
    monkeypatch.setattr(retrieval.settings, "rerank_base_url", "http://rerank")

    async def fake_embed(texts):
        return [[1.0, 0.0] for _ in texts]

    async def fake_rerank(query, documents, top_n):
        return [(index, 0.91 - index * 0.1) for index in range(min(top_n, len(documents)))]

    monkeypatch.setattr(retrieval, "embed", fake_embed)
    monkeypatch.setattr(retrieval, "rerank", fake_rerank)
    body = KnowledgeCreate.model_validate({
        "name": "客户信息查询", "summary": "按客户姓名查询基础资料", "matchPhrases": ["客户查询"],
        "workflowDefinition": {"entryNodeId": "sql-1", "nodes": [{"id": "sql-1", "type": "sql_read", "title": "查询客户", "config": {"databaseRef": "1/db/db/read/svc", "sqlTemplate": "SELECT 1"}, "inputs": []}], "edges": []},
    })
    async with sessions() as session:
        record = await create_knowledge(session, body, "creator")
        await submit_knowledge_review(session, record, "creator")
        await publish_knowledge(session, record.id, "creator")
    async with sessions() as session:
        items, diagnostics = await match_knowledge(session, "any", "查询客户王五信息", [], 3)
        assert items[0]["name"] == "客户信息查询"
        assert items[0]["vectorScore"] == 1.0
        assert items[0]["rerankScore"] == 0.91
        assert diagnostics["retrievalMode"] == "hybrid"
    await engine.dispose()


@pytest.mark.asyncio
async def test_rerank_auto_supports_inference_gateway_tei(monkeypatch):
    monkeypatch.setattr(retrieval.settings, "rerank_base_url", "http://gateway/v1")
    monkeypatch.setattr(retrieval.settings, "rerank_api_format", "auto")
    calls = []

    async def fake_post(url, body, api_key=""):
        calls.append(body)
        if "texts" in body:
            return [{"index": 0, "score": 0.88}]
        raise retrieval.RetrievalError("unsupported payload")

    monkeypatch.setattr(retrieval, "_post", fake_post)
    result = await retrieval.rerank("客户查询", ["客户信息查询"], 1)
    assert result == [(0, 0.88)]
    assert any("texts" in body for body in calls)
