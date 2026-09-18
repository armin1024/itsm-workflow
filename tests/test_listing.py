from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db import Base
from app.listing import paginated_knowledge, paginated_runs
from app.models import Knowledge, KnowledgeUser, WorkflowRun


def knowledge(index: int, *, status: str = "PUBLISHED", creator: str = "creator", public: bool = True) -> Knowledge:
    return Knowledge(
        id=f"knw_{index:04d}", status=status, name=f"客户查询 {index}", summary=f"按客户编号查询资料 {index}",
        match_phrases=[f"客户资料{index}"], negative_phrases=[], system_keys=[], creator_uid=creator,
        public=public, source_type="MANUAL", source_ticket_id=100000 + index,
        source_ticket_no=f"INC-{index:04d}", draft_definition={"nodes": [{"id": "sql-1"}], "edges": []},
        created_at=datetime.now(UTC) + timedelta(seconds=index), updated_at=datetime.now(UTC) + timedelta(seconds=index),
    )


@pytest.mark.asyncio
async def test_knowledge_pagination_boundaries_and_summary_dto(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'listing.db'}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with sessions() as session:
        session.add_all([knowledge(index) for index in range(201)])
        await session.commit()
        first = await paginated_knowledge(session, uid="admin", is_admin=True, page=1, page_size=20, keyword="", status="")
        last = await paginated_knowledge(session, uid="admin", is_admin=True, page=11, page_size=20, keyword="", status="")
        overflow = await paginated_knowledge(session, uid="admin", is_admin=True, page=999, page_size=20, keyword="", status="")
        empty = await paginated_knowledge(session, uid="admin", is_admin=True, page=7, page_size=20, keyword="missing", status="")
        exact = await paginated_knowledge(session, uid="admin", is_admin=True, page=1, page_size=20, keyword="INC-0021", status="")
    assert (first["total"], first["totalPages"], len(first["items"])) == (201, 11, 20)
    assert len(last["items"]) == 1 and overflow["page"] == 11
    assert {key: empty[key] for key in ("items", "page", "pageSize", "total", "totalPages")} == {"items": [], "page": 1, "pageSize": 20, "total": 0, "totalPages": 0}
    assert empty["appliedFilters"] == {"keyword": "missing"}
    assert exact["items"][0]["knowledgeId"] == "knw_0021"
    assert "workflowDefinition" not in first["items"][0] and first["items"][0]["nodeCount"] == 1
    await engine.dispose()


@pytest.mark.asyncio
async def test_knowledge_permissions_are_applied_before_pagination(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'permissions.db'}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    records = [
        knowledge(1, status="DRAFT", creator="user-a", public=False),
        knowledge(2, status="PENDING_REVIEW", creator="user-a", public=False),
        knowledge(3, public=True),
        knowledge(4, public=False),
        knowledge(5, creator="user-a", public=False),
        knowledge(6, public=False),
    ]
    async with sessions() as session:
        session.add_all(records)
        session.add(KnowledgeUser(knowledge_id="knw_0004", uid="user-a"))
        await session.commit()
        result = await paginated_knowledge(session, uid="user-a", is_admin=False, page=1, page_size=20, keyword="", status="")
    assert {item["knowledgeId"] for item in result["items"]} == {"knw_0001", "knw_0002", "knw_0003", "knw_0004"}
    assert result["total"] == 4
    await engine.dispose()


@pytest.mark.asyncio
async def test_exact_knowledge_filters_are_combined_and_reported(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'exact.db'}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with sessions() as session:
        first, second = knowledge(1, creator="S1"), knowledge(2, creator="S2")
        first.system_keys, first.match_phrases = ["crm"], ["客户资料"]
        session.add_all([first, second])
        await session.commit()
        result = await paginated_knowledge(session, uid="admin", is_admin=True, page=1, page_size=20, keyword="", filters={"creatorUid": ["S1"], "systemKey": ["crm"], "sourceTicketId": ["100001"]})
    assert [item["knowledgeId"] for item in result["items"]] == ["knw_0001"]
    assert result["appliedFilters"]["creatorUid"] == ["S1"]
    await engine.dispose()


@pytest.mark.asyncio
async def test_run_keyword_status_and_lightweight_page(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'runs.db'}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with sessions() as session:
        session.add(knowledge(1))
        for index in range(21):
            session.add(WorkflowRun(
                id=f"run_{index:04d}", knowledge_id="knw_0001", workflow_version_id="wfv_test",
                ticket_id=100000 + index, initiated_by="user-a" if index < 20 else "user-b",
                status="RUNNING" if index % 2 == 0 else "SUCCEEDED", plan_hash="0" * 64,
                workflow_snapshot={"nodes": []}, run_inputs={}, node_statuses={"sql-1": "SUCCEEDED" if index % 2 else "RUNNING"}, output_refs={},
                created_at=datetime.now(UTC) + timedelta(seconds=index),
            ))
        await session.commit()
        active = await paginated_runs(session, uid="user-a", is_admin=False, page=1, page_size=20, keyword="", status_group="ACTIVE")
        ticket = await paginated_runs(session, uid="admin", is_admin=True, page=1, page_size=20, keyword="100020", status_group="ALL")
        by_name = await paginated_runs(session, uid="admin", is_admin=True, page=1, page_size=20, keyword="客户查询", status_group="ALL")
    assert active["total"] == 10 and all(item["status"] == "RUNNING" for item in active["items"])
    assert ticket["items"][0]["runId"] == "run_0020"
    assert by_name["total"] == 21
    assert "workflow" not in by_name["items"][0] and "attempts" not in by_name["items"][0]
    await engine.dispose()
