import asyncio
import json
import sys

import pytest

from app.config import settings
from app.runtime.planner import validate_workflow
from app.runtime.push_executor import PushExecutor
from app.tec01_client import Tec01Error


class NoCallbacks:
    async def node_started(self, *args):
        raise AssertionError("本地调试不应访问tec01")

    node_completed = node_started
    run_released = node_started


def dispatch_payload(workflow, **overrides):
    result = {"runId": "run-test", "dispatchId": "dispatch-1", "ticketId": 100173, "workflowSnapshot": workflow, "workflowContentHash": validate_workflow(workflow, "EXECUTE")["workflowContentHash"], "nodeStates": {}, "nodeOutputs": {}, "runInputs": {}, "selectedRoutes": {}}
    result.update(overrides)
    return result


def sql_node(node_id, sql="SELECT 1", inputs=None):
    return {"id": node_id, "type": "sql_read", "title": "查询", "config": {"databaseRef": "124/db/db/readonly/service", "sqlTemplate": sql}, "inputs": inputs or []}


@pytest.fixture
def fake_cli(tmp_path, monkeypatch):
    path, trace, release = tmp_path / "aops-cli", tmp_path / "calls.jsonl", tmp_path / "release"
    path.write_text(f'''#!{sys.executable}
import json, os, sys, time
if "--version" in sys.argv:
    print("test-aops-cli v1")
    raise SystemExit(0)
with open({str(trace)!r}, "a") as stream:
    stream.write(json.dumps(sys.argv[1:]) + "\\n")
assert os.environ.get("AOPS_API_KEY")
sql = sys.argv[sys.argv.index("--sqltext") + 1]
if sql == "SELECT 99":
    print("Authorization: Bearer " + os.environ["AOPS_API_KEY"], file=sys.stderr)
    print("database denied", file=sys.stderr)
    raise SystemExit(1)
if sql in ("SELECT 11", "SELECT 88"):
    while not os.path.exists({str(release)!r}):
        time.sleep(0.02)
print('event:title\\ndata:["value"]\\n\\nevent:message\\ndata:[22]\\n\\nevent:done\\ndata:!ok\\n')
''')
    path.chmod(0o755)
    monkeypatch.setattr(settings, "aops_cli_path", path)
    monkeypatch.setattr(settings, "aops_cli_version_requirement", "")
    return trace, release


async def await_trace(trace):
    async with asyncio.timeout(4):
        while not trace.exists():
            await asyncio.sleep(0.02)


@pytest.mark.asyncio
async def test_real_cli_pause_resume_uses_browser_snapshot_without_replaying_success(fake_cli):
    trace, release = fake_cli
    inputs = [{"name": "value", "type": "integer", "source": {"kind": "NODE_OUTPUT", "nodeId": "sql1", "jsonPointer": "/data/0/value"}}]
    workflow = {"entryNodeId": "sql1", "nodes": [sql_node("sql1", "SELECT 11"), sql_node("sql2", "SELECT {{value}} AS second", inputs)], "edges": [{"id": "next", "source": "sql1", "target": "sql2"}]}
    executor, events = PushExecutor(NoCallbacks()), []

    async def sink(event):
        events.append(event)

    payload = dispatch_payload(workflow)
    executor.dispatch(payload, "test-credential", event_sink=sink, sync_tec01=False)
    task = executor.active["run-test"].task
    await await_trace(trace)
    assert executor.command("run-test", "pause1", "PAUSE")["accepted"]
    release.touch()
    await asyncio.wait_for(task, 4)
    saved = events[-1]["snapshot"]
    assert saved["status"] == "PAUSED" and saved["nodeStates"]["sql1"] == "SUCCEEDED"
    assert len(trace.read_text().splitlines()) == 1 and "test-credential" not in json.dumps(events)
    assert executor.accepted["dispatch-1"]["status"] == "PAUSED"
    assert executor.dispatch(payload, "test-credential", event_sink=sink, sync_tec01=False)[1] is False
    restored = {**saved, "dispatchId": "dispatch-2", "resumePayload": {"action": "continue"}}
    executor.dispatch(restored, "test-credential", event_sink=sink, sync_tec01=False)
    await asyncio.wait_for(executor.active["run-test"].task, 4)
    assert events[-1]["snapshot"]["status"] == "SUCCEEDED"
    commands = [json.loads(line) for line in trace.read_text().splitlines()]
    assert len(commands) == 2 and commands[1][commands[1].index("--sqltext") + 1] == "SELECT 22 AS second"
    assert all(command[command.index("--comment") + 1] == "#uatu-100173" for command in commands)
    assert not executor.active


