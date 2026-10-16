import asyncio
import copy
import json

import pytest

from app.extraction import compile_ticket_evidence, extract_operations, validate_ticket_evidence
from app.runtime.compiler_service import CompilerService
from app.runtime.planner import canonical_hash
from app.tec01_client import Tec01Error


def audit(sql="SELECT id FROM users WHERE name='王五'", **kwargs):
    return {"id": "op-1", "operation": "sql_exec_read", "result": '{"r":true,"e":""}', "created_at": 10, "details": json.dumps({"sql": {"serverid": 124, "dbid": "test", "dbname": "test", "dbuser": "readonly", "service_name": "test", "command": sql}}), **kwargs}


def job(knowledge_id="knw-1", request_id="req-1"):
    return {"knowledgeId": knowledge_id, "requestId": request_id, "ticketId": 100173, "ticketInfo": {"id": 100173, "incident_id": "INC-1", "event_title": "客户查询"}, "auditTimeline": [audit()]}


async def assessment(_ticket, _nodes):
    return {"name": "客户查询", "summary": "根据工单条件查询客户信息。", "matchPhrases": ["客户查询"], "negativePhrases": [], "steps": []}


class Client:
    def __init__(self):
        self.events = []

    async def extraction_callback(self, knowledge_id, event):
        self.events.append((knowledge_id, copy.deepcopy(event)))
        return {"knowledgeId": knowledge_id}


@pytest.mark.asyncio
async def test_compiler_unified_callbacks_have_ordered_stages_and_validation(monkeypatch):
    monkeypatch.setattr("app.extraction._assess", assessment)
    client = Client()
    service = CompilerService(client)
    data = job()
    data["auditTimeline"].append(audit(id="op-duplicate"))
    assert service.submit(data)
    await service.tasks["knw-1"]
    events = [event for _, event in client.events]
    assert all(key == "knw-1" for key, _ in client.events)
    assert [event["seq"] for event in events] == list(range(1, len(events) + 1))
    assert all(event["occurredAt"] for event in events)
    stages = list(dict.fromkeys(event["stage"] for event in events if event["type"] == "PROGRESS"))
    assert stages == ["FILTERING_OPERATIONS", "PARAMETERIZING_SQL", "ANALYZING_EVIDENCE", "BUILDING_WORKFLOW", "VALIDATING_WORKFLOW"]
    final = events[-1]
    assert final["type"] == "FINISHED" and final["status"] == "SUCCEEDED"
    assert final["validation"]["valid"]
    assert final["validation"]["workflowContentHash"] == canonical_hash(final["proposal"]["workflowDefinition"])
    assert final["diagnostics"]["ignoredOperationCount"] == 1
    assert final["diagnostics"]["ignoredOperations"] == [{"operationId": "op-duplicate", "reason": "duplicate_read_operation"}]
    assert service.get("knw-1")["status"] == "SUCCEEDED"


@pytest.mark.asyncio
async def test_analysis_heartbeat_shares_sequence_and_stops_before_next_stage(monkeypatch):
    async def slow_assessment(ticket, nodes):
        await asyncio.sleep(0.025)
        return await assessment(ticket, nodes)

    monkeypatch.setattr("app.extraction._assess", slow_assessment)
    client = Client()
    service = CompilerService(client, heartbeat_seconds=0.005)
    service.submit(job())
    await service.tasks["knw-1"]
    events = [event for _, event in client.events]
    analysis = [event for event in events if event.get("stage") == "ANALYZING_EVIDENCE"]
    assert len(analysis) >= 2
    assert all(event["progress"] == {"current": None, "total": None} for event in analysis)
    assert max(event["seq"] for event in analysis) < next(event["seq"] for event in events if event.get("stage") == "BUILDING_WORKFLOW")
    assert events[-1]["seq"] == len(events)


@pytest.mark.asyncio
async def test_local_compiler_has_no_tec01_calls_and_returns_proposal(monkeypatch):
    monkeypatch.setattr("app.extraction._assess", assessment)
    client = Client()
    events = []

    async def sink(event):
        events.append(event)

    service = CompilerService(client)
    service.submit(job(), event_sink=sink, sync_tec01=False)
    await service.tasks["knw-1"]
    assert client.events == []
    assert all(event["knowledgeId"] == "knw-1" for event in events)
    snapshot = service.get("knw-1")
    assert snapshot["proposal"]["name"] == "客户查询"
    snapshot["proposal"]["name"] = "changed"
    assert service.get("knw-1")["proposal"]["name"] == "客户查询"


