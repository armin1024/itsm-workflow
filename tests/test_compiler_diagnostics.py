import asyncio
import copy
import io
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app import call_logs
from app.compiler_errors import failure_details
from app.config import settings
from app.main import app
from app.runtime.compiler_service import CompilerService
from app.runtime.planner import validate_workflow
from app.tec01_client import Tec01Error
from tests.test_compiler_callbacks import Client, assessment, job


@pytest.mark.asyncio
@pytest.mark.parametrize("patch,field", [
    ({"steps": None}, "/steps"),
    ({"matchPhrases": None}, "/matchPhrases"),
    ({"negativePhrases": None}, "/negativePhrases"),
    ({"steps": [{"stepId": "sql-1", "parameters": None}]}, "/steps/0/parameters"),
    ({"steps": [{"stepId": "sql-1", "dependencies": None}]}, "/steps/0/dependencies"),
])
async def test_specific_failure_is_logged_without_tec01_and_can_be_searched(tmp_path, monkeypatch, patch, field):
    console = io.StringIO()
    store = call_logs.CallLogStore(tmp_path.resolve() / "logs", console=console)
    monkeypatch.setattr(call_logs, "call_logs", store)
    async def model(*args):
        return {**await assessment(None, None), **copy.deepcopy(patch)}
    monkeypatch.setattr("app.extraction._assess", model)
    store.start()
    service = CompilerService(Client())
    try:
        await service.submit_async(job(), sync_tec01=False)
        await service.tasks["knw-1"]
        error = service.get("knw-1")["error"]
        assert error["code"] == "LLM_ANALYSIS_FAILED"
        assert error["details"]["reasonCode"] == "LLM_RESPONSE_SCHEMA_INVALID"
        assert error["details"]["field"] == field and error["details"]["actualType"] == "null"
        assert error["details"]["stage"] == "ANALYZING_EVIDENCE"
        assert field in error["message"] and "数组" in error["message"]
        store.queue.join()
        rows = store.query(level="ERROR", peer="COMPILER", errorCode="LLM_ANALYSIS_FAILED", reasonCode="LLM_RESPONSE_SCHEMA_INVALID", errorField=field, errorId=error["errorId"], stage="ANALYZING_EVIDENCE")["items"]
        assert len(rows) == 1 and rows[0]["event"] == "COMPILER_FAILED"
        assert rows[0]["errorMessage"] == error["message"]
        assert store.query(keyword="数组")["items"]
        assert error["errorId"] in console.getvalue()
        assert "王五" not in console.getvalue() and "SELECT" not in console.getvalue()
    finally:
        await service.shutdown()
        store.stop()


def test_unknown_exception_never_logs_payload_and_has_safe_location():
    secret = "SELECT * FROM users WHERE name='王五' Authorization: Bearer abc-secret"
    try:
        raise TypeError(secret)
    except TypeError as exc:
        error = failure_details(exc, "BUILDING_WORKFLOW")
    text = json.dumps(error, ensure_ascii=False)
    assert error["code"] == "COMPILER_FAILED" and error["details"]["exceptionType"] == "TypeError"
    assert "类型处理异常" in error["message"] and error["errorId"].startswith("err_")
    assert all(value not in text for value in ("王五", "SELECT", "abc-secret", "/Users/"))


def test_workflow_validation_explains_rule_without_echoing_invalid_input():
    try:
        validate_workflow({"entryNodeId": "sql-1", "nodes": [{"id": "sql-1", "type": "sql_read", "title": "查询", "config": {"databaseRef": "fixture/db", "sqlTemplate": "SELECT {{customer_id}}"}, "inputs": []}], "edges": []}, "DRAFT")
    except Exception as exc:
        error = failure_details(exc, "VALIDATING_WORKFLOW")
    assert error["details"]["reasonCode"] == "SQL_INPUT_MISMATCH"
    assert error["details"]["field"] == "/workflowDefinition/nodes/0/inputs"
    assert "占位符" in error["message"] and "SELECT" not in error["message"]


@pytest.mark.asyncio
async def test_no_valid_operations_explains_filter_counts(monkeypatch):
    from app.extraction import compile_ticket_evidence
    from tests.test_compiler_callbacks import audit
    with pytest.raises(Exception) as caught:
        await compile_ticket_evidence({"id": 1}, [audit(result={"r": False}), audit(id="other", operation="sql_exec_write")])
    error = failure_details(caught.value, "FILTERING_OPERATIONS")
    assert error["code"] == "NO_VALID_OPERATIONS"
    assert error["details"]["filterCounts"] == {"failed_result": 1, "unsupported_operation": 1}
    assert "执行结果失败1条" in error["message"] and "非SQL读操作1条" in error["message"]


