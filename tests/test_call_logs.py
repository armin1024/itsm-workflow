import asyncio
import io
import json
import os
import time
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

from app import call_logs as module
from app.call_logs import CallLogStore, LogCursorExpired, call_span, hide_credentials, log_context
from app.config import settings
from app.main import app
from app.tec01_client import Tec01Client, Tec01Error


def store(tmp_path, **kwargs):
    return CallLogStore(tmp_path / "logs", console=io.StringIO(), **kwargs)


def records(value):
    return [json.loads(line) for path in value._files() for line in path.read_text().splitlines()]


def test_secure_fields_and_terminal_are_identical(tmp_path, monkeypatch):
    value = store(tmp_path)
    monkeypatch.setattr(module, "call_logs", value)
    monkeypatch.setattr(settings, "tec01_service_token", "service-private-key")
    value.start()
    with hide_credentials("private-aops-key"), log_context(runId="run-1", requestId="req-1"):
        module.emit("CALL_COMPLETED", operation="sql_read", errorCode="private-aops-key\nservice-private-key", url="http://u:password@host/path?api_key=secret#secret", stdout="SELECT '王五'", result={"name": "王五"}, apiKey="private-aops-key")
    value.stop()
    data = records(value)
    text = value.console.getvalue()
    assert data == [json.loads(line) for line in text.splitlines()]
    assert data[0]["runId"] == "run-1" and data[0]["requestId"] == "req-1"
    assert data[0]["url"] == "http://host/path"
    for secret in ("password", "private-aops-key", "service-private-key", "王五", "SELECT", "api_key", "stdout", "result"):
        assert secret not in text


def test_rotation_age_capacity_and_non_log_preservation(tmp_path, monkeypatch):
    now = datetime(2026, 10, 22, tzinfo=UTC)
    monkeypatch.setattr(module, "utcnow", lambda: now)
    value = store(tmp_path, file_bytes=1000, max_bytes=2600)
    value.start()
    for i in range(40):
        value.emit("CALL_COMPLETED", requestId=str(i))
    value.stop()
    assert len(value._files()) >= 2
    assert all(p.stat().st_size <= 1000 for p in value._files())
    assert sum(p.stat().st_size for p in value._files()) <= 2600
    preserved = value.directory / "user-notes.jsonl"
    preserved.write_text("do not delete")
    target = tmp_path / "outside"
    target.write_text("protected")
    link = value.directory / "calls-20000101T000000000000Z-000000000000.jsonl"
    link.symlink_to(target)
    # Restart treats all previous segments as closed and removes expired ones.
    monkeypatch.setattr(module, "utcnow", lambda: now + timedelta(days=8))
    restarted = store(tmp_path)
    restarted.cleanup()
    assert not restarted._files()
    assert preserved.read_text() == "do not delete"
    assert target.read_text() == "protected" and link.is_symlink()


def test_rotation_on_utc_date_and_idle_cleanup(tmp_path, monkeypatch):
    clock = [datetime(2026, 10, 22, 23, 59, tzinfo=UTC)]
    monkeypatch.setattr(module, "utcnow", lambda: clock[0])
    value = store(tmp_path)
    value.directory.mkdir()
    value._write({"timestamp": clock[0].isoformat(), "logId": "1", "event": "CALL_COMPLETED", "level": "INFO"})
    first = value.active
    clock[0] += timedelta(minutes=2)
    value._write({"timestamp": clock[0].isoformat(), "logId": "2", "event": "CALL_COMPLETED", "level": "INFO"})
    assert value.active != first and len(value._files()) == 2


