import asyncio
import sys
import threading

import pytest

from app import cli
from app.config import settings
from app.resources import ResourceError, data_budget
from app.runtime.planner import validate_workflow
from app.runtime.push_executor import PushExecutor


def payload():
    validated = validate_workflow({"entryNodeId": "query", "nodes": [
        {"id": "query", "type": "sql_read", "title": "查询", "config": {"databaseRef": "fixture/db", "sqlTemplate": "SELECT 1"}},
        {"id": "done", "type": "end", "title": "完成"},
    ], "edges": [{"id": "next", "source": "query", "target": "done"}]}, "EXECUTE")
    return {"runId": "resource-run", "dispatchId": "resource-dispatch", "ticketId": 1, "workflowSnapshot": validated["normalizedDefinition"], "workflowContentHash": validated["workflowContentHash"], "runInputs": {}}


@pytest.mark.asyncio
async def test_prepare_dispatch_runs_off_loop_and_owns_input(monkeypatch):
    executor = PushExecutor()
    loop_thread = threading.get_ident()
    original = executor._prepare
    threads = []

    def prepare(value, limit):
        threads.append(threading.get_ident())
        return original(value, limit)

    monkeypatch.setattr(executor, "_prepare", prepare)
    source = payload()
    prepared = await executor.prepare_dispatch(source)
    source["runInputs"]["changed"] = True
    assert threads and all(thread != loop_thread for thread in threads)
    assert prepared["payload"]["runInputs"] == {}
    assert prepared["inputSize"] > 0


@pytest.mark.asyncio
async def test_global_run_admission_fails_without_creating_task(monkeypatch):
    monkeypatch.setattr(settings, "runtime_data_budget_mb", 256)
    monkeypatch.setattr(settings, "executor_run_memory_mb", 256)
    before = data_budget.used
    data_budget.reserve("test-capacity", 1)
    executor = PushExecutor()
    try:
        with pytest.raises(ResourceError) as failure:
            executor.dispatch(payload(), "only-test-key", sync_tec01=False)
        assert failure.value.code == "RUNTIME_NO_CAPACITY"
        assert failure.value.status_code == 429
        assert not executor.active and not executor.accepted
    finally:
        data_budget.release("test-capacity")
    assert data_budget.used == before


@pytest.mark.asyncio
async def test_task_creation_failure_returns_run_budget(monkeypatch):
    executor = PushExecutor()
    before = data_budget.used

    def unavailable(*args, **kwargs):
        raise RuntimeError("cannot create task")

    monkeypatch.setattr("app.runtime.push_executor.asyncio.create_task", unavailable)
    with pytest.raises(RuntimeError, match="cannot create"):
        executor.dispatch(payload(), "only-test-key", sync_tec01=False)
    assert not executor.active and not executor.accepted and not executor._fingerprints
    assert data_budget.used == before


@pytest.mark.asyncio
async def test_result_limit_is_explicit_failure_without_running_next_node(monkeypatch):
    monkeypatch.setattr(settings, "executor_run_memory_mb", 1)
    calls, events = [], []
    before = data_budget.used

    async def execute(**kwargs):
        calls.append(kwargs["node"]["id"])
        return {"status": "SUCCEEDED", "output": {"data": [{"value": "x" * (1024 * 1024)}], "rowCount": 1, "status": 0}, "diagnostic": None, "interrupt": None}

    async def sink(event):
        events.append(event)

    monkeypatch.setattr("app.runtime.push_executor.execute_single_node", execute)
    executor = PushExecutor()
    prepared = await executor.prepare_dispatch(payload())
    executor.dispatch(payload(), "only-test-key", prepared=prepared, event_sink=sink, sync_tec01=False)
    await executor.active["resource-run"].task
    completed = next(event for event in events if event["type"] == "NODE_COMPLETED")
    assert calls == ["query"]
    assert completed["status"] == "FAILED" and completed["result"] is None
    assert completed["diagnostic"]["errorCode"] == "RESOURCE_LIMIT"
    assert events[-1]["status"] == "FAILED"
    assert data_budget.used == before and not executor.active


