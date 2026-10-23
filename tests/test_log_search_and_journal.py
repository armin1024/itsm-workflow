import asyncio
import json
import re
import sys
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from app import call_logs as logs
from app import journal_reader as journal
from app.config import settings
from app.main import app


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "studio_admin_token", "search-test")
    monkeypatch.setattr(settings, "log_dir", tmp_path / "logs")
    with TestClient(app) as http:
        assert http.post("/api/v1/studio/session", json={"token": "search-test"}).status_code == 200
        yield http


def test_event_operation_search_and_raw_source(client):
    logs.emit("CALL_FAILED", "WARNING", peer="LLM", operation="analyze_evidence", errorCode="LLM_INVALID_RESPONSE", spanId="specific-call")
    logs.emit("CALL_COMPLETED", peer="AOPS_CLI", operation="sql_read", exitCode=0)
    logs.call_logs.queue.join()
    response = client.get("/api/v1/studio/logs", params={"event": "CALL_FAILED", "operation": "analyze_evidence", "peer": "LLM"})
    assert response.status_code == 200 and len(response.json()["items"]) == 1
    raw = client.get("/api/v1/studio/logs/raw", params={"spanId": "specific-call"}).json()
    assert len(raw["rawLines"]) == 1
    line = raw["rawLines"][0]
    assert "LLM_INVALID_RESPONSE" in line["text"]
    assert line["text"] in (logs.call_logs.directory / line["fileName"]).read_text()
    assert client.get("/api/v1/studio/logs", params={"event": "CALL_FAILED", "operation": "sql_read"}).json()["items"] == []


@pytest.mark.parametrize("query,field", [({"fromTime": "not-time"}, "fromTime"), ({"fromTime": "2026-10-23T09:00:00"}, "fromTime"), ({"level": "全部"}, "level"), ({"keyword": "x" * 201}, "keyword"), ({"fromTime": "2026-10-24T00:00:00Z", "toTime": "2026-10-23T00:00:00Z"}, "toTime")])
def test_bad_filter_has_specific_chinese_message(client, query, field):
    response = client.get("/api/v1/studio/logs", params=query)
    assert response.status_code == 422
    values = response.json()["detail"]["details"]["fields"]
    assert any(row["field"] == f"query.{field}" and re.search(r"[\u4e00-\u9fff]", row["message"]) for row in values)
    assert "x" * 201 not in response.text


def test_empty_dates_are_no_filter_and_offset_dates_work(client):
    for path in ("/api/v1/studio/logs", "/api/v1/studio/logs/raw", "/api/v1/studio/logs/journal"):
        assert client.get(path, params={"fromTime": "", "toTime": ""}).status_code == 200
    assert client.get("/api/v1/studio/logs", params={"fromTime": "2026-10-23T08:00:00+08:00", "toTime": "2026-10-23T09:00:00+08:00"}).status_code == 200


def test_raw_and_journal_require_admin_cookie(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "log_dir", tmp_path / "protected")
    with TestClient(app) as http:
        assert http.get("/api/v1/studio/logs/raw").status_code == 401
        assert http.get("/api/v1/studio/logs/journal").status_code == 401


def test_openapi_is_chinese_without_renaming_wire_fields(client):
    document = client.get("/openapi.json").json()
    assert document["info"]["title"] == "ITSM工作流执行器接口"
    for path, methods in document["paths"].items():
        for method, value in methods.items():
            if method not in {"get", "post", "delete"}:
                continue
            assert re.search(r"[\u4e00-\u9fff]", value["summary"]), path
            assert value.get("tags") and value["tags"] != ["default"]
    parameters = document["paths"]["/api/v1/studio/logs"]["get"]["parameters"]
    assert next(value for value in parameters if value["name"] == "fromTime")["schema"]["title"] == "开始时间"
    props = document["components"]["schemas"]["ExtractionSubmitRequest"]["properties"]
    assert props["ticketInfo"]["title"] == "工单详情"
    journal_params = document["paths"]["/api/v1/studio/logs/journal"]["get"]["parameters"]
    assert not {"runId", "unit", "filename", "event"}.intersection(row["name"] for row in journal_params)


@pytest.mark.asyncio
async def test_journal_unavailable_is_actionable(monkeypatch):
    monkeypatch.setattr(journal, "is_linux", lambda: False)
    assert (await journal.read_journal())["reason"] == "NOT_LINUX"
    monkeypatch.setattr(journal, "is_linux", lambda: True)
    monkeypatch.setattr(journal.shutil, "which", lambda _: None)
    assert (await journal.read_journal())["reason"] == "JOURNAL_NOT_FOUND"


def executable(tmp_path, content):
    path = tmp_path / "journal-fixture"
    path.write_text(f"#!{sys.executable}\n" + content)
    path.chmod(0o700)
    return str(path)


@pytest.mark.asyncio
async def test_journal_is_fixed_unit_minimal_environment_and_redacted(tmp_path, monkeypatch):
    source = '''import json,os,sys
assert "--unit=itsm-workflow-api.service" in sys.argv
assert "--no-pager" in sys.argv and "--reverse" in sys.argv
assert not os.environ.get("AOPS_API_KEY")
print(json.dumps({"__REALTIME_TIMESTAMP":"1792713600000000","MESSAGE":"Starting ITSM Workflow API","PRIORITY":"6"}))
print(json.dumps({"__REALTIME_TIMESTAMP":"1792713601000000","MESSAGE":"Authorization: Bearer private-test-token","PRIORITY":"3"}))
'''
    path = executable(tmp_path, source)
    monkeypatch.setattr(journal, "is_linux", lambda: True)
    monkeypatch.setattr(journal.shutil, "which", lambda _: path)
    monkeypatch.setenv("AOPS_API_KEY", "must-not-inherit")
    result = await journal.read_journal()
    assert result["available"] and len(result["items"]) == 2
    assert "private-test-token" not in json.dumps(result)
    assert all(row["unit"] == "itsm-workflow-api.service" for row in result["items"])


@pytest.mark.asyncio
async def test_journal_permission_and_output_limit_do_not_raise_500(tmp_path, monkeypatch):
    monkeypatch.setattr(journal, "is_linux", lambda: True)
    path = executable(tmp_path, 'import sys\nprint("Permission denied",file=sys.stderr)\nsys.exit(1)\n')
    monkeypatch.setattr(journal.shutil, "which", lambda _: path)
    assert (await journal.read_journal())["reason"] == "JOURNAL_PERMISSION_DENIED"
    path = executable(tmp_path, 'import sys\nsys.stdout.write("x"*3000000)\nsys.stdout.flush()\n')
    monkeypatch.setattr(journal.shutil, "which", lambda _: path)
    result = await asyncio.wait_for(journal.read_journal(), 3)
    assert result["reason"] == "JOURNAL_OUTPUT_LIMIT"


def test_raw_redaction_covers_nested_bodies_cookies_and_url_password(monkeypatch):
    monkeypatch.setattr(settings, "llm_api_key", "model-private-key")
    text = journal.redact_raw(json.dumps({"error": "safe", "payload": {"name": "王五"}, "nested": {"db_password": "db-private", "API_KEY": "model-private-key"}}))
    for value in ("王五", "db-private", "model-private-key"):
        assert value not in text
    assert "http-user:pass-private" not in journal.redact_raw("GET https://http-user:pass-private@host/path")
    assert "session-private" not in journal.redact_raw("Cookie: session-private")
    assert "private-value" not in journal.redact_raw("SELECT " + "private-value," * 10000, maximum=100)
