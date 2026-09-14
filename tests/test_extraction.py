import json

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.cli import CliMetadata, CliResult
from app.db import Base
from app.extraction import SEMANTIC_DESCRIPTIONS, _apply_assessment, _assess, extract_operations, extract_ticket_draft, parameterize_sql


def audit_row(*, operation="sql_exec_read", result='{"r":true,"e":""}', command="SELECT id FROM users WHERE name='王五'"):
    return {"id": 19, "operation": operation, "result": result, "created_at": 10, "details": json.dumps({"sql": {"serverid": 124, "dbid": "aops-t", "dbname": "aops-t", "dbuser": "readonly", "service_name": "aops-t", "command": command}})}


def test_extraction_filters_failed_operations_before_parsing_sql():
    failed = audit_row(result='{"r":false,"e":"permission denied"}')
    missing = audit_row(result=None)
    invalid = audit_row(result="not-json")
    mutation = audit_row(command="DELETE FROM users")
    valid, ignored = extract_operations([failed, missing, invalid, mutation])
    assert valid == []
    assert [item["reason"].split(":")[0] for item in ignored] == ["failed_result", "missing_result", "invalid_result", "invalid_read_sql"]


def test_parameter_values_are_removed_and_descriptions_are_generic():
    template, inputs = parameterize_sql("SELECT id FROM users WHERE name='王五' AND mobile='13800138000' LIMIT 1", 2)
    assert "王五" not in template and "13800138000" not in template
    assert "LIMIT 1" in template
    nodes = [{"id": "sql-2", "title": "步骤", "inputs": inputs}]
    _apply_assessment(nodes, {"steps": [{"stepId": "sql-2", "title": "查询客户", "parameters": [{"name": inputs[0]["name"], "semanticField": "customer_name"}]}]})
    assert nodes[0]["inputs"][0]["description"] == SEMANTIC_DESCRIPTIONS["customer_name"]
    assert "王五" not in nodes[0]["inputs"][0]["description"]


@pytest.mark.asyncio
async def test_ticket_evidence_becomes_multi_step_draft(monkeypatch, tmp_path):
    ticket = {"status": 0, "data": {"id": 100173, "incident_id": "INC-1", "event_title": "客户查询", "system_list": [{"id": "crm"}]}}
    timeline = {"status": 0, "data": [audit_row(command="SELECT id FROM users WHERE name='王五'"), {**audit_row(command="SELECT status FROM users WHERE id=7"), "id": 20, "created_at": 20}]}

    async def fake_cli(arguments, **_kwargs):
        payload = ticket if arguments[1] == "info" else timeline
        return CliResult(payload, b"{}", b"", 0, CliMetadata("test", "0" * 64))

    async def fake_assess(_ticket, _nodes):
        return {"name": "客户信息查询", "summary": "根据工单条件查询客户基础信息与状态。", "matchPhrases": ["客户信息查询"], "negativePhrases": [], "steps": [{"stepId": "sql-1", "title": "查询客户编号", "parameters": [{"name": "step_1_value_1", "semanticField": "customer_name"}]}, {"stepId": "sql-2", "title": "查询客户状态", "parameters": [{"name": "step_2_value_1", "semanticField": "customer_id"}], "dependencies": [{"sourceStepId": "sql-1", "bindings": [{"parameter": "step_2_value_1", "jsonPointer": "/data/0/id"}]}]}]}

    monkeypatch.setattr("app.extraction.execute_json_command", fake_cli)
    monkeypatch.setattr("app.extraction._assess", fake_assess)
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'extraction.db'}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with sessions() as session:
        record, diagnostics = await extract_ticket_draft(session, ticket_id=100173, uids=["S2"], creator_uid="S1", api_key="secret")
        assert record.source_ticket_no == "INC-1" and record.source_type == "TICKET_EXTRACTION"
        assert len(record.draft_definition["nodes"]) == 3
        assert record.draft_definition["nodes"][1]["inputs"][0]["source"]["nodeId"] == "sql-1"
        assert diagnostics["acceptedOperationCount"] == 2
    await engine.dispose()


@pytest.mark.asyncio
async def test_llm_gateway_retries_without_response_format_and_parses_fence(monkeypatch):
    monkeypatch.setattr("app.extraction.settings.llm_base_url", "http://llm/v1")
    monkeypatch.setattr("app.extraction.settings.llm_model", "internal-model")
    monkeypatch.setattr("app.extraction.settings.llm_response_format", "auto")

    class Response:
        def __init__(self, status, payload=None, text=""):
            self.status_code, self._payload, self.text = status, payload, text
            self.is_error = status >= 400

        def json(self):
            return self._payload

    class Client:
        calls = []

        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, _url, **kwargs):
            self.calls.append(kwargs["json"])
            if len(self.calls) == 1:
                return Response(400, text="response_format unsupported")
            content = '```json\n{"name":"客户查询","summary":"查询客户信息。","matchPhrases":["客户查询"],"negativePhrases":[],"steps":[]}\n```'
            return Response(200, {"choices": [{"message": {"content": content}}]})

    monkeypatch.setattr("app.extraction.httpx.AsyncClient", Client)
    result = await _assess({"event_title": "客户查询"}, [])
    assert result["name"] == "客户查询"
    assert "response_format" in Client.calls[0] and "response_format" not in Client.calls[1]
