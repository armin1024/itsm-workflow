import json

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.config import Settings, settings
from app.main import app
from app.tec01_client import Tec01Client


@pytest.mark.parametrize("name,good", [
    ("tec01_run_callback_path", "/custom/runs/{runId}/report"),
    ("tec01_extraction_callback_path", "/custom/extract/{knowledgeId}/report"),
])
def test_path_configuration_validation(name, good):
    assert getattr(Settings(_env_file=None, **{name: good}), name) == good
    for bad in ("https://other/callback", "//other/callback", "/../{runId}", "/%2e/{runId}", "/no-placeholder", "/{unknown}", "/{runId}?key=x", "/{runId!r}", "/{runId}/{runId}", "/{runId}\n", "/{runId"):
        with pytest.raises(ValidationError):
            Settings(_env_file=None, **{name: bad})


@pytest.mark.asyncio
async def test_all_callbacks_use_configured_templates_and_keep_base_prefix(monkeypatch):
    monkeypatch.setattr(settings, "tec01_run_callback_path", "/custom/runs/{runId}/report")
    monkeypatch.setattr(settings, "tec01_extraction_callback_path", "/custom/extract/{knowledgeId}/report")
    requests = []
    run_id = "run/with space"

    async def handler(request):
        body = json.loads(request.content)
        requests.append(str(request.url))
        data = {"knowledgeId": "knw/1"} if body["type"] in {"PROGRESS", "FINISHED"} else {"runId": run_id, "status": "RUNNING", "lastEventId": 1}
        return httpx.Response(200, json={"status": 200, "data": data})

    client = Tec01Client(base_url="http://tec01/gateway", token="secret", transport=httpx.MockTransport(handler))
    await client.node_started(run_id, "sql", {"dispatchId": "d", "attemptId": "a"})
    await client.node_completed(run_id, "sql", {"dispatchId": "d", "attemptId": "a"})
    await client.run_released(run_id, {"dispatchId": "d"})
    for kind in ("PROGRESS", "FINISHED"):
        await client.extraction_callback("knw/1", {"requestId": "req1", "type": kind, "seq": 1})
    assert requests[:3] == ["http://tec01/gateway/custom/runs/run%2Fwith%20space/report"] * 3
    assert requests[3:] == ["http://tec01/gateway/custom/extract/knw%2F1/report"] * 2


def test_studio_configuration_is_authenticated_read_only_and_secret_free(monkeypatch):
    monkeypatch.setattr(settings, "studio_admin_token", "config-test")
    monkeypatch.setattr(settings, "tec01_base_url", "https://user:hidden@tec01/prefix?token=url-secret#fragment")
    monkeypatch.setattr(settings, "tec01_service_token", "callback-secret")
    monkeypatch.setattr(settings, "runtime_service_token", "runtime-secret")
    monkeypatch.setattr(settings, "llm_api_key", "model-secret")
    with TestClient(app) as client:
        assert client.get("/api/v1/studio/config").status_code == 401
        assert client.post("/api/v1/studio/session", json={"token": "config-test"}).status_code == 200
        response = client.get("/api/v1/studio/config")
        data = response.json()
        assert data["readOnly"] and not data["serverDatabase"]
        assert data["storage"] == "browser-indexeddb"
        assert data["tec01"]["baseUrl"] == "https://tec01/prefix"
        assert data["tec01"]["serviceTokenConfigured"] is True
        assert data["tec01"]["callbackMaxAttempts"] == settings.tec01_callback_max_attempts
        for secret in ("hidden", "url-secret", "callback-secret", "runtime-secret", "model-secret", "config-test"):
            assert secret not in response.text
        assert client.patch("/api/v1/studio/config", json={}).status_code == 405


def test_no_database_setting_or_dependency():
    assert "studio_database_path" not in Settings.model_fields
