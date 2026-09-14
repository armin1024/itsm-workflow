from datetime import UTC, datetime, timedelta

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.cli import CliExecutionError, CliMetadata, CliResult
from app.crypto import SecretBox, canonical_hash
from app.db import Base
from app.engine import WorkflowEngine
from app.models import RunCredential, WorkflowRun
from app.runs import update_credential
from app.workflow import WorkflowDefinition


@pytest.mark.asyncio
async def test_engine_executes_sql_and_updates_observable_state(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'engine.db'}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    definition = WorkflowDefinition.model_validate({
        "entryNodeId": "sql-1",
        "nodes": [
            {"id": "sql-1", "type": "sql_read", "title": "查询", "config": {"databaseRef": "1/db/db/read/svc", "sqlTemplate": "SELECT id FROM users WHERE name={{name}}"}, "inputs": [{"name": "name", "type": "string", "source": {"kind": "RUN_INPUT", "key": "name"}}]},
            {"id": "done", "type": "end", "title": "完成"},
        ],
        "edges": [{"id": "e1", "source": "sql-1", "target": "done"}],
    })
    run_id = "run_test"
    async with sessions() as session:
        session.add(WorkflowRun(id=run_id, knowledge_id="knw", workflow_version_id="wfv", ticket_id=100, initiated_by="uid", status="RUNNING", plan_hash=canonical_hash({}), workflow_snapshot=definition.model_dump(mode="json"), run_inputs={"name": "王五"}, node_statuses={"sql-1": "PENDING", "done": "PENDING"}, output_refs={}))
        session.add(RunCredential(id="cred", run_id=run_id, uid="uid", ciphertext=SecretBox().seal({"apiKey": "secret"}, purpose="run-credential:" + run_id), expires_at=datetime.now(UTC) + timedelta(hours=1)))
        await session.commit()

    async def fake_execute(**kwargs):
        assert kwargs["sql"] == "SELECT id FROM users WHERE name='王五'"
        return CliResult({"status": 0, "data": [{"id": 7}]}, b"{}", b"", 0, CliMetadata("test", "a" * 64))

    monkeypatch.setattr("app.engine.execute_sql_read", fake_execute)
    graph = WorkflowEngine(sessions, InMemorySaver()).compile(definition)
    await graph.ainvoke({"run_id": run_id, "inputs": {"name": "王五"}, "output_refs": {}, "routes": {}}, config={"configurable": {"thread_id": run_id}})
    async with sessions() as session:
        run = await session.get(WorkflowRun, run_id)
        assert run.node_statuses == {"sql-1": "SUCCEEDED", "done": "SUCCEEDED"}
        assert "sql-1" in run.output_refs
    await engine.dispose()


@pytest.mark.asyncio
async def test_condition_executes_one_branch_and_marks_other_skipped(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'condition.db'}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    definition = WorkflowDefinition.model_validate({
        "entryNodeId": "route",
        "nodes": [
            {"id": "route", "type": "condition", "title": "状态判断"},
            {"id": "active", "type": "end", "title": "正常结束"},
            {"id": "inactive", "type": "end", "title": "停用结束"},
        ],
        "edges": [
            {"id": "active-edge", "source": "route", "target": "active", "condition": {"path": "/inputs/status", "op": "eq", "value": "ACTIVE"}},
            {"id": "default-edge", "source": "route", "target": "inactive", "default": True},
        ],
    })
    run_id = "run_condition"
    async with sessions() as session:
        session.add(WorkflowRun(id=run_id, knowledge_id="knw", workflow_version_id="wfv", ticket_id=100, initiated_by="uid", status="RUNNING", plan_hash=canonical_hash({}), workflow_snapshot=definition.model_dump(mode="json"), run_inputs={"status": "ACTIVE"}, node_statuses={node.id: "PENDING" for node in definition.nodes}, output_refs={}))
        await session.commit()
    graph = WorkflowEngine(sessions, InMemorySaver()).compile(definition)
    await graph.ainvoke({"run_id": run_id, "inputs": {"status": "ACTIVE"}, "output_refs": {}, "routes": {}}, config={"configurable": {"thread_id": run_id}})
    async with sessions() as session:
        run = await session.get(WorkflowRun, run_id)
        assert run.node_statuses == {"route": "SUCCEEDED", "active": "SUCCEEDED", "inactive": "SKIPPED"}
    await engine.dispose()