def test_pagination_filters_corruption_and_expired_cursor(tmp_path):
    value = store(tmp_path)
    value.start()
    for i in range(700):
        value.emit("CALL_COMPLETED", runId="r", requestId=str(i), operation="sql_read")
    value.stop()
    with value.active.open("ab") as stream:
        stream.write(b"invalid-json\n[]\n")
    found, cursor = [], None
    while True:
        page = value.query(limit=37, cursor=cursor, runId="r")
        found.extend(page["items"])
        cursor = page["nextCursor"]
        if not cursor:
            break
    assert [int(row["requestId"]) for row in found] == list(reversed(range(700)))
    assert len(value.query(level="ERROR")["items"]) == 0
    assert len(value.query(keyword="sql_read", requestId="4")["items"]) == 1
    first = value.query(limit=1)
    with pytest.raises(LogCursorExpired):
        value.query(cursor="../../etc/passwd")
    value.active.unlink()
    with pytest.raises(LogCursorExpired):
        value.query(cursor=first["nextCursor"])


def test_query_scans_at_most_eight_mib(tmp_path):
    value = store(tmp_path)
    value.directory.mkdir()
    path = value.directory / "calls-20261022T000000000000Z-000000000000.jsonl"
    row = json.dumps({"timestamp": "2026-10-22T00:00:00+00:00", "logId": "1", "level": "INFO", "event": "CALL_COMPLETED", "operation": "x" * 400}) + "\n"
    path.write_text(row * 20000)
    page = value.query(keyword="no-such-event")
    assert page["scannedBytes"] <= 8 * 1024 * 1024 and page["nextCursor"]
    assert value.query(cursor=page["nextCursor"], keyword="no-such-event")["nextCursor"] is None


def test_queue_full_disk_failure_symlink_and_level_are_nonfatal(tmp_path, monkeypatch):
    value = store(tmp_path, queue_size=1, level="WARNING")
    value.emit("CALL_COMPLETED")
    assert value.queue.empty()
    value.emit("CALL_FAILED", "ERROR")
    value.emit("CALL_FAILED", "ERROR")
    assert value.dropped == 1
    broken = tmp_path / "not-directory"
    broken.write_text("file")
    invalid = CallLogStore(broken, console=io.StringIO())
    invalid.start()
    invalid.emit("CALL_FAILED", "ERROR")
    invalid.stop()
    assert invalid.write_errors == 1 and invalid.cleanup_errors > 0
    assert invalid.console.getvalue() and invalid.status()["available"] is False
    linked = tmp_path / "linked"
    linked.symlink_to(tmp_path)
    symlinked = CallLogStore(linked)
    symlinked.cleanup()
    assert symlinked.cleanup_errors == 1


def test_cleanup_unlink_failure_is_visible_without_removing_other_files(tmp_path, monkeypatch):
    value = store(tmp_path)
    value.directory.mkdir()
    old = value.directory / "calls-20000101T000000000000Z-000000000000.jsonl"
    old.write_text("old")
    original = type(old).unlink

    def fail(path, *args, **kwargs):
        if path == old:
            raise PermissionError()
        return original(path, *args, **kwargs)

    monkeypatch.setattr(type(old), "unlink", fail)
    value.cleanup()
    assert value.cleanup_errors == 1 and old.exists()


def test_idle_periodic_cleanup_and_restart_preserve_recent_files(tmp_path, monkeypatch):
    clock = [datetime(2026, 10, 22, tzinfo=UTC)]
    monkeypatch.setattr(module, "utcnow", lambda: clock[0])
    value = store(tmp_path, cleanup_seconds=0.01)
    value.start()
    value.emit("CALL_COMPLETED")
    value.queue.join()
    assert value._files()
    clock[0] += timedelta(days=8)
    for _ in range(100):
        if not value._files():
            break
        time.sleep(0.01)
    assert not value._files()
    value.emit("CALL_COMPLETED")
    value.stop()
    assert len(records(value)) == 1
    restarted = store(tmp_path)
    restarted.start()
    restarted.stop()
    assert len(records(restarted)) == 1


