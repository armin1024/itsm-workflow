import asyncio
import copy
import json

import httpx
import pytest

from app.config import Settings, settings
from app.runtime.compiler_service import CompilerService
from app.tec01_client import Tec01Client, Tec01Error


def ack(body):
    return {"status": 200, "data": {"runId": "run1", "status": "RUNNING", "lastEventId": 1}}


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["5xx", "connect", "timeout"])
async def test_execution_retries_six_times_with_unchanged_identity_and_redaction(monkeypatch, failure):
    monkeypatch.setattr(settings, "tec01_callback_max_attempts", 6)
    calls, delays = [], []

    async def sleep(delay):
        delays.append(delay)

    async def handler(request):
        calls.append((request.headers["X-Request-Id"], request.content))
        if failure == "connect":
            raise httpx.ConnectError("connection contains private-token", request=request)
        if failure == "timeout":
            raise httpx.ReadTimeout("private-token", request=request)
        return httpx.Response(503, json={"message": "Authorization: Bearer private-token password=secret"})

    client = Tec01Client(base_url="http://tec01", token="private-token", transport=httpx.MockTransport(handler), sleep=sleep)
    with pytest.raises(Tec01Error) as caught:
        await client.node_completed("run1", "sql1", {"dispatchId": "d1", "attemptId": "a1", "status": "SUCCEEDED"})
    assert len(calls) == 6 and len(set(calls)) == 1
    assert delays == [1, 2, 4, 8, 16]
    assert "private-token" not in str(caught.value) and "secret" not in str(caught.value)


@pytest.mark.asyncio
async def test_total_deadline_applies_during_backoff(monkeypatch):
    monkeypatch.setattr(settings, "tec01_callback_wait_seconds", 0.02)
    calls = []

    async def handler(request):
        calls.append(request)
        return httpx.Response(503)

    client = Tec01Client(base_url="http://tec01", token="test", transport=httpx.MockTransport(handler))
    with pytest.raises(Tec01Error) as caught:
        await asyncio.wait_for(client.run_released("run1", {"dispatchId": "d1"}), 1)
    assert caught.value.code == "TEC01_ACK_WAIT_EXCEEDED" and len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("response", [
    httpx.Response(204), httpx.Response(202, json={"status": 200, "data": {"knowledgeId": "k"}}),
    httpx.Response(200, text="not-json"), httpx.Response(200, json=[]),
    httpx.Response(200, json={"status": "200", "data": {"knowledgeId": "k"}}),
    httpx.Response(200, json={"status": 500, "data": {"knowledgeId": "k"}}),
    httpx.Response(200, json={"status": 200, "data": {"knowledgeId": "other"}}),
    httpx.Response(200, json={"status": 200, "data": {}}),
])
async def test_invalid_extraction_ack_is_not_retried(response):
    calls = []

    async def handler(request):
        calls.append(request)
        return response

    client = Tec01Client(base_url="http://tec01", token="test", transport=httpx.MockTransport(handler))
    with pytest.raises(Tec01Error):
        await client.extraction_callback("k", {"requestId": "req1", "type": "FINISHED", "seq": 9})
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_extraction_requires_original_request_id():
    client = Tec01Client(base_url="http://tec01", token="test")
    with pytest.raises(Tec01Error, match="requestId"):
        await client.extraction_callback("k", {"type": "FINISHED", "seq": 1})


@pytest.mark.asyncio
async def test_duplicate_execution_ack_still_requires_original_identity():
    calls = []

    async def handler(request):
        calls.append(request.content)
        body = json.loads(request.content)
        return httpx.Response(200, json=ack(body))

    client = Tec01Client(base_url="http://tec01", token="test", transport=httpx.MockTransport(handler))
    for _ in range(2):
        await client.node_completed("run1", "sql1", {"dispatchId": "d1", "attemptId": "a1"})
    assert calls[0] == calls[1]


@pytest.mark.asyncio
async def test_compiler_final_redelivery_and_retry_are_isolated_by_request_id(monkeypatch):
    model_calls, requests, stored, seen = [], [], {}, set()
    current_request = "req1"
    unavailable = True

    async def compile_evidence(*args, **kwargs):
        model_calls.append(1)
        if len(model_calls) == 1:
            raise ValueError("LLM_ANALYSIS_FAILED")
        flow = {"entryNodeId": "end", "nodes": [{"id": "end", "type": "end", "title": "完成"}], "edges": []}
        return {"name": "客户查询", "summary": "查询客户信息", "workflowDefinition": flow}, {}

    async def sleep(_delay):
        pass

    async def handler(request):
        body = json.loads(request.content)
        identity = request.headers["X-Request-Id"]
        requests.append((identity, copy.deepcopy(body)))
        if unavailable:
            return httpx.Response(503)
        # This mock is the tec01 contract oracle, not executor-side storage.
        if body["requestId"] == current_request and identity not in seen:
            seen.add(identity)
            stored[body["requestId"]] = copy.deepcopy(body)
        return httpx.Response(200, json={"status": 200, "data": {"knowledgeId": "k"}})

    monkeypatch.setattr("app.runtime.compiler_service.compile_ticket_evidence", compile_evidence)
    client = Tec01Client(base_url="http://tec01", token="test", transport=httpx.MockTransport(handler), sleep=sleep)
    service = CompilerService(client)
    job = {"knowledgeId": "k", "requestId": "req1", "ticketId": 1, "ticketInfo": {"id": 1}, "auditTimeline": []}
    assert service.submit(job)
    await service.tasks["k"]
    original = copy.deepcopy(requests[-1])
    assert service.get("k")["syncStatus"] == "FAILED"
    unavailable = False
    assert service.submit(job) is False
    await service.tasks["k"]
    assert requests[-1] == original and len(model_calls) == 1
    assert stored["req1"]["status"] == "FAILED"
    current_request = "req2"
    assert service.submit({**job, "requestId": current_request})
    await service.tasks["k"]
    assert len(model_calls) == 2 and stored["req2"]["status"] == "SUCCEEDED"
    before = copy.deepcopy(stored)
    await client.extraction_callback("k", original[1])
    await client.extraction_callback("k", {**original[1], "type": "PROGRESS", "seq": 999})
    assert stored == before
    assert "k:req1:finished" in {key for key, _ in requests}
    assert "k:req2:finished" in {key for key, _ in requests}


def test_default_retry_policy_and_explicit_override():
    assert Settings(_env_file=None).tec01_callback_max_attempts == 6
    assert Settings(_env_file=None, tec01_callback_max_attempts=3).tec01_callback_max_attempts == 3
