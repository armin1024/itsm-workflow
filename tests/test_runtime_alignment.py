import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.runtime.planner import validate_workflow
from app.runtime.push_executor import ActiveRun, PushExecutor, push_executor
from app.tec01_client import Tec01Client, Tec01Error


def acknowledgement(body, **changes):
    data = {"runId": "r", "status": "RUNNING", "lastEventId": 1}
    data.update(changes)
    return {"status": 200, "data": data}


def definition():
    return {"entryNodeId": "a", "nodes": [{"id": key, "type": "end", "title": key} for key in ("a", "b")], "edges": [{"id": "next", "source": "a", "target": "b"}]}


def payload(flow):
    valid = validate_workflow(flow, "EXECUTE")
    return {"runId": "r", "dispatchId": "d", "ticketId": 1, "workflowSnapshot": valid["normalizedDefinition"], "workflowContentHash": valid["workflowContentHash"]}


@pytest.mark.asyncio
async def test_timeout_retry_reuses_same_body_and_acknowledgement_identity(monkeypatch):
    monkeypatch.setattr(settings, "tec01_callback_max_attempts", 3)
    calls, delays = [], []

    async def sleep(delay):
        delays.append(delay)

    async def handler(request):
        calls.append((request.headers["X-Request-Id"], request.content))
        if len(calls) == 1:
            raise httpx.ReadTimeout("timeout", request=request)
        return httpx.Response(200, json=acknowledgement(json.loads(request.content)))

    client = Tec01Client(base_url="http://tec01", token="service-key", transport=httpx.MockTransport(handler), sleep=sleep)
    await client.node_completed("r", "a", {"dispatchId": "d", "attemptId": "try", "status": "SUCCEEDED"})
    assert len(calls) == 2 and calls[0] == calls[1] and delays == [1]


@pytest.mark.asyncio
@pytest.mark.parametrize("change,expected", [({"runId": "other"}, "TEC01_ACK_MISMATCH"), ({"runId": None}, "TEC01_ACK_MISMATCH")])
async def test_business_acknowledgements_must_confirm_this_result(change, expected):
    calls = []

    async def handler(request):
        calls.append(request)
        return httpx.Response(200, json=acknowledgement(json.loads(request.content), **change))

    client = Tec01Client(base_url="http://tec01", token="key", transport=httpx.MockTransport(handler))
    with pytest.raises(Tec01Error) as caught:
        await client.node_completed("r", "a", {"dispatchId": "d", "attemptId": "try", "status": "SUCCEEDED"})
    assert caught.value.code == expected and len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("response", [httpx.Response(202, json={"status": 200}), httpx.Response(204), httpx.Response(200, json={"status": 500, "data": {"runId": "r"}}), httpx.Response(200, json={"status": "200", "data": {"runId": "r"}}), httpx.Response(200, json={"status": True, "data": {"runId": "r"}}), httpx.Response(200, json={"status": 200, "data": {}}), httpx.Response(200, text="not-json")])
async def test_http_success_alone_is_not_an_adoption_ack(response):
    client = Tec01Client(base_url="http://tec01", token="key", transport=httpx.MockTransport(lambda _: response))
    with pytest.raises(Tec01Error):
        await client.node_completed("r", "a", {"dispatchId": "d", "attemptId": "try", "status": "SUCCEEDED"})


@pytest.mark.asyncio
async def test_total_ack_wait_has_a_hard_deadline(monkeypatch):
    monkeypatch.setattr(settings, "tec01_callback_wait_seconds", 0.02)

    async def handler(request):
        await asyncio.sleep(10)
        return httpx.Response(200, json=acknowledgement(json.loads(request.content)))

    client = Tec01Client(base_url="http://tec01", token="key", transport=httpx.MockTransport(handler))
    with pytest.raises(Tec01Error, match="总等待") as caught:
        await asyncio.wait_for(client.node_completed("r", "a", {"dispatchId": "d", "attemptId": "try", "status": "SUCCEEDED"}), 1)
    assert caught.value.code == "TEC01_ACK_WAIT_EXCEEDED"


@pytest.mark.asyncio
async def test_next_node_waits_for_saved_ack_and_status_exposes_no_payload():
    waiting, confirmed, seen = asyncio.Event(), asyncio.Event(), []

    class Client:
        async def node_started(self, run, node, body):
            seen.append(("start", node))

        async def node_completed(self, run, node, body):
            assert not {"snapshot", "workflowSnapshot", "nodeOutputs"}.intersection(body)
            seen.append(("complete", node, body["runStatus"]))
            if node == "a":
                waiting.set()
                await confirmed.wait()

        async def run_released(self, run, body):
            seen.append(("released", body["status"]))

    executor = PushExecutor(Client())
    executor.dispatch(payload(definition()), None)
    task = executor.active["r"].task
    await asyncio.wait_for(waiting.wait(), 1)
    state = executor.activity("r")
    assert state["active"] and state["phase"] == "WAITING_RESULT_ACK"
    assert ("start", "b") not in seen
    assert not {"result", "workflowSnapshot", "apiKey"}.intersection(state)
    confirmed.set()
    await asyncio.wait_for(task, 1)
    assert ("complete", "b", "SUCCEEDED") in seen
    assert executor.activity("r")["active"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("command,expected", [("PAUSE", "PAUSED"), ("CANCEL", "TERMINATED")])
async def test_control_during_ack_wait_does_not_start_another_node(command, expected):
    waiting, ack, starts, stopped = asyncio.Event(), asyncio.Event(), [], []

    class Client:
        async def node_started(self, run, node, body):
            starts.append(node)

        async def node_completed(self, *args):
            waiting.set()
            await ack.wait()

        async def run_released(self, run, body):
            stopped.append(body["status"])

    executor = PushExecutor(Client())
    executor.dispatch(payload(definition()), None)
    task = executor.active["r"].task
    await asyncio.wait_for(waiting.wait(), 1)
    assert executor.command("r", "control", command)["accepted"]
    assert executor.activity("r")["pauseRequested" if command == "PAUSE" else "cancelRequested"]
    ack.set()
    await asyncio.wait_for(task, 1)
    assert starts == ["a"] and stopped == [expected]
    assert not executor.activity("r")["active"]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["timeout", "server", "connect"])
