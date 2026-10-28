import copy
import io
import json

import pytest

from app.compiler_errors import CompilerFailure, failure_details
from app import call_logs
from app.config import settings
from app.extraction import SQL_FAILURE_MESSAGES, compile_ticket_evidence, extraction_outcome
from app.main import schema_catalog
from app.runtime.compiler_service import CompilerService
from app.schemas import ExtractionFinished
from tests.test_compiler_callbacks import Client, assessment, audit, job


@pytest.mark.asyncio
@pytest.mark.parametrize("sql,reason", [
    ("SELECT (", "SQL_PARSE_FAILED"),
    ("DELETE FROM users", "SQL_NOT_READ_ONLY"),
    ("SELECT 1; SELECT 2", "SQL_MULTIPLE_STATEMENTS"),
])
async def test_bad_sql_is_isolated_and_counts_are_exhaustive(monkeypatch, sql, reason):
    model_nodes = []
    async def model(ticket, nodes):
        model_nodes.extend(copy.deepcopy(nodes))
        return await assessment(ticket, nodes)
    monkeypatch.setattr("app.extraction._assess", model)
    rows = [audit(sql=sql, id="bad"), audit(), audit(id="dup"), audit(id="failed", result='{"r":false}')]
    proposal, report = await compile_ticket_evidence({"id": 1}, rows)
    assert len(model_nodes) == 1 and len(proposal["workflowDefinition"]["nodes"]) == 2
    assert report["failedOperations"][0]["code"] == reason
    assert report["failedOperations"][0]["auditIndex"] == 0
    assert report["auditOperationCount"] == 4
    assert report["acceptedOperationCount"] + report["ignoredOperationCount"] + report["failedOperationCount"] == 4
    assert extraction_outcome(report) == {"extractionOutcome": "PARTIAL_SUCCESS", "publishable": False}


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,reason", [("exception", "SQL_PARAMETERIZATION_FAILED"), ("invalid", "SQL_NODE_VALIDATION_FAILED"), ("length", "SQL_TEMPLATE_TOO_LARGE")])
async def test_parameterization_or_single_node_failure_keeps_remaining_nodes(monkeypatch, mode, reason):
    from app.extraction import parameterize_sql as original
    def parameterize(sql, step):
        if "broken" in sql:
            if mode == "exception":
                raise TypeError("Bearer actual-secret-value SELECT sensitive")
            if mode == "length":
                return "x" * (settings.sql_template_max_chars + 1), []
            return "SELECT {{missing}}", []
        return original(sql, step)
    monkeypatch.setattr("app.extraction.parameterize_sql", parameterize)
    monkeypatch.setattr("app.extraction._assess", assessment)
    rows = [audit(sql="SELECT id FROM broken", id=None), audit()]
    proposal, report = await compile_ticket_evidence({"id": 1}, rows)
    assert proposal["workflowDefinition"]["entryNodeId"] == "sql-1"
    assert report["failedOperations"][0] == {"operationId": None, "auditIndex": 0, "stage": "PARAMETERIZING_SQL", "code": reason,
        "message": SQL_FAILURE_MESSAGES[reason]}
    assert "actual-secret" not in json.dumps(report)


