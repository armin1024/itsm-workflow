import json

import httpx
import pytest

from app.tec01_client import Tec01Client, Tec01Error


@pytest.mark.asyncio
async def test_unified_callbacks_include_type_node_and_stable_distinct_request_ids():
    requests = []

    async def handler(request):
        body = json.loads(request.content)
        requests.append((request.url.path, request.headers["X-Request-Id"], body))
        data = {"knowledgeId": "knw1"} if body["type"] in {"PROGRESS", "FINISHED"} else {"runId": "run1", "status": "RUNNING", "lastEventId": 1}
        return httpx.Response(200, json={"status": 200, "msg": "success", "data": data})

    client = Tec01Client(base_url="http://tec01", token="service-token", transport=httpx.MockTransport(handler))
    await client.node_started("run1", "sql1", {"dispatchId": "d1", "attemptId": "a1", "startedAt": "now"})
    await client.node_completed("run1", "sql1", {"dispatchId": "d1", "attemptId": "a1", "status": "SUCCEEDED", "result": {"rowCount": 1}})
    await client.run_released("run1", {"dispatchId": "d1", "status": "SUCCEEDED", "currentNodeId": "sql1"})
    await client.extraction_callback("knw1", {"requestId": "req1", "type": "PROGRESS", "seq": 1, "stage": "ANALYZING_EVIDENCE"})
    await client.extraction_callback("knw1", {"requestId": "req1", "type": "FINISHED", "seq": 2, "status": "SUCCEEDED"})
    assert [item[0] for item in requests[:3]] == ["/internal/v1/runs/run1/callbacks"] * 3
    assert [item[1] for item in requests] == ["a1:started", "a1:completed", "d1:released", "knw1:req1:1", "knw1:req1:finished"]
    assert [item[2]["type"] for item in requests[:3]] == ["NODE_STARTED", "NODE_COMPLETED", "RUN_RELEASED"]
    assert all(item[2]["nodeId"] == "sql1" for item in requests[:2])
    assert all(item[0] == "/internal/v1/extractions/knw1/callbacks" for item in requests[3:])


@pytest.mark.asyncio
async def test_extraction_timeout_and_server_failure_keep_original_retry_policy():
    calls, delays = [], []

    async def sleep(delay):
        delays.append(delay)

    async def handler(request):
        calls.append((request.headers["X-Request-Id"], request.content))
        if len(calls) == 1:
            raise httpx.ReadTimeout("timeout", request=request)
        return httpx.Response(503, json={"code": "UNAVAILABLE", "message": "重试"})

    client = Tec01Client(base_url="http://tec01", token="service-token", transport=httpx.MockTransport(handler), sleep=sleep)
    with pytest.raises(Tec01Error) as caught:
        await client.extraction_callback("knw1", {"requestId": "req1", "type": "FINISHED", "status": "SUCCEEDED"})
    assert caught.value.retryable and len(calls) == 6
    assert delays == [1, 2, 4, 8, 16]
    assert len(set(calls)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [401, 404, 409, 422])
async def test_client_errors_are_not_retried_or_leak_credentials(status_code):
    calls = []

    async def handler(request):
        calls.append(request)
        return httpx.Response(status_code, json={"code": "REJECTED", "message": "Authorization: Bearer service-token password=secret"})

    client = Tec01Client(base_url="http://tec01", token="service-token", transport=httpx.MockTransport(handler))
    with pytest.raises(Tec01Error) as caught:
        await client.run_released("run1", {"dispatchId": "d1", "status": "FAILED"})
    assert len(calls) == 1 and not caught.value.retryable
    assert "service-token" not in str(caught.value) and "secret" not in str(caught.value)
