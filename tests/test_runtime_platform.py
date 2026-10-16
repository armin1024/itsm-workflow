import asyncio
import json

import httpx
import pytest

from app.runtime import NODE_REGISTRY
from app.runtime.compiler_service import CompilerService
from app.runtime.executor import execute_single_node
from app.runtime.planner import render_plan, validate_workflow
from app.runtime.push_executor import PushExecutor
from app.studio.debug import reject_credentials, run_single_node
from app.tec01_client import Tec01Client, Tec01Error


def test_catalog_contains_deterministic_runtime_nodes():
    types = {item["type"] for item in NODE_REGISTRY.catalog()["nodes"]}
    assert types == {"sql_read", "condition", "hitl_form", "end"}
    workflow = {"entryNodeId": "sql-1", "nodes": [{"id": "sql-1", "type": "sql_read", "title": "查询", "config": {"databaseRef": "1/db/db/read/svc", "sqlTemplate": "SELECT 1"}, "inputs": []}], "edges": []}
    validated = validate_workflow(workflow, "PUBLISH")
    assert render_plan(workflow_version_id="wfv", workflow_content_hash=validated["workflowContentHash"], workflow_snapshot=workflow, ticket_id=1, parameters={}, actor_uid="S1")["valid"]
    with pytest.raises(ValueError, match="禁止保存凭据"): reject_credentials({"AOPS_API_KEY": "secret"})


@pytest.mark.asyncio
async def test_hitl_form_accepts_multiple_values():
    form = {"id": "form", "type": "hitl_form", "title": "输入", "config": {"title": "输入参数", "fields": [{"name": "customer_id", "label": "编号", "type": "string", "required": True}, {"name": "limit", "label": "条数", "type": "integer", "required": True, "minimum": 1}]}, "inputs": []}
    result = await execute_single_node(node=form, inputs={}, mode="TEST", resume_payload={"nodeId": "form", "action": "SUBMIT", "values": {"customer_id": "C1", "limit": 2}})
    assert result["output"]["values"] == {"customer_id": "C1", "limit": 2}


@pytest.mark.asyncio
async def test_tec01_callback_client_structured_error():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={"code": "STATE_CONFLICT", "message": "changed"})
    client = Tec01Client(base_url="http://tec01", token="token", transport=httpx.MockTransport(handler))
    with pytest.raises(Tec01Error) as error: await client.run_released("run", {"dispatchId": "d", "status": "FAILED"})
    assert error.value.code == "STATE_CONFLICT"


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
