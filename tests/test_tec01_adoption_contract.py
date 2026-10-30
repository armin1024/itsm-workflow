"""Contract tests use a tec01 HTTP oracle; no production CLI or service calls."""
import copy
import json

import httpx
import pytest

from app.runtime.planner import validate_workflow
from app.runtime.push_executor import PushExecutor
from app.tec01_client import Tec01Client


def dispatch():
    definition = validate_workflow({
        "entryNodeId": "sql-1",
        "nodes": [
            {"id": "sql-1", "type": "sql_read", "title": "查询", "config": {"databaseRef": "fixture/db", "sqlTemplate": "SELECT 1"}},
            {"id": "done", "type": "end", "title": "完成"},
        ],
        "edges": [{"id": "next", "source": "sql-1", "target": "done"}],
    }, "EXECUTE")
    return {"runId": "run1", "dispatchId": "dispatch1", "ticketId": 1, "workflowSnapshot": definition["normalizedDefinition"], "workflowContentHash": definition["workflowContentHash"]}


def adopted(status="RUNNING"):
    return httpx.Response(200, json={"status": 200, "msg": "success", "data": {"runId": "run1", "status": status, "lastEventId": 1}})


@pytest.mark.asyncio
async def test_final_completion_response_loss_is_idempotent_even_after_terminal(monkeypatch):
    requests, persisted, sql_calls, delays = [], {}, [], []
    terminal = False

    async def handler(request):
        nonlocal terminal
        body = json.loads(request.content)
        key = request.headers["X-Request-Id"]
        requests.append((key, copy.deepcopy(body)))
        # Current dispatch, already committed identical message: acknowledge it
        # BEFORE rejecting new node callbacks due to the terminal run state.
        if key in persisted:
            assert persisted[key] == body
            return adopted("SUCCEEDED" if terminal else "RUNNING")
        if terminal and body["type"] != "RUN_RELEASED":
            return httpx.Response(409, json={"code": "CALLBACK_RUN_TERMINAL", "retryable": False})
        persisted[key] = copy.deepcopy(body)
        if body["type"] == "NODE_COMPLETED" and body["nodeId"] == "done":
            terminal = True
            raise httpx.ReadTimeout("response lost after commit", request=request)
        return adopted("SUCCEEDED" if terminal else "RUNNING")

    async def sleep(delay):
        delays.append(delay)

    async def execute(**kwargs):
        if kwargs["node"]["type"] == "sql_read":
            sql_calls.append(kwargs["node"]["id"])
        return {"status": "SUCCEEDED", "output": {"status": 0, "rowCount": 0, "data": []}, "diagnostic": None, "interrupt": None}

    monkeypatch.setattr("app.runtime.push_executor.execute_single_node", execute)
    client = Tec01Client(base_url="http://tec01", token="test", transport=httpx.MockTransport(handler), sleep=sleep)
    executor = PushExecutor(client)
    executor.dispatch(dispatch(), "ephemeral-test-key")
    await executor.active["run1"].task
    assert sql_calls == ["sql-1"] and delays == [1]
    assert requests[-1][1]["type"] == "RUN_RELEASED"
    assert requests[-1][1]["status"] == "SUCCEEDED"
    assert len(persisted) == 5 and len(requests) == 6
    assert not executor.active


@pytest.mark.asyncio
@pytest.mark.parametrize("rejected_type", ["NODE_STARTED", "NODE_COMPLETED"])
@pytest.mark.parametrize("error_code", ["CALLBACK_DISPATCH_STALE", "CALLBACK_RUN_TERMINAL"])
async def test_409_stops_without_retry_release_or_next_node(monkeypatch, rejected_type, error_code):
    requests, sql_calls = [], []

    async def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        if body["type"] == rejected_type:
            return httpx.Response(409, json={"code": error_code, "retryable": False})
        return adopted()

    async def execute(**kwargs):
        sql_calls.append(kwargs["node"]["id"])
        return {"status": "SUCCEEDED", "output": {"status": 0, "rowCount": 0, "data": []}, "diagnostic": None, "interrupt": None}

    async def unexpected_retry(_delay):
        raise AssertionError("409 must not retry")

    monkeypatch.setattr("app.runtime.push_executor.execute_single_node", execute)
    client = Tec01Client(base_url="http://tec01", token="test", transport=httpx.MockTransport(handler), sleep=unexpected_retry)
    executor = PushExecutor(client)
    executor.dispatch(dispatch(), "ephemeral-test-key")
    await executor.active["run1"].task
    assert sql_calls == ([] if rejected_type == "NODE_STARTED" else ["sql-1"])
    assert len(requests) == (1 if rejected_type == "NODE_STARTED" else 2)
    assert not any(body["type"] == "RUN_RELEASED" for body in requests)
    assert not executor.active


@pytest.mark.asyncio
async def test_cancel_callbacks_use_terminated_without_executing_queued_sql(monkeypatch):
    requests, events = [], []

    async def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        if body["type"] == "NODE_STARTED":
            assert executor.command("run1", "cancel1", "CANCEL")["accepted"]
        assert body.get("status") != "CANCELLED"
        assert body.get("runStatus") != "CANCELLED"
        return adopted("TERMINATED" if body.get("status") == "TERMINATED" else "RUNNING")

    async def unexpected_sql(**_kwargs):
        raise AssertionError("cancelled queued SQL must not execute")

    async def sink(event):
        events.append(event)

    monkeypatch.setattr("app.runtime.push_executor.execute_single_node", unexpected_sql)
    client = Tec01Client(base_url="http://tec01", token="test", transport=httpx.MockTransport(handler))
    executor = PushExecutor(client)
    executor.dispatch(dispatch(), "ephemeral-test-key", event_sink=sink)
    await executor.active["run1"].task
    assert [body["type"] for body in requests] == ["NODE_STARTED", "NODE_COMPLETED", "RUN_RELEASED"]
    assert requests[1]["status"] == requests[1]["runStatus"] == requests[2]["status"] == "TERMINATED"
    assert events[-1]["snapshot"]["nodeStates"]["sql-1"] == "TERMINATED"
    assert not executor.active