@pytest.mark.asyncio
async def test_callback_attempts_and_ack_failures_have_safe_logs(tmp_path, monkeypatch):
    value = store(tmp_path)
    monkeypatch.setattr(module, "call_logs", value)
    value.start()
    calls = []

    async def sleep(delay):
        pass

    async def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(503, json={"message": "SELECT '王五'"})
        return httpx.Response(200, json={"status": 500, "data": {"runId": "r"}})

    client = Tec01Client(base_url="http://tec01", token="aops-secret", transport=httpx.MockTransport(handler), sleep=sleep)
    with pytest.raises(Tec01Error):
        await client.node_completed("r", "sql-1", {"dispatchId": "d", "attemptId": "a", "status": "FAILED", "result": {"name": "王五"}})
    value.stop()
    data = records(value)
    assert len(calls) == 2
    failures = [row for row in data if row["event"] == "CALL_FAILED"]
    assert [row["httpStatus"] for row in failures] == [503, 200]
    assert failures[-1]["errorCode"] == "TEC01_ACK_INVALID"
    assert all(row["businessStatus"] == "FAILED" for row in failures)
    assert all(row["runId"] == "r" and row["nodeId"] == "sql-1" for row in failures)
    assert len({row["callId"] for row in data if row.get("callId")}) == 1
    starts = [row for row in data if row["event"] == "CALL_STARTED"]
    assert len({row["spanId"] for row in starts}) == 2
    assert {row["spanId"] for row in starts} == {row["spanId"] for row in failures}
    retry = next(row for row in data if row["event"] == "RETRY_SCHEDULED")
    assert retry["spanId"] == starts[0]["spanId"]
    assert all(row["operationLabel"] == "回报节点执行结果" for row in starts)
    assert "王五" not in value.console.getvalue() and "aops-secret" not in value.console.getvalue()


def test_log_api_auth_filtering_and_no_query_recursion(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "studio_admin_token", "logs-test")
    monkeypatch.setattr(settings, "log_dir", tmp_path / "server-logs")
    monkeypatch.setattr(settings, "runtime_service_token", "runtime-secret")
    with TestClient(app) as client:
        assert client.get("/api/v1/studio/logs").status_code == 401
        assert client.post("/api/v1/studio/session", json={"token": "logs-test"}).status_code == 200
        module.call_logs.queue.join()
        before = len(module.call_logs.query(limit=200)["items"])
        for _ in range(3):
            assert client.get("/api/v1/studio/logs").status_code == 200
            assert client.get("/api/v1/studio/logs/status").status_code == 200
        module.call_logs.queue.join()
        assert len(module.call_logs.query(limit=200)["items"]) == before
        catalog = client.get("/internal/v1/runtime/catalog", headers={"Authorization": "Bearer runtime-secret", "X-Request-Id": "catalog-check"})
        assert catalog.status_code == 200
        module.call_logs.queue.join()
        entries = module.call_logs.query(requestId="catalog-check")["items"]
        assert {row["event"] for row in entries} == {"REQUEST_STARTED", "REQUEST_COMPLETED"}
        assert all(row.get("url") == "/internal/v1/runtime/catalog" for row in entries)
        assert len({row["spanId"] for row in entries}) == 1
        assert all(row["operationLabel"] == "获取节点目录" for row in entries)
        linked = client.get("/api/v1/studio/logs", params={"spanId": entries[0]["spanId"]}).json()["items"]
        assert len(linked) == 2
        assert client.get("/api/v1/studio/logs?limit=201").status_code == 422
        assert client.get("/api/v1/studio/logs?cursor=bad").status_code == 409
        assert client.get("/api/v1/studio/logs?fromTime=2026-10-22T00:00:00").status_code == 422
        status = client.get("/api/v1/studio/logs/status").json()
        assert status["retentionDays"] == 7 and status["maxTotalBytes"] == 512 * 1024 * 1024
        assert client.delete("/api/v1/studio/logs").status_code == 405
        control = client.post("/internal/v1/executions/not-active/commands", headers={"Authorization": "Bearer runtime-secret", "X-Request-Id": "pause-check"}, json={"commandId": "cmd-1", "type": "PAUSE"})
        assert control.status_code == 200
        module.call_logs.queue.join()
        control_logs = module.call_logs.query(requestId="pause-check")["items"]
        assert any(row.get("operationLabel") == "请求暂停Workflow运行" and row.get("runId") == "not-active" for row in control_logs)
        assert "runtime-secret" not in client.get("/api/v1/studio/logs").text