@pytest.mark.asyncio
async def test_cancel_terminates_actual_cli_and_releases_active_slot(fake_cli):
    trace, _ = fake_cli
    workflow = {"entryNodeId": "sql1", "nodes": [sql_node("sql1", "SELECT 88")], "edges": []}
    executor, events = PushExecutor(NoCallbacks()), []

    async def sink(event):
        events.append(event)

    executor.dispatch(dispatch_payload(workflow), "test-credential", event_sink=sink, sync_tec01=False)
    task = executor.active["run-test"].task
    await await_trace(trace)
    executor.command("run-test", "cancel1", "CANCEL")
    await asyncio.wait_for(task, 4)
    assert events[-1]["status"] == "TERMINATED" and not executor.active
    assert events[-1]["snapshot"]["nodeStates"]["sql1"] == "TERMINATED"


@pytest.mark.asyncio
async def test_cli_failure_exposes_reason_and_redacts_secret(fake_cli):
    workflow = {"entryNodeId": "sql1", "nodes": [sql_node("sql1", "SELECT 99")], "edges": []}
    events, executor = [], PushExecutor(NoCallbacks())

    async def sink(event):
        events.append(event)

    executor.dispatch(dispatch_payload(workflow), "test-credential", event_sink=sink, sync_tec01=False)
    await executor.active["run-test"].task
    failure = next(event for event in events if event["type"] == "NODE_COMPLETED")
    assert failure["diagnostic"]["errorCode"] == "NON_ZERO_EXIT" and "denied" in failure["safeSummary"]
    assert "test-credential" not in json.dumps(events)
    assert events[-1]["status"] == "FAILED"


@pytest.mark.asyncio
async def test_condition_skips_entire_unselected_branch_but_keeps_merge():
    nodes = [{"id": key, "type": "condition" if key == "branch" else "end", "title": key} for key in ["branch", "yes", "no", "no2", "merge"]]
    edges = [{"id": "yes-edge", "source": "branch", "target": "yes", "condition": {"path": "/inputs/hasRows", "op": "eq", "value": True}}, {"id": "no-edge", "source": "branch", "target": "no", "default": True}, {"id": "yes-merge", "source": "yes", "target": "merge"}, {"id": "no-next", "source": "no", "target": "no2"}, {"id": "no-merge", "source": "no2", "target": "merge"}]
    workflow = {"entryNodeId": "branch", "nodes": nodes, "edges": edges}
    events, executor = [], PushExecutor(NoCallbacks())

    async def sink(event):
        events.append(event)

    executor.dispatch(dispatch_payload(workflow, runInputs={"hasRows": True}), None, event_sink=sink, sync_tec01=False)
    await executor.active["run-test"].task
    assert [event["nodeId"] for event in events if event["type"] == "NODE_STARTED"] == ["branch", "yes", "merge"]
    snapshot = events[-1]["snapshot"]
    assert snapshot["nodeStates"] == {"branch": "SUCCEEDED", "yes": "SUCCEEDED", "no": "SKIPPED", "no2": "SKIPPED", "merge": "SUCCEEDED"}
    assert snapshot["selectedRoutes"]["branch"] == "yes-edge"


@pytest.mark.asyncio
async def test_hitl_form_wait_releases_and_restores_from_browser_state(fake_cli):
    fields = [{"name": "customer_id", "label": "客户编号", "type": "string", "required": True}, {"name": "limit", "label": "条数", "type": "integer", "required": True, "minimum": 1, "maximum": 100}]
    form = {"id": "form", "type": "hitl_form", "title": "输入条件", "config": {"title": "请填写", "fields": fields}}
    inputs = [{"name": "id", "type": "string", "source": {"kind": "NODE_OUTPUT", "nodeId": "form", "jsonPointer": "/values/customer_id"}}, {"name": "limit", "type": "integer", "source": {"kind": "NODE_OUTPUT", "nodeId": "form", "jsonPointer": "/values/limit"}}]
    workflow = {"entryNodeId": "form", "nodes": [form, sql_node("query", "SELECT {{id}} LIMIT {{limit}}", inputs)], "edges": [{"id": "next", "source": "form", "target": "query"}]}
    events, executor = [], PushExecutor(NoCallbacks())

    async def sink(event):
        events.append(event)

    executor.dispatch(dispatch_payload(workflow), "test-credential", event_sink=sink, sync_tec01=False)
    await executor.active["run-test"].task
    saved = events[-1]["snapshot"]
    assert saved["status"] == "WAITING_INPUT" and saved["interaction"]["kind"] == "HITL_FORM"
    assert saved["nodeStates"]["form"] == "WAITING" and not executor.active
    executor.dispatch({**saved, "dispatchId": "dispatch-2", "resumePayload": {"nodeId": "form", "action": "SUBMIT", "values": {"customer_id": "C001", "limit": 2}}}, "test-credential", event_sink=sink, sync_tec01=False)
    await executor.active["run-test"].task
    assert events[-1]["status"] == "SUCCEEDED"
    assert events[-1]["snapshot"]["nodeOutputs"]["form"]["values"] == {"customer_id": "C001", "limit": 2}