async def test_callback_exhaustion_does_not_execute_next_sql_or_retry_stop(monkeypatch, failure):
    calls, delays, executed, events = [], [], [], []
    monkeypatch.setattr(settings, "tec01_callback_max_attempts", 3)

    async def sleep(delay):
        delays.append(delay)

    async def handler(request):
        body = json.loads(request.content)
        calls.append((request.headers["X-Request-Id"], body))
        if body["type"] == "NODE_COMPLETED":
            if failure == "server":
                return httpx.Response(503, json={"message": "暂时无法保存"})
            if failure == "connect":
                raise httpx.ConnectError("unavailable", request=request)
            raise httpx.ReadTimeout("lost", request=request)
        return httpx.Response(200, json=acknowledgement(body))

    async def execute(**kwargs):
        executed.append(kwargs["node"]["id"])
        return {"status": "SUCCEEDED", "output": {"status": 0, "data": [], "rowCount": 0}, "diagnostic": None, "interrupt": None}

    monkeypatch.setattr("app.runtime.push_executor.execute_single_node", execute)
    flow = definition()
    for node in flow["nodes"]:
        node.update(type="sql_read", config={"databaseRef": "fixture/db", "sqlTemplate": "SELECT 1"})
    client = Tec01Client(base_url="http://tec01", token="key", transport=httpx.MockTransport(handler), sleep=sleep)
    executor = PushExecutor(client)

    async def sink(event):
        events.append(event)

    executor.dispatch(payload(flow), "only-test-key", event_sink=sink)
    await executor.active["r"].task
    assert executed == ["a"] and delays == [1, 2]
    completed = [item for item in calls if item[1]["type"] == "NODE_COMPLETED"]
    assert len(completed) == 3 and all(item == completed[0] for item in completed)
    assert all(item[1]["type"] != "RUN_RELEASED" for item in calls)
    assert not executor.active and events[-1]["status"] == "UNKNOWN"
    assert "only-test-key" not in json.dumps(events)


@pytest.mark.asyncio
async def test_invalid_business_ack_stops_without_remote_failure_retry():
    calls, events = [], []

    async def handler(request):
        body = json.loads(request.content)
        calls.append(body)
        return httpx.Response(200, json=acknowledgement(body, runId="other" if body["type"] == "NODE_COMPLETED" else "r"))

    client = Tec01Client(base_url="http://tec01", token="key", transport=httpx.MockTransport(handler))
    executor = PushExecutor(client)

    async def sink(event):
        events.append(event)

    executor.dispatch(payload(definition()), None, event_sink=sink)
    await executor.active["r"].task
    assert [c["type"] for c in calls] == ["NODE_STARTED", "NODE_COMPLETED"]
    assert all(c.get("nodeId") != "b" for c in calls)
    assert not executor.active and events[-1]["status"] == "UNKNOWN"


def test_activity_api_requires_auth_and_inactive_is_not_success(monkeypatch):
    monkeypatch.setattr(settings, "runtime_service_token", "runtime-test")
    with TestClient(app) as client:
        assert client.get("/internal/v1/executions/missing").status_code == 401
        state = client.get("/internal/v1/executions/missing", headers={"Authorization": "Bearer runtime-test"}).json()
        assert state["active"] is False and state["phase"] == "INACTIVE"
        assert "status" not in state and "result" not in state
        push_executor.active["live-test"] = ActiveRun("d", current_node_id="q", phase="WAITING_RESULT_ACK")
        try:
            state = client.get("/internal/v1/executions/live-test", headers={"Authorization": "Bearer runtime-test"}).json()
            assert state["active"] and state["dispatchId"] == "d" and state["currentNodeId"] == "q"
        finally:
            push_executor.active.pop("live-test")


def test_studio_confirmation_hash_binds_ticket_and_parameters(monkeypatch):
    monkeypatch.setattr(settings, "studio_admin_token", "studio-test")
    with TestClient(app) as client:
        assert client.post("/api/v1/studio/session", json={"token": "studio-test"}).status_code == 200
        plan = client.post("/api/v1/studio/workflows/plan", json={"workflowDefinition": definition(), "ticketId": 1, "inputs": {}}).json()
        request = {"runId": "confirm-test", "dispatchId": "confirm-d", "ticketId": 2, "workflowSnapshot": plan["normalizedDefinition"], "workflowContentHash": plan["workflowContentHash"], "planMaterialHash": plan["planMaterialHash"]}
        rejected = client.post("/api/v1/studio/executions/dispatch", json=request)
        assert rejected.status_code == 409 and "计划" in rejected.text
        request["ticketId"] = 1
        assert client.post("/api/v1/studio/executions/dispatch", json=request).status_code == 200