@pytest.mark.asyncio
async def test_llm_failure_and_cli_metadata_never_log_bodies(tmp_path, monkeypatch):
    from app import extraction
    from app.cli import CliExecutionError
    value = store(tmp_path)
    monkeypatch.setattr(module, "call_logs", value)
    monkeypatch.setattr(settings, "llm_base_url", "http://llm/v1")
    monkeypatch.setattr(settings, "llm_model", "internal-model")
    monkeypatch.setattr(settings, "llm_response_format", "plain")
    value.start()
    original = httpx.AsyncClient
    transport = httpx.MockTransport(lambda _: httpx.Response(200, json={"choices": [{"message": {"content": "not-json-sensitive-value"}}]}))
    monkeypatch.setattr(extraction.httpx, "AsyncClient", lambda **kwargs: original(**kwargs, transport=transport))
    with pytest.raises(ValueError):
        await extraction._assess({"event_title": "sensitive-ticket"}, [])

    @module.logged_cli("sql_read")
    async def failed_cli(**kwargs):
        raise CliExecutionError("NON_ZERO_EXIT", "王五 private-key", exit_code=1, stdout=b"secret-query-result")

    with pytest.raises(CliExecutionError):
        await failed_cli(ticket_id=100173, api_key="private-key", sql="SELECT '王五'")
    value.stop()
    data = records(value)
    assert any(row.get("errorCode") == "LLM_INVALID_RESPONSE" for row in data)
    assert any(row.get("exitCode") == 1 and "httpStatus" not in row for row in data)
    for forbidden in ("not-json-sensitive-value", "sensitive-ticket", "王五", "private-key", "secret-query-result", "SELECT"):
        assert forbidden not in value.console.getvalue()


@pytest.mark.asyncio
async def test_business_error_is_searchable_without_logging_error_body(tmp_path, monkeypatch):
    value = store(tmp_path)
    monkeypatch.setattr(module, "call_logs", value)
    value.start()
    client = Tec01Client(base_url="http://tec01", token="private", transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"status": 200, "data": {"knowledgeId": "k"}})))
    await client.extraction_callback("k", {"type": "FINISHED", "requestId": "req1", "status": "FAILED", "seq": 1, "error": {"code": "LLM_ANALYSIS_FAILED", "message": "private customer details"}})
    value.stop()
    found = value.query(keyword="LLM_ANALYSIS_FAILED")["items"]
    assert len(found) == 2
    final = next(row for row in found if row["event"] == "CALL_COMPLETED")
    assert final["httpStatus"] == 200 and final["acknowledged"]
    assert final["businessStatus"] == "FAILED" and final["operationLabel"] == "回报知识提取失败"
    assert "private customer details" not in value.console.getvalue()


def test_old_log_files_get_safe_descriptions_without_rewriting(tmp_path):
    value = store(tmp_path)
    value.directory.mkdir()
    path = value.directory / "calls-20261023T000000000000Z-000000000000.jsonl"
    old = {"timestamp": "2026-10-23T00:00:00+00:00", "logId": "old", "event": "CALL_COMPLETED", "level": "INFO", "operation": "sql_read", "peer": "AOPS_CLI", "exitCode": 0}
    text = json.dumps(old) + "\n"
    path.write_text(text)
    found = value.query(keyword="SQL只读查询")["items"]
    assert len(found) == 1 and found[0]["operationLabel"] == "执行AOPS SQL只读查询"
    assert path.read_text() == text