@pytest.mark.asyncio
async def test_nullable_ticket_system_list_is_supported_and_invalid_type_is_specific(monkeypatch):
    from app.extraction import compile_ticket_evidence
    from tests.test_compiler_callbacks import audit
    monkeypatch.setattr("app.extraction._assess", assessment)
    proposal, _ = await compile_ticket_evidence({"id": 1, "system_list": None}, [audit()])
    assert proposal["systemKeys"] == []
    with pytest.raises(Exception) as caught:
        await compile_ticket_evidence({"id": 1, "system_list": {"private": "王五"}}, [audit()])
    error = failure_details(caught.value)
    assert error["details"]["field"] == "/ticketInfo/system_list" and error["details"]["actualType"] == "object"
    assert "王五" not in str(error)


@pytest.mark.asyncio
async def test_business_failure_log_survives_callback_failure(tmp_path, monkeypatch):
    store = call_logs.CallLogStore(tmp_path.resolve() / "logs", console=io.StringIO())
    monkeypatch.setattr(call_logs, "call_logs", store)
    class FailedClient(Client):
        async def extraction_callback(self, *args):
            raise Tec01Error("TEC01_UNAVAILABLE", "no network", retryable=True)
    async def broken(*args):
        raise RuntimeError("private model response and 王五")
    monkeypatch.setattr("app.extraction._assess", broken)
    service = CompilerService(FailedClient())
    store.start()
    try:
        await service.submit_async(job())
        await service.tasks["knw-1"]
        error = service.get("knw-1")["error"]
        store.queue.join()
        assert len(store.query(errorId=error["errorId"], level="ERROR")["items"]) == 1
        assert service.get("knw-1")["syncStatus"] == "FAILED"
        assert "private model response" not in store.console.getvalue()
    finally:
        await service.shutdown()
        store.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,reason,phrase", [("http", "LLM_HTTP_ERROR", "503"), ("timeout", "LLM_TIMEOUT", "超时"), ("invalid", "LLM_RESPONSE_INVALID", "JSON")])
async def test_model_gateway_failure_reports_safe_specific_reason(monkeypatch, kind, reason, phrase):
    from app.extraction import _assess
    from contextlib import asynccontextmanager
    async def handle(request):
        if kind == "timeout": raise httpx.ReadTimeout("secret token and 王五", request=request)
        return httpx.Response(503, text="private body") if kind == "http" else httpx.Response(200, json={"choices": [None], "token": "secret"})
    @asynccontextmanager
    async def client():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as value:
            yield value
    monkeypatch.setattr("app.extraction.get_llm_client", client)
    monkeypatch.setattr(settings, "llm_base_url", "http://model.invalid/v1")
    monkeypatch.setattr(settings, "llm_model", "fixture")
    with pytest.raises(Exception) as caught:
        await _assess({"id": 1}, [])
    error = failure_details(caught.value, "ANALYZING_EVIDENCE")
    assert error["details"]["reasonCode"] == reason and phrase in error["message"]
    assert "private body" not in str(error) and "secret" not in str(error)


def test_authenticated_api_searches_failure_reason_fields(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "studio_admin_token", "diag-admin")
    monkeypatch.setattr(settings, "log_dir", tmp_path.resolve() / "logs")
    with TestClient(app) as http:
        assert http.get("/api/v1/studio/logs", params={"reasonCode": "X"}).status_code == 401
        http.post("/api/v1/studio/session", json={"token": "diag-admin"})
        call_logs.emit("COMPILER_FAILED", "ERROR", operation="compile_evidence", peer="COMPILER", direction="INTERNAL", errorCode="COMPILER_FAILED", reasonCode="SQL_INPUT_MISMATCH", errorField="/workflowDefinition/nodes/0/inputs", errorId="err_fixture", errorMessage="SQL占位符与参数不一致", knowledgeId="knw_fixture", stage="VALIDATING_WORKFLOW")
        call_logs.call_logs.queue.join()
        query = {"level": "ERROR", "peer": "COMPILER", "direction": "INTERNAL", "keyword": "参数不一致", "errorCode": "COMPILER_FAILED", "reasonCode": "SQL_INPUT_MISMATCH", "errorField": "/workflowDefinition/nodes/0/inputs", "errorId": "err_fixture", "stage": "VALIDATING_WORKFLOW"}
        response = http.get("/api/v1/studio/logs", params=query)
        assert response.status_code == 200 and len(response.json()["items"]) == 1
        assert http.get("/api/v1/studio/logs", params={**query, "errorId": "other"}).json()["items"] == []
