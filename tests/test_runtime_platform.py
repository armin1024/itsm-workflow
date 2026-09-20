import json

import httpx
import pytest

from app.runtime import NODE_REGISTRY
from app.runtime.planner import render_plan, validate_workflow
from app.runtime.remote_checkpointer import RemoteTec01Checkpointer
from app.studio.debug import reject_credentials, run_single_node
from app.studio.store import StudioStore
from app.tec01_client import Tec01Client, Tec01Error
from app.compiler_worker import CompilerWorker
from app.workflow import WorkflowDefinition


def test_catalog_and_plan_are_registry_driven():
    catalog = NODE_REGISTRY.catalog()
    types = {item["type"] for item in catalog["nodes"]}
    assert {"sql_read", "condition", "llm_extract", "hitl_select", "human_input", "approval", "end"} <= types
    workflow = {
        "entryNodeId": "sql-1",
        "nodes": [{"id": "sql-1", "type": "sql_read", "title": "查询", "config": {"databaseRef": "1/db/db/read/svc", "sqlTemplate": "SELECT 1"}, "inputs": []}],
        "edges": [],
    }
    validated = validate_workflow(workflow, "PUBLISH")
    assert validated["normalizedDefinition"]["nodes"][0]["handlerVersion"] == "1.0.0"
    plan = render_plan(workflow_version_id="wfv", workflow_content_hash=validated["workflowContentHash"], workflow_snapshot=workflow, ticket_id=1, parameters={}, actor_uid="S1")
    assert plan["valid"] is True and plan["catalogDigest"] == catalog["catalogDigest"]
    with pytest.raises(ValueError, match="禁止保存凭据"):
        reject_credentials({"AOPS_API_KEY": "secret"})


def test_refinement_edge_is_bounded_and_targets_upstream_llm():
    workflow = {
        "entryNodeId": "llm",
        "nodes": [
            {"id": "llm", "type": "llm_extract", "title": "提取", "config": {"modelProfile": "model", "promptTemplateId": "prompt", "responseSchema": {"type": "object"}}, "inputs": [{"name": "user_feedback", "type": "string", "required": False, "source": {"kind": "RUN_INPUT", "key": "feedback"}}]},
            {"id": "hitl", "type": "hitl_select", "title": "选择", "config": {"selectionMode": "SINGLE", "autoSelectSingle": True, "zeroCandidatePolicy": "FAIL", "title": "选择"}, "inputs": [{"name": "candidates", "type": "array", "source": {"kind": "NODE_OUTPUT", "nodeId": "llm", "jsonPointer": "/candidates"}}]},
            {"id": "done", "type": "end", "title": "完成"},
        ],
        "edges": [
            {"id": "e1", "source": "llm", "target": "hitl"},
            {"id": "e2", "source": "hitl", "target": "done"},
            {"id": "r1", "kind": "REFINEMENT", "source": "hitl", "target": "llm", "maxIterations": 3, "feedbackInputName": "feedback"},
        ],
    }
    assert WorkflowDefinition.model_validate(workflow).edges[-1].kind == "REFINEMENT"
    workflow["edges"][-1]["maxIterations"] = 9
    with pytest.raises(ValueError):
        WorkflowDefinition.model_validate(workflow)


@pytest.mark.asyncio
async def test_studio_single_node_debug_and_hitl_reply(tmp_path):
    store = StudioStore(tmp_path / "studio.db")
    await store.initialize()
    sql = {"id": "sql", "type": "sql_read", "title": "查询", "config": {"databaseRef": "test/db", "sqlTemplate": "SELECT 1"}, "inputs": []}
    executed = run_single_node(node=sql, inputs={}, mode="SIMULATION", simulation={"fixtureOutput": {"status": 0, "data": [{"value": 1}], "rowCount": 1}})
    saved = await store.create_debug_run(creator_uid="admin", workspace_id=None, node=sql, inputs={}, mode="SIMULATION", handler_version="1.0.0", status=executed["status"], output=executed["output"], diagnostic=None, interrupt=None)
    assert (await store.get_debug_run(saved["debugRunId"], "admin", True))["output"]["rowCount"] == 1
    hitl = {"id": "hitl", "type": "hitl_select", "title": "选择", "config": {"selectionMode": "SINGLE", "autoSelectSingle": True, "zeroCandidatePolicy": "FAIL", "title": "选择"}, "inputs": []}
    waiting = run_single_node(node=hitl, inputs={"candidates": [{"id": "a", "label": "A", "value": 1}, {"id": "b", "label": "B", "value": 2}]}, mode="SIMULATION")
    record = await store.create_debug_run(creator_uid="admin", workspace_id=None, node=hitl, inputs={"candidates": []}, mode="SIMULATION", handler_version="1.0.0", status=waiting["status"], output=None, diagnostic=None, interrupt=waiting["interrupt"])
    resolved = await store.resolve_interrupt(record["debugRunId"], "admin", {"action": "SELECT", "candidateId": "b"})
    assert resolved["status"] == "SUCCEEDED" and resolved["output"]["selected"]["value"] == 2