@pytest.mark.asyncio
async def test_long_sql_is_single_failure_not_global_rejection(monkeypatch):
    monkeypatch.setattr(settings, "sql_template_max_chars", 80)
    monkeypatch.setattr("app.extraction._assess", assessment)
    _, report = await compile_ticket_evidence({"id": 1}, [audit(sql="SELECT '" + "x" * 100 + "'"), audit(sql="SELECT id FROM users")])
    assert report["failedOperations"][0]["code"] == "SQL_TEMPLATE_TOO_LARGE"
    assert report["acceptedOperationCount"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("parameterization", [False, True])
async def test_all_invalid_fails_before_llm_with_diagnostics(monkeypatch, parameterization):
    async def never(*args):
        pytest.fail("model must not run with zero nodes")
    monkeypatch.setattr("app.extraction._assess", never)
    if parameterization:
        def broken(*args):
            raise ValueError("secret")
        monkeypatch.setattr("app.extraction.parameterize_sql", broken)
    with pytest.raises(CompilerFailure) as caught:
        await compile_ticket_evidence({"id": 1}, [audit(sql="SELECT id FROM users" if parameterization else "SELECT ("), audit(result=None)])
    result = failure_details(caught.value, "PARAMETERIZING_SQL")
    assert result["code"] == "NO_VALID_OPERATIONS"
    assert result["details"]["diagnostics"]["failedOperationCount"] == 1
    assert result["details"]["diagnostics"]["ignoredOperationCount"] == 1


@pytest.mark.asyncio
async def test_invalid_dependency_is_reported_without_inventing_binding(monkeypatch):
    async def model(ticket, nodes):
        value = await assessment(ticket, nodes)
        value["steps"] = [{"stepId": "sql-1", "dependencies": [{"sourceStepId": "missing", "bindings": [{"parameter": "step_1_value_1", "jsonPointer": "/data/0/id"}]}]}]
        return value
    monkeypatch.setattr("app.extraction._assess", model)
    proposal, report = await compile_ticket_evidence({"id": 1}, [audit()])
    assert proposal["workflowDefinition"]["nodes"][0]["inputs"][0]["source"]["kind"] == "RUN_INPUT"
    assert report["blockingIssues"][0]["nodeIds"] == ["sql-1"]
    assert extraction_outcome(report)["publishable"] is False


@pytest.mark.asyncio
async def test_model_failure_preserves_completed_sql_classification(monkeypatch):
    async def broken(*args):
        raise CompilerFailure("LLM_ANALYSIS_FAILED", "LLM_TIMEOUT", "模型超时")
    monkeypatch.setattr("app.extraction._assess", broken)
    with pytest.raises(CompilerFailure) as caught:
        await compile_ticket_evidence({"id": 1}, [audit(), audit(sql="SELECT (")])
    error = failure_details(caught.value, "ANALYZING_EVIDENCE")
    assert error["code"] == "LLM_ANALYSIS_FAILED"
    assert error["details"]["diagnostics"]["acceptedOperationCount"] == 1
    assert error["details"]["diagnostics"]["failedOperationCount"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("partial", [False, True])
async def test_callback_and_polling_preserve_outcome_and_schema(monkeypatch, partial):
    monkeypatch.setattr("app.extraction._assess", assessment)
    client, service = Client(), CompilerService()
    service.client = client
    data = job()
    if partial:
        data["auditTimeline"].append(audit(sql="SELECT (", id="bad"))
    try:
        service.submit(data)
        await service.tasks[data["knowledgeId"]]
        event = {"knowledgeId": data["knowledgeId"], **client.events[-1][1]}
        ExtractionFinished.model_validate(event)
        assert event["status"] == "SUCCEEDED"
        assert event["extractionOutcome"] == ("PARTIAL_SUCCESS" if partial else "SUCCESS")
        assert event["publishable"] is not partial
        stored = await service.get_async(data["knowledgeId"])
        assert stored["extractionOutcome"] == event["extractionOutcome"]
        assert stored["diagnostics"] == event["diagnostics"]
        schemas = schema_catalog()["schemas"]
        assert "extractionFinished" in schemas and "extractionDiagnostics" in schemas
        assert "extractionOutcome" in schemas["extractionFinished"]["required"]
    finally:
        await service.shutdown()
        for key in list(service.records):
            service._evict(key)


@pytest.mark.asyncio
async def test_partial_success_warning_is_searchable_without_tec01(tmp_path, monkeypatch):
    store = call_logs.CallLogStore(tmp_path / "logs", console=io.StringIO())
    monkeypatch.setattr(call_logs, "call_logs", store)
    monkeypatch.setattr("app.extraction._assess", assessment)
    service, data = CompilerService(Client()), job()
    data["auditTimeline"].append(audit(sql="SELECT (", id="op-bad"))
    store.start()
    try:
        service.submit(data, sync_tec01=False)
        await service.tasks[data["knowledgeId"]]
        store.queue.join()
        rows = store.query(level="WARNING", keyword="SQL_PARSE_FAILED", requestId=data["requestId"])["items"]
        assert len(rows) == 1 and rows[0]["event"] == "COMPILER_PARTIAL_SUCCESS"
        assert store.query(keyword="部分成功")["items"]
        assert "SELECT" not in store.console.getvalue() and "王五" not in store.console.getvalue()
    finally:
        await service.shutdown()
        for key in list(service.records):
            service._evict(key)
        store.stop()
