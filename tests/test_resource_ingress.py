import asyncio
import json

import httpx
import pytest

from app.config import settings
from app.main import app
from app.resources import ResourceError, cpu_run, data_budget
from app.runtime.compiler_service import compiler_service
from app.runtime.planner import validate_workflow
from app.runtime.push_executor import push_executor
from app.studio.events import studio_events


def test_container_budget_is_checked_before_json_dom_allocation(monkeypatch):
    from app import request_limits
    monkeypatch.setattr(request_limits, "CPU_SCRATCH_BYTES", 128 * 1024)
    called = []
    def forbidden(_raw):
        called.append(True)
        raise AssertionError("must reject before DOM allocation")
    monkeypatch.setattr(request_limits.json, "loads", forbidden)
    with pytest.raises(ResourceError) as caught:
        request_limits._decode([b"[" + b"{}," * 1200 + b"{}]"], True)
    assert caught.value.code == "REQUEST_MEMORY_LIMIT" and called == []


def test_preflight_ignores_brackets_and_escaped_quotes_in_strings(monkeypatch):
    from app import request_limits
    monkeypatch.setattr(request_limits, "CPU_SCRATCH_BYTES", 128 * 1024)
    expected = {"text": '{}[]"\\' * 2000}
    raw = json.dumps(expected).encode()
    assert request_limits._decode([raw], True)[1] == expected


def test_preflight_rejects_depth_and_giant_numeric_scalar():
    from app import request_limits
    for raw in (b"[" * 129 + b"0" + b"]" * 129, b"[" + b"1" * 5000 + b"]"):
        with pytest.raises(ResourceError) as caught:
            request_limits._decode([raw], True)
        assert caught.value.code == "JSON_COMPLEXITY_LIMIT"


@pytest.fixture
def api_config(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "environment", "test")
    monkeypatch.setattr(settings, "runtime_service_token", "resource-runtime")
    monkeypatch.setattr(settings, "studio_admin_token", "resource-studio")
    monkeypatch.setattr(settings, "log_dir", tmp_path.resolve() / "logs")


@pytest.mark.asyncio
async def test_streamed_body_limit_ignores_content_length(api_config, monkeypatch):
    monkeypatch.setattr(settings, "request_max_body_mb", 1)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            async def chunks():
                for _ in range(20):
                    yield b" " * 65536
            response = await client.post("/internal/v1/runtime/workflows/validate", content=chunks(), headers={"Authorization": "Bearer resource-runtime", "Content-Type": "application/json"})
            assert response.status_code == 413 and response.json()["code"] == "REQUEST_TOO_LARGE"
            assert not push_executor.active


@pytest.mark.asyncio
async def test_unauthenticated_heavy_request_rejected_without_decode(api_config):
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post("/internal/v1/runtime/workflows/validate", content=b"not-json", headers={"Content-Type": "application/json"})
            assert response.status_code == 401


@pytest.mark.asyncio
async def test_audit_count_rejected_before_model_or_async_task(api_config, monkeypatch):
    monkeypatch.setattr(settings, "tec01_base_url", "http://never-called")
    monkeypatch.setattr(settings, "tec01_service_token", "unused")
    called = []
    async def forbidden(*args, **kwargs):
        called.append(True)
        raise AssertionError("should reject before LLM")
    monkeypatch.setattr("app.extraction._assess", forbidden)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post("/internal/v1/extractions/too-many", headers={"Authorization": "Bearer resource-runtime"}, json={"requestId": "r", "ticketId": 1, "ticketInfo": {"id": 1}, "auditTimeline": [{}] * 5001})
            assert response.status_code == 422 and response.json()["code"] == "AUDIT_OPERATIONS_TOO_LARGE"
            assert not called and "too-many" not in compiler_service.tasks


@pytest.mark.asyncio
async def test_local_memory_refusal_precedes_execution(api_config, monkeypatch):
    monkeypatch.setattr(settings, "runtime_data_budget_mb", 1)
    validated = validate_workflow({"entryNodeId": "end", "nodes": [{"id": "end", "type": "end", "title": "完成"}], "edges": []}, "TEST")
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            await client.post("/api/v1/studio/session", json={"token": "resource-studio"})
            response = await client.post("/api/v1/studio/executions/dispatch", json={"dispatchId": "memory-refused-d", "runId": "memory-refused", "ticketId": 1, "workflowContentHash": validated["workflowContentHash"], "workflowSnapshot": validated["normalizedDefinition"]})
            assert response.status_code == 429 and response.headers["retry-after"] == "3"
            assert "memory-refused" not in push_executor.active
            assert "memory-refused-d" not in push_executor.accepted


@pytest.mark.asyncio
async def test_studio_cache_has_budget_expiration_and_gap(monkeypatch):
    monkeypatch.setattr(settings, "runtime_data_budget_mb", 2048)
    # Standalone store: use only synthetic metadata, never an external result.
    from app.studio.events import StudioEvents
    events = StudioEvents()
    try:
        events.reserve("gap-test", 1024 * 1024)
        events.open("gap-test")
        for _ in range(70):
            await events.publish("gap-test", {"type": "NODE_STARTED", "snapshot": {"nodeStates": {"a": "RUNNING"}}})
        found = events.get("gap-test", 0)
        assert found["historyGap"] and found["events"][-1]["snapshot"]
        await events.publish("gap-test", {"type": "RUN_RELEASED", "status": "SUCCEEDED", "snapshot": {"nodeStates": {"a": "SUCCEEDED"}}})
        assert data_budget.owners[events._owner("gap-test")] < 1024 * 1024
        events.streams["gap-test"]["updated"] -= 901
        events.cleanup()
        assert "gap-test" not in events.streams and events._owner("gap-test") not in data_budget.owners
    finally:
        events.clear()


@pytest.mark.asyncio
async def test_oversized_studio_snapshot_does_not_stick_or_replace_last_saved(monkeypatch):
    from app.studio.events import StudioEvents
    events = StudioEvents()
    try:
        events.reserve("oversized-cache", 8192)
        events.open("oversized-cache")
        await events.publish("oversized-cache", {"type": "NODE_STARTED", "snapshot": {"nodeStates": {"a": "RUNNING"}}})
        with pytest.raises(ResourceError, match="超出"):
            await events.publish("oversized-cache", {"type": "NODE_COMPLETED", "snapshot": {"nodeOutputs": {"a": {"data": "x" * 16000}}}})
        page = events.get("oversized-cache", 0)
        assert not page["active"] and page["events"][-1]["status"] == "UNKNOWN"
        assert page["events"][0]["snapshot"]["nodeStates"] == {"a": "RUNNING"}
        assert "x" * 16000 not in json.dumps(page)
    finally:
        events.clear()
