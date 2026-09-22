import copy
import json

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import settings
from app.db import Base
from app.knowledge import create_knowledge
from app.schemas import KnowledgeCreate
from app.transfer import export_package, export_preview, import_package, import_preview


def payload():
    return KnowledgeCreate.model_validate({
        "name": "客户查询", "summary": "按客户姓名查询客户资料", "matchPhrases": ["客户信息查询"], "uids": ["S2"],
        "workflowDefinition": {"schemaVersion": 1, "entryNodeId": "sql-1", "nodes": [{"id": "sql-1", "type": "sql_read", "title": "查询客户", "config": {"databaseRef": "1/dev/dev/read/svc", "sqlTemplate": "SELECT id FROM users WHERE name={{name}}"}, "inputs": [{"name": "name", "type": "string", "source": {"kind": "RUN_INPUT", "key": "name"}}]}], "edges": []},
    })


@pytest.mark.asyncio
async def test_native_transfer_deduplicates_paths_and_skips_exact_reimport(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "knowledge_environment_name", "development")
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'transfer.db'}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with sessions() as session:
        first = await create_knowledge(session, payload(), "S1")
        second = await create_knowledge(session, payload(), "S1")
        preview = await export_preview(session, [first.id, second.id])
        assert len(preview["databasePaths"]) == 1 and preview["databasePaths"][0]["usageCount"] == 2
        package = await export_package(session, {"knowledgeIds": [first.id, second.id], "confirmed": True, "databaseMappings": [{"sourceRef": "1/dev/dev/read/svc", "targetRef": "9/prod/prod/read/svc"}]}, "ADMIN")
        assert "AOPS_API_KEY" not in str(package) and "embedding" not in str(package)
        package = json.loads(json.dumps(package, ensure_ascii=False))
        imported_preview = await import_preview(session, {"package": package})
        assert imported_preview["items"][0]["workflowDefinition"]["nodes"][0]["config"]["databaseRef"] == "9/prod/prod/read/svc"
        assert not any("来源哈希不同" in value for value in imported_preview["warnings"])
        edited = copy.deepcopy(package)
        edited["items"][0]["definition"]["summary"] = "人工核实后修改的通用摘要"
        edited_preview = await import_preview(session, {"package": edited})
        assert edited_preview["items"][0]["contentHashMismatch"] is True
        assert any("重新计算有效哈希" in value for value in edited_preview["warnings"])
        imported = await import_package(session, {"package": package, "confirmed": True}, "ADMIN")
        assert len(imported["createdKnowledgeIds"]) == 2
        repeated = await import_package(session, {"package": package, "confirmed": True}, "ADMIN")
        assert repeated["createdKnowledgeIds"] == [] and repeated["skippedItemIndexes"] == [0, 1]
    await engine.dispose()
