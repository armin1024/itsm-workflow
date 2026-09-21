import asyncio
import json

import httpx
import pytest

from app.runtime import NODE_REGISTRY
from app.runtime.compiler_service import CompilerService
from app.runtime.executor import build_candidates, execute_single_node
from app.runtime.planner import render_plan, validate_workflow
from app.runtime.push_executor import PushExecutor
from app.studio.debug import reject_credentials, run_single_node
from app.studio.store import StudioStore
from app.tec01_client import Tec01Client, Tec01Error


def test_catalog_contains_deterministic_runtime_nodes():
    types = {item["type"] for item in NODE_REGISTRY.catalog()["nodes"]}
    assert types == {"sql_read", "condition", "hitl_select", "hitl_form", "end"}
    workflow = {"entryNodeId": "sql-1", "nodes": [{"id": "sql-1", "type": "sql_read", "title": "查询", "config": {"databaseRef": "1/db/db/read/svc", "sqlTemplate": "SELECT 1"}, "inputs": []}], "edges": []}
    validated = validate_workflow(workflow, "PUBLISH")
    assert render_plan(workflow_version_id="wfv", workflow_content_hash=validated["workflowContentHash"], workflow_snapshot=workflow, ticket_id=1, parameters={}, actor_uid="S1")["valid"]
    with pytest.raises(ValueError, match="禁止保存凭据"): reject_credentials({"AOPS_API_KEY": "secret"})


@pytest.mark.asyncio
async def test_hitl_select_maps_sql_rows_and_form_accepts_multiple_values():
    select_node = {"id": "choose", "type": "hitl_select", "title": "选择", "config": {"title": "选择客户", "selectionMode": "SINGLE", "idPath": "/customer_id", "labelTemplate": "{{customer_name}} / {{customer_id}}", "displayFields": [{"name": "customer_name", "label": "姓名", "path": "/customer_name"}, {"name": "customer_id", "label": "编号", "path": "/customer_id"}], "outputFields": [{"name": "customer_id", "path": "/customer_id"}]}, "inputs": []}
    rows = [{"customer_id": "C1", "customer_name": "王五", "secret": "hidden"}, {"customer_id": "C2", "customer_name": "王五", "secret": "hidden"}]
    candidates = build_candidates(select_node, rows)
    assert "secret" not in json.dumps(candidates) and candidates[0]["label"] == "王五 / C1"
    waiting = await execute_single_node(node=select_node, inputs={"rows": rows}, mode="TEST")
    selected = await execute_single_node(node=select_node, inputs={"rows": rows}, mode="TEST", resume_payload={"nodeId": "choose", "action": "SELECT", "candidateIds": [candidates[1]["candidateId"]]})
    assert waiting["status"] == "WAITING_INPUT" and selected["output"]["selected"][0]["values"]["customer_id"] == "C2"
    form = {"id": "form", "type": "hitl_form", "title": "输入", "config": {"title": "输入参数", "fields": [{"name": "customer_id", "label": "编号", "type": "string", "required": True}, {"name": "limit", "label": "条数", "type": "integer", "required": True, "minimum": 1}]}, "inputs": []}
    result = await execute_single_node(node=form, inputs={}, mode="TEST", resume_payload={"nodeId": "form", "action": "SUBMIT", "values": {"customer_id": "C1", "limit": 2}})
    assert result["output"]["values"] == {"customer_id": "C1", "limit": 2}


@pytest.mark.asyncio
async def test_studio_hitl_reply(tmp_path):
    store = StudioStore(tmp_path / "studio.db"); await store.initialize()
    node = {"id": "choose", "type": "hitl_select", "title": "选择", "config": {"title": "选择客户", "selectionMode": "SINGLE", "idPath": "/id", "labelTemplate": "{{name}}", "displayFields": [{"name": "name", "label": "姓名", "path": "/name"}], "outputFields": [{"name": "id", "path": "/id"}]}, "inputs": []}
    waiting = await run_single_node(node=node, inputs={"rows": [{"id": "1", "name": "A"}, {"id": "2", "name": "B"}]}, mode="TEST")
    record = await store.create_debug_run(creator_uid="admin", workspace_id=None, node=node, inputs={}, mode="TEST", handler_version="2.0.0", status="WAITING_INPUT", output=None, diagnostic=None, interrupt=waiting["interrupt"])
    cid = waiting["interrupt"]["candidates"][1]["candidateId"]
    resolved = await store.resolve_interrupt(record["debugRunId"], "admin", {"action": "SELECT", "candidateIds": [cid]})
    assert resolved["output"]["selected"][0]["values"]["id"] == "2"


@pytest.mark.asyncio
async def test_tec01_callback_client_structured_error():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={"code": "STATE_CONFLICT", "message": "changed"})
    client = Tec01Client(base_url="http://tec01", token="token", transport=httpx.MockTransport(handler))
    with pytest.raises(Tec01Error) as error: await client.run_released("run", {"dispatchId": "d", "status": "FAILED"})
    assert error.value.code == "STATE_CONFLICT"


@pytest.mark.asyncio
async def test_compiler_service_reports_progress_and_completion(monkeypatch):
    events = []
    class Client:
        async def compiler_progress(self, job_id, payload): events.append(("progress", job_id, payload)); return {}
        async def compiler_complete(self, job_id, payload): events.append(("complete", job_id, payload)); return {}
        async def compiler_fail(self, *args): raise AssertionError(args)
    async def compile_evidence(_ticket, _rows):
        return {"name": "测试", "summary": "测试", "matchPhrases": ["测试"], "negativePhrases": [], "systemKeys": [], "workflowDefinition": {"entryNodeId": "done", "nodes": [{"id": "done", "type": "end", "title": "完成"}], "edges": []}}, {}
    monkeypatch.setattr("app.runtime.compiler_service.compile_ticket_evidence", compile_evidence)
    service = CompilerService(Client())  # type: ignore[arg-type]
    service.submit({"jobId": "j1", "ticketInfo": {}, "auditTimeline": []})
    await service.tasks["j1"]
    assert [item[0] for item in events][-1] == "complete"


@pytest.mark.asyncio
async def test_push_executor_accepts_whole_workflow_and_reports_each_node():
    events = []
    class Client:
        async def node_started(self, run, node, payload): events.append(("started", node)); return {}
        async def node_completed(self, run, node, payload): events.append(("completed", node, payload["status"])); return {}
        async def run_released(self, run, payload): events.append(("released", payload["status"])); return {}
    executor = PushExecutor(Client())  # type: ignore[arg-type]
    workflow = {"entryNodeId": "done", "nodes": [{"id": "done", "type": "end", "title": "完成"}], "edges": []}
    validated = validate_workflow(workflow, "EXECUTE")
    accepted, created = executor.dispatch({"dispatchId": "d1", "runId": "r1", "ticketId": 1, "workflowContentHash": validated["workflowContentHash"], "workflowSnapshot": workflow, "runInputs": {}, "nodeStates": {"done": "READY"}, "nodeOutputs": {}, "selectedRoutes": {}, "resumePayload": None}, None)
    assert created and accepted["accepted"]
    await executor.active["r1"].task
    assert events == [("started", "done"), ("completed", "done", "SUCCEEDED"), ("released", "SUCCEEDED")]