@pytest.mark.asyncio
async def test_unconfirmed_final_callback_redelivers_without_recompiling(monkeypatch):
    calls = 0

    async def counted_assessment(ticket, nodes):
        nonlocal calls
        calls += 1
        return await assessment(ticket, nodes)

    class UnavailableClient(Client):
        unavailable = True

        async def extraction_callback(self, knowledge_id, event):
            self.events.append((knowledge_id, copy.deepcopy(event)))
            if self.unavailable:
                raise Tec01Error("TEC01_TIMEOUT", "secret-internal-error", retryable=True)
            return {}

    monkeypatch.setattr("app.extraction._assess", counted_assessment)
    client = UnavailableClient()
    service = CompilerService(client)
    service.submit(job())
    await service.tasks["knw-1"]
    record = service.get("knw-1")
    assert record["status"] == "SUCCEEDED" and record["syncStatus"] == "FAILED"
    assert "secret-internal-error" not in json.dumps(record)
    seq = record["seq"]
    original_final = client.events[-1][1]
    client.unavailable = False
    assert not service.submit(job())
    await service.tasks["knw-1"]
    assert calls == 1 and client.events[-1][1] == original_final
    assert service.get("knw-1")["seq"] == seq
    assert service.get("knw-1")["syncStatus"] == "SYNCED"


@pytest.mark.asyncio
async def test_submit_is_idempotent_and_failed_retry_keeps_knowledge_id(monkeypatch):
    async def failed_assessment(*_args):
        raise ValueError("LLM_ANALYSIS_FAILED：Authorization: Bearer secret-api-key")

    monkeypatch.setattr("app.extraction._assess", failed_assessment)
    client = Client()
    service = CompilerService(client)
    assert service.submit(job())
    assert not service.submit(job())
    different = job()
    different["ticketInfo"]["event_title"] = "different"
    with pytest.raises(ValueError, match="EXTRACTION_CONFLICT"):
        service.submit(different)
    await service.tasks["knw-1"]
    failure = service.get("knw-1")
    assert failure["error"]["code"] == "LLM_ANALYSIS_FAILED"
    assert "secret-api-key" not in json.dumps(failure)
    assert not service.submit(job())
    monkeypatch.setattr("app.extraction._assess", assessment)
    assert service.submit(job(request_id="retry-2"))
    await service.tasks["knw-1"]
    assert service.get("knw-1")["status"] == "SUCCEEDED"
    assert service.get("knw-1")["seq"] > failure["seq"]


@pytest.mark.parametrize("ticket,rows,message", [
    ({"status": 0, "data": {"id": 1}}, [], "data对象"),
    ({"id": 1}, {"status": 0, "data": []}, "data数组"),
    ({"id": 1}, [None], "JSON对象数组"),
    ({"id": True}, [], "正整数"),
])
def test_extraction_requires_data_object_and_array(ticket, rows, message):
    with pytest.raises(ValueError, match=message):
        validate_ticket_evidence(ticket, rows)


@pytest.mark.asyncio
async def test_empty_timeline_finishes_without_llm_and_invalid_sql_is_filtered(monkeypatch):
    async def unexpected(*_args):
        raise AssertionError("LLM should not run")

    monkeypatch.setattr("app.extraction._assess", unexpected)
    service = CompilerService(Client())
    data = job()
    data["auditTimeline"] = []
    service.submit(data)
    await service.tasks["knw-1"]
    assert service.get("knw-1")["error"]["code"] == "NO_VALID_OPERATIONS"
    valid, ignored = extract_operations([
        audit("SELECT 1; DELETE FROM users"),
        audit("SELECT 1", id="failed", result={"r": False, "status": 0}),
        audit("SELECT 1", id="bad-status", result={"status": {"unexpected": True}}),
    ])
    assert not valid
    assert [item["reason"].split(":")[0] for item in ignored] == ["invalid_read_sql", "failed_result", "failed_result"]


@pytest.mark.asyncio
async def test_compiler_bounds_completed_history_and_cancels_on_shutdown(monkeypatch):
    monkeypatch.setattr("app.extraction._assess", assessment)
    service = CompilerService(Client(), max_completed=1)
    for number in range(3):
        key = f"knw-{number}"
        service.submit(job(key), sync_tec01=False)
        await service.tasks[key]
        await asyncio.sleep(0)
    assert len(service.records) == 1 and service.get("knw-2")

    async def long_assessment(*_args):
        await asyncio.Event().wait()

    monkeypatch.setattr("app.extraction._assess", long_assessment)
    service.submit(job("active"), sync_tec01=False)
    await asyncio.sleep(0)
    await service.shutdown()
    assert service.get("active")["status"] == "INTERRUPTED"
