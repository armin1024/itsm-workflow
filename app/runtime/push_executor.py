from __future__ import annotations

import asyncio
import copy
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Awaitable, Callable

from app.cli import redact_diagnostic
from app.conditions import evaluate, pointer
from app.config import settings
from app.runtime import NODE_REGISTRY
from app.runtime.executor import execute_single_node
from app.runtime.planner import canonical_hash, validate_workflow
from app.tec01_client import Tec01Client, Tec01Error
from app.workflow import WorkflowDefinition, WorkflowEdge, WorkflowNode


EventSink = Callable[[dict[str, Any]], Awaitable[None]]
NODE_STATES = {"PENDING", "READY", "RUNNING", "WAITING", "WAITING_INPUT", "SUCCEEDED", "SKIPPED", "FAILED", "PAUSED", "CANCELLED", "UNKNOWN"}


def _now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass
class ActiveRun:
    dispatch_id: str
    task: asyncio.Task | None = None
    pause: asyncio.Event = field(default_factory=asyncio.Event)
    cancel: asyncio.Event = field(default_factory=asyncio.Event)
    current_node_id: str | None = None


class PushExecutor:
    """Active execution is transient; the caller owns every recoverable snapshot."""

    def __init__(self, client: Tec01Client | None = None):
        self.client = client or Tec01Client()
        self.active: dict[str, ActiveRun] = {}
        self.accepted: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._fingerprints: dict[str, str] = {}
        self.cli_slots = asyncio.Semaphore(settings.executor_max_active_cli)

    def dispatch(self, payload: dict[str, Any], api_key: str | None, *, event_sink: EventSink | None = None, sync_tec01: bool = True) -> tuple[dict[str, Any], bool]:
        dispatch_id, run_id = str(payload["dispatchId"]), str(payload["runId"])
        fingerprint = canonical_hash(payload)
        if dispatch_id in self.accepted:
            if self._fingerprints[dispatch_id] != fingerprint:
                raise RuntimeError("DISPATCH_PAYLOAD_CONFLICT")
            return dict(self.accepted[dispatch_id]), False
        if run_id in self.active:
            raise RuntimeError("RUN_ALREADY_ACTIVE")
        if len(self.active) >= settings.executor_max_active_runs:
            raise RuntimeError("EXECUTOR_NO_CAPACITY")
        validated = validate_workflow(dict(payload["workflowSnapshot"]), "EXECUTE")
        if str(payload["workflowContentHash"]) != validated["workflowContentHash"]:
            raise ValueError("WORKFLOW_CONTENT_HASH_MISMATCH")
        definition = WorkflowDefinition.model_validate(validated["normalizedDefinition"])
        node_map = {node.id: node for node in definition.nodes}
        states = dict(payload.get("nodeStates") or {})
        if any(key not in node_map or value not in NODE_STATES for key, value in states.items()):
            raise ValueError("NODE_STATES_INVALID")
        if any(value in {"RUNNING", "UNKNOWN"} for value in states.values()):
            raise ValueError("AMBIGUOUS_NODE_STATE：RUNNING/UNKNOWN必须先由tec01或调试用户确认")
        if payload.get("currentNodeId") and payload["currentNodeId"] not in node_map:
            raise ValueError("CURRENT_NODE_NOT_FOUND")
        resume = payload.get("resumePayload") or {}
        if resume.get("nodeId") and resume["nodeId"] not in node_map:
            raise ValueError("RESUME_NODE_NOT_FOUND")
        if any(node.type == "sql_read" for node in definition.nodes) and not api_key:
            raise ValueError("SQL读执行必须提供AOPS_API_KEY")
        if isinstance(payload.get("ticketId"), bool) or int(payload.get("ticketId") or 0) <= 0:
            raise ValueError("工单ID必须为正整数")
        for node in definition.nodes:
            manifest = NODE_REGISTRY.get(node.type, node.schemaVersion).manifest
            if node.handlerVersion != manifest.handler_version:
                raise ValueError(f"节点 {node.id} 的Handler版本不可用：{node.handlerVersion}")
        for node_id, edge_id in (payload.get("selectedRoutes") or {}).items():
            if not any(edge.id == edge_id and edge.source == node_id for edge in definition.edges):
                raise ValueError("SELECTED_ROUTE_INVALID")
        for node in definition.nodes:
            if node.type == "condition" and states.get(node.id) == "SUCCEEDED" and node.id not in (payload.get("selectedRoutes") or {}):
                raise ValueError(f"已完成条件节点 {node.id} 缺少已提交的分支")
        accepted = {"dispatchId": dispatch_id, "runId": run_id, "accepted": True, "executorId": settings.executor_id, "acceptedAt": _now(), "status": "RUNNING"}
        active = ActiveRun(dispatch_id)
        self.active[run_id] = active
        self.accepted[dispatch_id] = accepted
        self._fingerprints[dispatch_id] = fingerprint
        self._trim_metadata()
        active.task = asyncio.create_task(self._run(copy.deepcopy(payload), definition, api_key, active, event_sink, sync_tec01), name="execution:" + run_id)
        active.task.add_done_callback(lambda _: self.active.pop(run_id, None) if self.active.get(run_id) is active else None)
        return dict(accepted), True

    def _trim_metadata(self) -> None:
        active_ids = {item.dispatch_id for item in self.active.values()}
        for dispatch_id in list(self.accepted):
            if len(self.accepted) <= 256:
                break
            if dispatch_id not in active_ids:
                self.accepted.pop(dispatch_id, None)
                self._fingerprints.pop(dispatch_id, None)

    def command(self, run_id: str, command_id: str, command: str) -> dict[str, Any]:
        if command not in {"PAUSE", "CANCEL"}:
            raise ValueError("仅支持PAUSE或CANCEL；继续执行请重新下发快照")
        active = self.active.get(run_id)
        if not active:
            return {"commandId": command_id, "accepted": False, "reason": "RUN_NOT_ACTIVE"}
        (active.pause if command == "PAUSE" else active.cancel).set()
        return {"commandId": command_id, "accepted": True, "runId": run_id, "currentNodeId": active.current_node_id}

    @staticmethod
    def _resolve(node: WorkflowNode, run_inputs: dict[str, Any], outputs: dict[str, dict[str, Any]]) -> dict[str, Any]:
        values: dict[str, Any] = {}
        missing: list[str] = []
        for item in node.inputs:
            try:
                if item.source.kind == "RUN_INPUT":
                    value = run_inputs[str(item.source.key)]
                elif item.source.kind == "LITERAL":
                    value = item.source.value
                else:
                    value = pointer(outputs[str(item.source.nodeId)], str(item.source.jsonPointer))
                values[item.name] = value
            except (KeyError, IndexError, TypeError, ValueError):
                if item.required:
                    missing.append(item.name)
        if missing:
            raise ValueError(f"节点 {node.id} 缺少输入：{'、'.join(missing)}")
        return values

    @staticmethod
    def _next(node: WorkflowNode, outgoing: list[WorkflowEdge], state: dict[str, Any]) -> WorkflowEdge | None:
        if not outgoing:
            return None
        if node.type != "condition":
            return outgoing[0]
        return next((edge for edge in outgoing if not edge.default and edge.condition and evaluate(edge.condition, state)), None) or next((edge for edge in outgoing if edge.default), None)

    @staticmethod
    def _skipped(selected: WorkflowEdge | None, alternatives: list[WorkflowEdge], outgoing: dict[str, list[WorkflowEdge]]) -> list[str]:
        def reachable(start: str) -> set[str]:
            seen, pending = set(), [start]
            while pending:
                item = pending.pop()
                if item not in seen:
                    seen.add(item)
                    pending.extend(edge.target for edge in outgoing[item])
            return seen
        kept = reachable(selected.target) if selected else set()
        skipped: set[str] = set()
        for edge in alternatives:
            if not selected or edge.id != selected.id:
                skipped.update(reachable(edge.target) - kept)
        return sorted(skipped)

    async def _run(self, payload: dict[str, Any], definition: WorkflowDefinition, api_key: str | None, active: ActiveRun, event_sink: EventSink | None, sync_tec01: bool) -> None:
        run_id, dispatch_id = str(payload["runId"]), str(payload["dispatchId"])
        nodes = {node.id: node for node in definition.nodes}
        outgoing: dict[str, list[WorkflowEdge]] = {node.id: [] for node in definition.nodes}
        for edge in definition.edges:
            outgoing[edge.source].append(edge)
        states = {node.id: "PENDING" for node in definition.nodes}
        states.update(payload.get("nodeStates") or {})
        outputs = dict(payload.get("nodeOutputs") or {})
        routes = dict(payload.get("selectedRoutes") or {})
        results = copy.deepcopy(payload.get("nodeResults") or {})
        run_inputs = dict(payload.get("runInputs") or {})
        current = str(payload.get("currentNodeId") or (payload.get("resumePayload") or {}).get("nodeId") or definition.entryNodeId)
        status, interaction = "RUNNING", None
        external_completed = False
        completion_committed = False

        def snapshot() -> dict[str, Any]:
            return {"runId": run_id, "dispatchId": dispatch_id, "ticketId": payload["ticketId"], "workflowSnapshot": payload["workflowSnapshot"], "workflowContentHash": payload["workflowContentHash"], "runInputs": run_inputs, "nodeStates": states, "nodeOutputs": outputs, "selectedRoutes": routes, "currentNodeId": current, "nodeResults": results, "interaction": interaction, "status": status}

        def safe_message(value: Any) -> str:
            text = str(value)
            if api_key:
                text = text.replace(api_key, "***")
            return redact_diagnostic(text, 1000)[0]

        async def notify(kind: str, body: dict[str, Any], *, remote: bool = True) -> None:
            nonlocal completion_committed
            if sync_tec01 and remote:
                if kind == "NODE_STARTED":
                    await self.client.node_started(run_id, str(body["nodeId"]), body)
                elif kind == "NODE_COMPLETED":
                    await self.client.node_completed(run_id, str(body["nodeId"]), body)
                    completion_committed = True
                else:
                    await self.client.run_released(run_id, body)
            if event_sink:
                await event_sink(copy.deepcopy({**body, "type": kind, "runId": run_id, "occurredAt": _now(), "snapshot": snapshot()}))
            if kind == "NODE_COMPLETED" and not sync_tec01:
                completion_committed = True

        async def release(value: str, reason: str, *, remote: bool = True) -> None:
            nonlocal status
            status = value
            record = self.accepted.get(dispatch_id)
            if record is not None:
                record.update({"status": value, "finishedAt": _now()})
            await notify("RUN_RELEASED", {"dispatchId": dispatch_id, "status": value, "currentNodeId": current, "reason": safe_message(reason)}, remote=remote)

        try:
            for _ in range(len(nodes) + 1):
                active.current_node_id = current
                if active.cancel.is_set():
                    await release("CANCELLED", "用户取消")
                    return
                if active.pause.is_set():
                    await release("PAUSED", "用户暂停")
                    return
                node = nodes[current]
                if states[current] == "SKIPPED":
                    raise ValueError(f"不能从已跳过节点 {current} 恢复")
                if states[current] == "SUCCEEDED":
                    edge = next((item for item in outgoing[current] if item.id == routes.get(current)), None) if node.type == "condition" else (outgoing[current][0] if outgoing[current] else None)
                    if not edge:
                        await release("SUCCEEDED", "Workflow执行完成")
                        return
                    current = edge.target
                    continue
                if states[current] == "CANCELLED":
                    raise ValueError("已取消节点不能自动重新执行")
                attempt_id = "attempt_" + uuid.uuid4().hex
                external_completed = False
                completion_committed = False
                states[current] = "RUNNING"
                interaction = None
                results[current] = {"attemptId": attempt_id, "status": "RUNNING", "startedAt": _now()}
                await notify("NODE_STARTED", {"dispatchId": dispatch_id, "attemptId": attempt_id, "nodeId": current, "startedAt": results[current]["startedAt"]})
                try:
                    values = self._resolve(node, run_inputs, outputs)
                    state = {"inputs": run_inputs, "nodes": {key: {"output": value} for key, value in outputs.items()}}
                    edge = self._next(node, outgoing[current], state)
                    skipped = self._skipped(edge, outgoing[current], outgoing) if node.type == "condition" else []
                    if node.type == "condition":
                        result = {"status": "SUCCEEDED", "output": {"selectedTarget": edge.target if edge else None, "matched": bool(edge and not edge.default)}, "diagnostic": None, "interrupt": None}
                    else:
                        async def cancelled() -> bool:
                            return active.cancel.is_set()
                        args = {"node": node.model_dump(mode="json"), "inputs": values, "mode": "PRODUCTION", "api_key": api_key, "ticket_id": int(payload["ticketId"]), "resume_payload": payload.get("resumePayload"), "cancel_requested": cancelled}
                        if node.type == "sql_read":
                            acquired = False
                            try:
                                while not acquired and not active.cancel.is_set():
                                    try:
                                        await asyncio.wait_for(self.cli_slots.acquire(), 0.2)
                                        acquired = True
                                    except TimeoutError:
                                        pass
                                if active.cancel.is_set():
                                    result = {"status": "CANCELLED", "output": None, "diagnostic": None, "interrupt": None}
                                else:
                                    result = await execute_single_node(**args)
                            finally:
                                if acquired:
                                    self.cli_slots.release()
                        else:
                            result = await execute_single_node(**args)
                    external_completed = node.type == "sql_read" and result["status"] == "SUCCEEDED"
                except Exception as exc:
                    result = {"status": "FAILED", "output": None, "diagnostic": {"errorCode": "NODE_EXECUTION_FAILED", "message": safe_message(exc)}, "interrupt": None}
                    edge, skipped = None, []

                result_status = str(result["status"])
                states[current] = "WAITING" if result_status == "WAITING_INPUT" else result_status
                if result_status == "SUCCEEDED":
                    outputs[current] = result["output"]
                    if edge:
                        routes[current] = edge.id
                    for node_id in skipped:
                        states[node_id] = "SKIPPED"
                    if edge and states[edge.target] == "PENDING":
                        states[edge.target] = "READY"
                interaction = result.get("interrupt")
                if interaction:
                    interaction = {**interaction, "nodeId": current}
                diagnostic = result.get("diagnostic")
                if diagnostic:
                    diagnostic = {key: safe_message(value) if isinstance(value, str) else value for key, value in diagnostic.items()}
                results[current].update({"status": result_status, "finishedAt": _now(), "diagnostic": diagnostic, "interaction": interaction})
                summary = f"{node.title}：{result_status}"
                if node.type == "sql_read" and result_status == "SUCCEEDED":
                    summary = f"查询返回 {result['output'].get('rowCount', 0)} 行"
                elif diagnostic:
                    summary = safe_message(diagnostic.get("message") or summary)
                callback = {"dispatchId": dispatch_id, "attemptId": attempt_id, "nodeId": current, "status": result_status, "safeSummary": summary, "result": result.get("output"), "diagnostic": diagnostic, "interaction": interaction, "selectedEdgeId": edge.id if edge and result_status == "SUCCEEDED" else None, "skippedNodeIds": skipped if result_status == "SUCCEEDED" else []}
                await notify("NODE_COMPLETED", callback)
                external_completed = False
                if result_status != "SUCCEEDED":
                    await release(result_status, summary)
                    return
                if active.cancel.is_set():
                    await release("CANCELLED", "用户取消；已完成节点结果保留")
                    return
                if active.pause.is_set():
                    await release("PAUSED", "当前节点已完成，运行暂停")
                    return
                if not edge:
                    await release("SUCCEEDED", "Workflow执行完成")
                    return
                current = edge.target
            raise ValueError("WORKFLOW_NO_TERMINAL_NODE")
        except asyncio.CancelledError:
            terminal = "UNKNOWN" if status != "RUNNING" else "PAUSED"
            if (external_completed and not completion_committed) or (states.get(current) == "RUNNING" and nodes[current].type == "sql_read"):
                states[current], terminal = "UNKNOWN", "UNKNOWN"
            try:
                await release(terminal, "执行服务关闭，请根据已保存快照确认后继续")
            except Exception:
                await release(terminal, "执行服务关闭，状态回调未确认", remote=False)
        except Exception as exc:
            # A lost completion acknowledgement makes the SQL outcome uncertain.
            # A lost release acknowledgement only affects the run: the node's
            # already committed result must remain SUCCEEDED and never replay.
            uncertain = external_completed and not completion_committed
            if uncertain:
                states[current] = "UNKNOWN"
            release_unconfirmed = isinstance(exc, Tec01Error) and status != "RUNNING"
            terminal = "UNKNOWN" if uncertain or release_unconfirmed else "FAILED"
            try:
                await release(terminal, safe_message(exc))
            except Exception:
                await release(terminal, "tec01状态回调未确认，请人工核实后恢复", remote=False)

    async def shutdown(self, timeout: float = 10) -> None:
        tasks = [active.task for active in self.active.values() if active.task]
        for active in self.active.values():
            active.cancel.set()
        if not tasks:
            return
        _, pending = await asyncio.wait(tasks, timeout=timeout)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.wait(pending, timeout=2)


push_executor = PushExecutor()