@pytest.mark.asyncio
async def test_tec01_client_handles_claim_and_structured_error():
    calls = 0
    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(204)
        return httpx.Response(409, json={"code": "STATE_REVISION_CONFLICT", "message": "changed", "retryable": True, "details": {"actualRevision": 3}})
    client = Tec01Client(base_url="http://tec01", token="token", transport=httpx.MockTransport(handler))
    assert await client.claim_run({"executorId": "e", "runtimeVersion": "1", "availableSlots": 1}) is None
    with pytest.raises(Tec01Error) as error:
        await client.start_attempt("run", {}, "key")
    assert error.value.code == "STATE_REVISION_CONFLICT" and error.value.retryable is True


@pytest.mark.asyncio
async def test_remote_checkpointer_stages_and_reads_committed_checkpoint():
    class Client:
        payload = None
        async def stage_checkpoint(self, thread_id, checkpoint_id, payload, _key):
            self.payload = {**payload, "checkpointId": checkpoint_id, "checkpointNamespace": "", "pendingWrites": []}
            return {}
        async def get_checkpoint(self, _thread_id, _checkpoint_id=None, _namespace=""):
            return self.payload
        async def stage_checkpoint_writes(self, *_args): return {}
        async def list_checkpoints(self, *_args): return {"items": []}
        async def delete_checkpoint_thread(self, *_args): return None
    client = Client()
    saver = RemoteTec01Checkpointer(client)  # type: ignore[arg-type]
    config = {"configurable": {"thread_id": "run-1", "checkpoint_ns": "", "lease_token": "lease", "attempt_id": "att"}}
    checkpoint = {"v": 1, "id": "cp-1", "ts": "2026-09-20T00:00:00Z", "channel_values": {}, "channel_versions": {}, "versions_seen": {}, "updated_channels": None}
    next_config = await saver.aput(config, checkpoint, {"source": "loop", "step": 1, "parents": {}}, {})
    restored = await saver.aget_tuple(next_config)
    assert restored is not None and restored.checkpoint["id"] == "cp-1"
    assert client.payload["leaseToken"] == "lease"


@pytest.mark.asyncio
async def test_compiler_worker_completes_storage_agnostic_proposal(monkeypatch):
    completed = {}
    class Client:
        async def complete_extraction(self, extraction_id, payload, idempotency_key):
            completed.update({"id": extraction_id, "payload": payload, "key": idempotency_key})
            return {}
        async def fail_extraction(self, *_args, **_kwargs):
            raise AssertionError("unexpected failure")
    async def compile_evidence(_ticket, _timeline):
        workflow = {"entryNodeId": "done", "nodes": [{"id": "done", "type": "end", "title": "完成"}], "edges": []}
        return {"name": "测试", "summary": "测试草稿", "matchPhrases": ["测试"], "negativePhrases": [], "systemKeys": [], "workflowDefinition": workflow}, {"acceptedOperationCount": 1}
    monkeypatch.setattr("app.compiler_worker.compile_ticket_evidence", compile_evidence)
    evidence = {"ticketInfo": {"id": 1}, "auditTimeline": []}
    import hashlib
    digest = hashlib.sha256(json.dumps(evidence, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    await CompilerWorker(Client(), "compiler").process({"extractionId": "ext-1", "leaseToken": "lease", **evidence, "evidenceHash": digest, "targetCatalogDigest": NODE_REGISTRY.catalog()["catalogDigest"]})  # type: ignore[arg-type]
    assert completed["id"] == "ext-1" and completed["payload"]["proposal"]["workflowDefinition"]["schemaVersion"] == 2
