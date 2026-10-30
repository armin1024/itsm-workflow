import json

import httpx
import pytest

import app.http_clients as pools
from app.tec01_client import Tec01Client


@pytest.mark.asyncio
async def test_lifespan_pools_reuse_connections_and_isolate_model_from_callback():
    await pools.start_http_clients()
    try:
        async with pools.get_tec01_client() as first:
            async with pools.get_tec01_client() as second:
                assert first is second
            async with pools.get_llm_client() as model:
                assert model is not first
            async with pools.get_tec01_client(transport=httpx.MockTransport(lambda _: httpx.Response(200))) as injected:
                assert injected is not first
        assert not first.is_closed
    finally:
        await pools.close_http_clients()
    assert first.is_closed and model.is_closed


@pytest.mark.asyncio
async def test_callback_body_is_encoded_once_and_reused_on_transport_retries(monkeypatch):
    from app import tec01_client as module
    original = module.json.dumps
    encoded_calls, sent = [], []

    def counted(value, **kwargs):
        encoded_calls.append(value)
        return original(value, **kwargs)

    async def handler(request):
        sent.append(request.content)
        if len(sent) == 1:
            return httpx.Response(503)
        # Pre-encode response without invoking the patched json.dumps helper.
        return httpx.Response(200, content=b'{"status":200,"data":{"runId":"r"}}', headers={"Content-Type": "application/json"})

    async def no_sleep(_seconds):
        pass

    monkeypatch.setattr(module.json, "dumps", counted)
    client = Tec01Client(base_url="http://tec01", token="test", transport=httpx.MockTransport(handler), sleep=no_sleep)
    await client.run_released("r", {"dispatchId": "d", "status": "SUCCEEDED"})
    assert len(encoded_calls) == 1 and len(sent) == 2 and sent[0] == sent[1]