@pytest.mark.asyncio
async def test_expired_credential_interrupts_and_resumes(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'credential.db'}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    definition = WorkflowDefinition.model_validate({
        "entryNodeId": "sql-1",
        "nodes": [{"id": "sql-1", "type": "sql_read", "title": "查询", "config": {"databaseRef": "1/db/db/read/svc", "sqlTemplate": "SELECT 1"}, "inputs": []}],
        "edges": [],
    })
    run_id = "run_expired"
    async with sessions() as session:
        session.add(WorkflowRun(id=run_id, knowledge_id="knw", workflow_version_id="wfv", ticket_id=1, initiated_by="uid", status="RUNNING", plan_hash=canonical_hash({}), workflow_snapshot=definition.model_dump(mode="json"), run_inputs={}, node_statuses={"sql-1": "PENDING"}, output_refs={}))
        session.add(RunCredential(id="expired", run_id=run_id, uid="uid", ciphertext=SecretBox().seal({"apiKey": "old"}, purpose="run-credential:" + run_id), expires_at=datetime.now(UTC) - timedelta(hours=1)))
        await session.commit()

    async def fake_execute(**kwargs):
        assert kwargs["api_key"] == "new"
        return CliResult({"status": 0, "data": []}, b"{}", b"", 0, CliMetadata("test", "a" * 64))

    monkeypatch.setattr("app.engine.execute_sql_read", fake_execute)
    graph = WorkflowEngine(sessions, InMemorySaver()).compile(definition)
    config = {"configurable": {"thread_id": run_id}}
    await graph.ainvoke({"run_id": run_id, "inputs": {}, "output_refs": {}, "routes": {}}, config=config)
    async with sessions() as session:
        run = await session.get(WorkflowRun, run_id)
        assert run.status == "WAITING_CREDENTIAL"
        await update_credential(session, run, "uid", "new")
    await graph.ainvoke(Command(resume={"action": "credential_updated"}), config=config)
    async with sessions() as session:
        run = await session.get(WorkflowRun, run_id)
        assert run.node_statuses["sql-1"] == "SUCCEEDED"
    await engine.dispose()


@pytest.mark.asyncio
async def test_failed_graph_node_can_resume_as_a_new_attempt(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'retry.db'}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    definition = WorkflowDefinition.model_validate({"entryNodeId": "sql-1", "nodes": [{"id": "sql-1", "type": "sql_read", "title": "查询", "config": {"databaseRef": "1/db/db/read/svc", "sqlTemplate": "SELECT 1"}, "inputs": []}], "edges": []})
    run_id = "run_retry"
    async with sessions() as session:
        session.add(WorkflowRun(id=run_id, knowledge_id="knw", workflow_version_id="wfv", ticket_id=1, initiated_by="uid", status="RUNNING", plan_hash=canonical_hash({}), workflow_snapshot=definition.model_dump(mode="json"), run_inputs={}, node_statuses={"sql-1": "PENDING"}, output_refs={}, started_at=datetime.now(UTC)))
        session.add(RunCredential(id="retry-cred", run_id=run_id, uid="uid", ciphertext=SecretBox().seal({"apiKey": "secret"}, purpose="run-credential:" + run_id), expires_at=datetime.now(UTC) + timedelta(hours=1)))
        await session.commit()
    calls = 0

    async def flaky_execute(**_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise CliExecutionError("TEMPORARY", "temporary failure")
        return CliResult({"status": 0, "data": []}, b"", b"", 0, CliMetadata("test", "a" * 64))

    monkeypatch.setattr("app.engine.execute_sql_read", flaky_execute)
    saver = InMemorySaver()
    graph = WorkflowEngine(sessions, saver).compile(definition)
    config = {"configurable": {"thread_id": run_id}}
    with pytest.raises(CliExecutionError):
        await graph.ainvoke({"run_id": run_id, "inputs": {}, "output_refs": {}, "routes": {}}, config=config)
    async with sessions() as session:
        run = await session.get(WorkflowRun, run_id)
        run.status = "RUNNING"
        run.finished_at = None
        run.node_statuses = {"sql-1": "PENDING"}
        await session.commit()
    await graph.ainvoke(None, config=config)
    async with sessions() as session:
        run = await session.get(WorkflowRun, run_id)
        assert run.node_statuses["sql-1"] == "SUCCEEDED"
    assert calls == 2
    await engine.dispose()