@pytest.mark.asyncio
async def test_removed_hitl_select_rejected_before_any_cli_execution(fake_cli):
    from app.runtime.executor import execute_single_node

    removed = {"id": "choose", "type": "hitl_select", "title": "旧的选择节点", "config": {}}
    with pytest.raises(ValueError, match="不支持的节点类型"):
        await execute_single_node(node=removed, inputs={}, mode="PRODUCTION", api_key="test-credential", ticket_id=100173)
    workflow = {"entryNodeId": "choose", "nodes": [removed], "edges": []}
    with pytest.raises(ValueError, match="不支持的节点类型"):
        PushExecutor(NoCallbacks()).dispatch({"runId": "run", "dispatchId": "dispatch", "ticketId": 100173, "workflowSnapshot": workflow, "workflowContentHash": "unused"}, "test-credential", sync_tec01=False)
    assert not fake_cli[0].exists()


@pytest.mark.asyncio
async def test_callback_failure_after_sql_does_not_replay_or_mark_node_failed(fake_cli):
    events = []

    class Client:
        async def node_started(self, *args):
            return {}

        async def node_completed(self, *args):
            raise Tec01Error("TEC01_TIMEOUT", "回调超时", retryable=True)

        async def run_released(self, run_id, body):
            events.append(body)
            return {}

    snapshots, executor = [], PushExecutor(Client())

    async def sink(event):
        snapshots.append(event)

    workflow = {"entryNodeId": "sql1", "nodes": [sql_node("sql1"), sql_node("sql2")], "edges": [{"id": "next", "source": "sql1", "target": "sql2"}]}
    executor.dispatch(dispatch_payload(workflow), "test-credential", event_sink=sink)
    await executor.active["run-test"].task
    assert events == []  # No second remote retry cycle after the result ACK failed.
    assert snapshots[-1]["status"] == "UNKNOWN"
    saved = snapshots[-1]["snapshot"]
    assert saved["nodeStates"]["sql1"] == "UNKNOWN" and saved["nodeStates"]["sql2"] == "READY"
    assert len(fake_cli[0].read_text().splitlines()) == 1
    with pytest.raises(ValueError, match="AMBIGUOUS"):
        executor.dispatch({**saved, "dispatchId": "dispatch-2"}, "test-credential", sync_tec01=False)


@pytest.mark.asyncio
async def test_unconfirmed_release_preserves_committed_sql_and_resume_never_replays(fake_cli):
    class Client:
        async def node_started(self, *args):
            return {}

        async def node_completed(self, *args):
            return {}

        async def run_released(self, *args):
            raise Tec01Error("TEC01_TIMEOUT", "释放回调超时", retryable=True)

    events, executor = [], PushExecutor(Client())

    async def sink(event):
        events.append(event)

    workflow = {"entryNodeId": "query", "nodes": [sql_node("query")], "edges": []}
    executor.dispatch(dispatch_payload(workflow), "test-credential", event_sink=sink)
    await executor.active["run-test"].task
    saved = events[-1]["snapshot"]
    assert saved["status"] == "UNKNOWN"
    assert saved["nodeStates"]["query"] == "SUCCEEDED"
    assert saved["nodeOutputs"]["query"]["rowCount"] == 1
    executor.dispatch({**saved, "dispatchId": "dispatch-recover"}, "test-credential", event_sink=sink, sync_tec01=False)
    await executor.active["run-test"].task
    assert events[-1]["status"] == "SUCCEEDED"
    assert len(fake_cli[0].read_text().splitlines()) == 1


@pytest.mark.asyncio
async def test_catalog_upgrade_without_handler_change_keeps_saved_plan_executable(monkeypatch):
    from app.runtime import NODE_REGISTRY

    workflow = {"entryNodeId": "done", "nodes": [{"id": "done", "type": "end", "title": "完成"}], "edges": []}
    validated = validate_workflow(workflow, "PUBLISH")
    original_catalog = NODE_REGISTRY.catalog()
    monkeypatch.setattr(NODE_REGISTRY, "catalog", lambda: {**original_catalog, "catalogDigest": "unrelated-catalog-update"})
    payload = dispatch_payload(validated["normalizedDefinition"], workflowContentHash=validated["workflowContentHash"])
    executor, events = PushExecutor(NoCallbacks()), []

    async def sink(event):
        events.append(event)

    executor.dispatch(payload, None, event_sink=sink, sync_tec01=False)
    await executor.active["run-test"].task
    assert events[-1]["status"] == "SUCCEEDED"


@pytest.mark.asyncio
async def test_invalid_hash_and_duplicate_payload_are_rejected_before_background_execution():
    workflow = {"entryNodeId": "done", "nodes": [{"id": "done", "type": "end", "title": "完成"}], "edges": []}
    executor = PushExecutor(NoCallbacks())
    payload = dispatch_payload(workflow)
    with pytest.raises(ValueError, match="HASH_MISMATCH"):
        executor.dispatch({**payload, "workflowContentHash": "tampered"}, None, sync_tec01=False)
    assert not executor.active and not executor.accepted
    executor.dispatch(payload, None, sync_tec01=False)
    with pytest.raises(RuntimeError, match="PAYLOAD_CONFLICT"):
        executor.dispatch({**payload, "ticketId": 2}, None, sync_tec01=False)
    await executor.active["run-test"].task
