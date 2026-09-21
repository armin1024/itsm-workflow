from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from app.conditions import evaluate, pointer
from app.config import settings
from app.runtime.executor import execute_single_node
from app.runtime.planner import validate_workflow
from app.tec01_client import Tec01Client
from app.workflow import WorkflowDefinition, WorkflowEdge, WorkflowNode


@dataclass
class ActiveRun:
    dispatch_id: str
    task: asyncio.Task | None = None
    pause: asyncio.Event = field(default_factory=asyncio.Event)
    cancel: asyncio.Event = field(default_factory=asyncio.Event)
    current_node_id: str | None = None


class PushExecutor:
    def __init__(self, client: Tec01Client | None = None):
        self.client = client or Tec01Client(); self.active: dict[str, ActiveRun] = {}; self.accepted: dict[str, dict[str, Any]] = {}; self.cli_slots = asyncio.Semaphore(settings.executor_max_active_cli)

    def dispatch(self, payload: dict[str, Any], api_key: str | None) -> tuple[dict[str, Any], bool]:
        dispatch_id, run_id = str(payload["dispatchId"]), str(payload["runId"])
        if dispatch_id in self.accepted: return self.accepted[dispatch_id], False
        if run_id in self.active: raise RuntimeError("RUN_ALREADY_ACTIVE")
        if len(self.active) >= settings.executor_max_active_runs: raise RuntimeError("EXECUTOR_NO_CAPACITY")
        accepted = {"dispatchId": dispatch_id, "runId": run_id, "accepted": True, "executorId": settings.executor_id, "acceptedAt": datetime.now(UTC).isoformat()}
        active = ActiveRun(dispatch_id); self.active[run_id] = active; self.accepted[dispatch_id] = accepted
        active.task = asyncio.create_task(self._run(payload, api_key, active), name="execution:" + run_id)
        active.task.add_done_callback(lambda _: self.active.pop(run_id, None))
        return accepted, True

    def command(self, run_id: str, command_id: str, command: str) -> dict[str, Any]:
        active = self.active.get(run_id)
        if not active: return {"commandId": command_id, "accepted": False, "reason": "RUN_NOT_ACTIVE"}
        (active.pause if command == "PAUSE" else active.cancel).set()
        return {"commandId": command_id, "accepted": True, "runId": run_id, "currentNodeId": active.current_node_id}

    @staticmethod
    def _resolve(node: WorkflowNode, run_inputs: dict[str, Any], outputs: dict[str, dict[str, Any]]) -> dict[str, Any]:
        values: dict[str, Any] = {}; missing: list[str] = []
        for item in node.inputs:
            try:
                if item.source.kind == "RUN_INPUT": value = run_inputs[str(item.source.key)]
                elif item.source.kind == "LITERAL": value = item.source.value
                else: value = pointer(outputs[str(item.source.nodeId)], str(item.source.jsonPointer))
                values[item.name] = value
            except (KeyError, IndexError, TypeError, ValueError):
                if item.required: missing.append(item.name)
        if missing: raise ValueError(f"节点 {node.id} 缺少输入：{'、'.join(missing)}")
        return values

    @staticmethod
    def _next(node: WorkflowNode, outgoing: list[WorkflowEdge], state: dict[str, Any]) -> tuple[WorkflowEdge | None, list[str]]:
        if not outgoing: return None, []
        if node.type != "condition": return outgoing[0], []
        selected = next((edge for edge in outgoing if not edge.default and edge.condition and evaluate(edge.condition, state)), None) or next((edge for edge in outgoing if edge.default), None)
        return selected, [edge.target for edge in outgoing if selected and edge.id != selected.id]

    async def _run(self, payload: dict[str, Any], api_key: str | None, active: ActiveRun) -> None:
        run_id, dispatch_id = str(payload["runId"]), str(payload["dispatchId"])
        try:
            validated = validate_workflow(dict(payload["workflowSnapshot"]), "EXECUTE")
            if str(payload["workflowContentHash"]) != validated["workflowContentHash"]: raise ValueError("WORKFLOW_CONTENT_HASH_MISMATCH")
            definition = WorkflowDefinition.model_validate(validated["normalizedDefinition"]); nodes = {node.id: node for node in definition.nodes}
            outgoing: dict[str, list[WorkflowEdge]] = {node.id: [] for node in definition.nodes}
            for edge in definition.edges: outgoing[edge.source].append(edge)
            statuses = dict(payload.get("nodeStates") or {}); outputs = dict(payload.get("nodeOutputs") or {}); run_inputs = dict(payload.get("runInputs") or {})
            current = str(payload.get("currentNodeId") or (payload.get("resumePayload") or {}).get("nodeId") or definition.entryNodeId)
            for _ in range(len(definition.nodes) + 1):
                active.current_node_id = current
                if active.cancel.is_set(): await self.client.run_released(run_id, {"dispatchId": dispatch_id, "status": "CANCELLED", "currentNodeId": current, "reason": "用户取消"}); return
                if active.pause.is_set(): await self.client.run_released(run_id, {"dispatchId": dispatch_id, "status": "PAUSED", "currentNodeId": current, "reason": "用户暂停"}); return
                node = nodes[current]
                if statuses.get(current) in {"SUCCEEDED", "SKIPPED"}:
                    route_id = (payload.get("selectedRoutes") or {}).get(current); edge = next((item for item in outgoing[current] if item.id == route_id), None) or (outgoing[current][0] if outgoing[current] else None)
                    if not edge: break
                    current = edge.target; continue
                values = self._resolve(node, run_inputs, outputs); attempt_id = "attempt_" + uuid.uuid4().hex
                await self.client.node_started(run_id, node.id, {"dispatchId": dispatch_id, "attemptId": attempt_id, "startedAt": datetime.now(UTC).isoformat()})
                if node.type == "condition":
                    state = {"inputs": run_inputs, "nodes": {key: {"output": value} for key, value in outputs.items()}}; edge, skipped = self._next(node, outgoing[node.id], state)
                    result = {"status": "SUCCEEDED", "output": {"selectedTarget": edge.target if edge else None, "matched": bool(edge and not edge.default)}, "diagnostic": None, "interrupt": None, "handlerVersion": node.handlerVersion or "1.0.0"}
                else:
                    async def cancelled() -> bool: return active.cancel.is_set()
                    if node.type == "sql_read":
                        async with self.cli_slots: result = await execute_single_node(node=node.model_dump(mode="json"), inputs=values, mode="PRODUCTION", api_key=api_key, ticket_id=int(payload["ticketId"]), resume_payload=payload.get("resumePayload"), cancel_requested=cancelled)
                    else: result = await execute_single_node(node=node.model_dump(mode="json"), inputs=values, mode="PRODUCTION", api_key=api_key, ticket_id=int(payload["ticketId"]), resume_payload=payload.get("resumePayload"))
                    edge, skipped = self._next(node, outgoing[node.id], {"inputs": run_inputs, "nodes": {key: {"output": value} for key, value in outputs.items()}})
                callback = {"dispatchId": dispatch_id, "attemptId": attempt_id, "status": result["status"], "safeSummary": f"{node.title}：{result['status']}", "result": result.get("output"), "diagnostic": result.get("diagnostic"), "interaction": result.get("interrupt"), "selectedEdgeId": edge.id if edge and result["status"] == "SUCCEEDED" else None, "skippedNodeIds": skipped if result["status"] == "SUCCEEDED" else []}
                await self.client.node_completed(run_id, node.id, callback)
                if result["status"] != "SUCCEEDED": await self.client.run_released(run_id, {"dispatchId": dispatch_id, "status": result["status"], "currentNodeId": node.id, "reason": callback["safeSummary"]}); return
                outputs[node.id] = result["output"]
                if active.pause.is_set(): await self.client.run_released(run_id, {"dispatchId": dispatch_id, "status": "PAUSED", "currentNodeId": node.id, "reason": "用户暂停"}); return
                if not edge: await self.client.run_released(run_id, {"dispatchId": dispatch_id, "status": "SUCCEEDED", "currentNodeId": node.id, "reason": "Workflow执行完成"}); return
                current = edge.target
            raise ValueError("WORKFLOW_NO_TERMINAL_NODE")
        except Exception as exc:
            await self.client.run_released(run_id, {"dispatchId": dispatch_id, "status": "FAILED", "currentNodeId": active.current_node_id, "reason": str(exc)[:1000]})


push_executor = PushExecutor()