@pytest.mark.asyncio
async def test_oversize_resume_is_rejected_before_execution(monkeypatch):
    monkeypatch.setattr(settings, "executor_run_memory_mb", 1)
    request = payload()
    request["nodeOutputs"] = {"old": {"data": ["x" * (1024 * 1024)]}}
    executor = PushExecutor()
    with pytest.raises(ResourceError) as failure:
        await executor.prepare_dispatch(request)
    assert failure.value.code == "RESOURCE_LIMIT"
    assert not executor.active


@pytest.mark.asyncio
async def test_cli_metadata_is_singleflight_and_invalidated_by_file_change(tmp_path, monkeypatch):
    executable, count = tmp_path / "aops-cli", tmp_path / "versions"
    executable.write_text(f"#!{sys.executable}\nfrom pathlib import Path\np=Path({str(count)!r})\np.write_text(p.read_text()+'v\\n' if p.exists() else 'v\\n')\nprint('test-cli')\n")
    executable.chmod(0o755)
    monkeypatch.setattr(settings, "aops_cli_version_requirement", "")
    values = await asyncio.gather(*(cli.inspect_cli(executable) for _ in range(4)))
    assert len(count.read_text().splitlines()) == 1
    assert len({value.sha256 for value in values}) == 1
    await cli.inspect_cli(executable)
    assert len(count.read_text().splitlines()) == 1
    executable.write_text(executable.read_text() + "# changed binary\n")
    changed = await cli.inspect_cli(executable)
    assert changed.sha256 != values[0].sha256
    assert len(count.read_text().splitlines()) == 2
    monkeypatch.setattr(settings, "aops_cli_version_requirement", "missing-version")
    with pytest.raises(cli.CliExecutionError) as failure:
        await cli.inspect_cli(executable)
    assert failure.value.code == "CLI_VERSION_MISMATCH"
    assert len(count.read_text().splitlines()) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [20 * 1024 * 1024, 1024 * 1024])
async def test_cli_stream_limit_accepts_boundary_and_rejects_excess_without_truncating(limit):
    class Stream:
        def __init__(self, length):
            self.remaining = length

        async def read(self, length):
            count = min(length, self.remaining)
            self.remaining -= count
            return b"x" * count

    assert len(await cli._read_limited(Stream(limit), limit)) == limit
    with pytest.raises(cli.CliExecutionError) as failure:
        await cli._read_limited(Stream(limit + 1), limit)
    assert failure.value.code == "OUTPUT_LIMIT_EXCEEDED"
    assert not failure.value.truncated


@pytest.mark.asyncio
async def test_cancelling_parse_keeps_run_owned_until_thread_finishes(tmp_path, monkeypatch):
    executable = tmp_path / "aops-cli"
    executable.write_text(f"#!{sys.executable}\nimport sys\nprint('test-cli' if '--version' in sys.argv else '{{\"status\":0,\"rowCount\":0,\"data\":[]}}')\n")
    executable.chmod(0o755)
    monkeypatch.setattr(settings, "aops_cli_path", executable)
    monkeypatch.setattr(settings, "aops_cli_version_requirement", "")
    started, finish = threading.Event(), threading.Event()
    original = cli.parse_sql_read_output

    def parse(value):
        started.set()
        assert finish.wait(5)
        return original(value)

    monkeypatch.setattr(cli, "parse_sql_read_output", parse)
    before = data_budget.used
    executor = PushExecutor()
    executor.dispatch(payload(), "only-test-key", sync_tec01=False)
    task = executor.active["resource-run"].task
    try:
        async with asyncio.timeout(3):
            while not started.is_set():
                await asyncio.sleep(.005)
        task.cancel()
        await asyncio.sleep(.02)
        assert not task.done() and executor.activity("resource-run")["active"]
        assert data_budget.used >= before + settings.executor_run_memory_mb * 1024 * 1024
    finally:
        finish.set()
        await asyncio.wait_for(task, 3)
    assert not executor.active and data_budget.used == before
