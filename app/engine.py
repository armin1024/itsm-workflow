from __future__ import annotations

import copy
import json
import re
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Awaitable, Callable, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.cli import CliExecutionError, execute_sql_read
from app.conditions import evaluate, pointer
from app.config import settings
from app.crypto import SecretBox, sha256_bytes
from app.events import emit_event
from app.models import EncryptedArtifact, InterruptRecord, NodeAttempt, RunCredential, WorkflowRun
from app.workflow import WorkflowDefinition, WorkflowNode
from app.runtime.model_client import ModelInvocationError, invoke_structured


class RuntimeState(TypedDict):
    run_id: str
    inputs: dict[str, Any]
    output_refs: dict[str, str]
    routes: dict[str, str]
    refinements: dict[str, int]


class NodeExecutionError(RuntimeError):
    pass


def _id(prefix: str) -> str:
    return prefix + uuid.uuid4().hex


def _sql_literal(value: Any, value_type: str) -> str:
    if value_type == "string":
        return "'" + str(value).replace("'", "''") + "'"
    if value_type == "integer":
        if isinstance(value, bool):
            raise ValueError("布尔值不能作为整数")
        return str(int(value))
    if value_type == "number":
        if isinstance(value, bool):
            raise ValueError("布尔值不能作为数字")
        return str(float(value))
    if value_type == "boolean":
        return "1" if bool(value) else "0"
    raise ValueError(f"SQL 参数不支持类型 {value_type}")


class WorkflowEngine:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession], checkpointer, handlers: dict[str, Callable[..., Awaitable[dict[str, Any]]]] | None = None):
        self.sessions = session_factory
        self.checkpointer = checkpointer
        self.handlers = handlers or {
            "sql_read": self._handle_sql,
            "condition": self._handle_condition,
            "llm_extract": self._handle_llm_extract,
            "hitl_select": self._handle_hitl_select,
            "human_input": self._handle_human_input,
            "approval": self._handle_noop,
            "end": self._handle_noop,
        }

    async def _handle_sql(self, node: WorkflowNode, state: RuntimeState, values: dict[str, Any], _outgoing, _all_outgoing) -> dict[str, Any]:
        return await self._execute_sql(node, state, values)

    async def _handle_condition(self, node: WorkflowNode, state: RuntimeState, _values: dict[str, Any], outgoing, all_outgoing) -> dict[str, Any]:
        return await self._condition(node, state, outgoing, all_outgoing)

    async def _handle_llm_extract(self, node: WorkflowNode, state: RuntimeState, values: dict[str, Any], _outgoing, _all_outgoing) -> dict[str, Any]:
        attempt_id, _ = await self._mark_running(state["run_id"], node)
        try:
            config = node.config
            output = await invoke_structured(prompt_template_id=str(config.get("promptTemplateId") or ""), inputs=values, response_schema=dict(config.get("responseSchema") or {}), timeout_seconds=node.timeoutSeconds)
            artifact_id = await self._complete_output(state["run_id"], node, attempt_id, values, output, "LLM结构化提取完成")
            return {"output_refs": {**state["output_refs"], node.id: artifact_id}}
        except (ModelInvocationError, ValueError) as exc:
            await self._fail_attempt(state["run_id"], node, attempt_id, "MODEL_OUTPUT_INVALID", str(exc))
            raise NodeExecutionError(str(exc)) from exc

    async def _handle_hitl_select(self, node: WorkflowNode, state: RuntimeState, values: dict[str, Any], outgoing, _all_outgoing) -> dict[str, Any]:
        normal_edge = next((edge for edge in outgoing if edge.get("kind") != "REFINEMENT"), None)
        refinement_edge = next((edge for edge in outgoing if edge.get("kind") == "REFINEMENT"), None)
        candidates = values.get("candidates")
        if not isinstance(candidates, list):
            raise NodeExecutionError("HITL候选必须是数组")
        if len(candidates) == 1 and bool(node.config.get("autoSelectSingle", True)):
            attempt_id, _ = await self._mark_running(state["run_id"], node)
            output = {"selected": candidates[0], "selectionMode": "AUTO_SELECTED_SINGLE"}
            artifact_id = await self._complete_output(state["run_id"], node, attempt_id, values, output, "唯一候选已自动选择")
            result = {"output_refs": {**state["output_refs"], node.id: artifact_id}}
            if normal_edge:
                result["routes"] = {**state["routes"], node.id: normal_edge["target"]}
            return result
        if not candidates:
            raise NodeExecutionError("没有可供选择的候选")
        async with self.sessions() as session:
            record = await session.scalar(select(InterruptRecord).where(InterruptRecord.run_id == state["run_id"], InterruptRecord.node_id == node.id, InterruptRecord.kind == "HITL_SELECT", InterruptRecord.status == "OPEN"))
        if record is None:
            attempt_id, _ = await self._mark_running(state["run_id"], node)
            actions = ["SELECT", "MANUAL_VALUE", "CANCEL"]
            if refinement_edge:
                actions.insert(1, "REFINE")
            request = {"message": str(node.config.get("title") or "请选择候选"), "actions": actions, "candidates": candidates, "attemptId": attempt_id, "iteration": int((state.get("refinements") or {}).get(node.id, 0)), "maxIterations": int(refinement_edge.get("maxIterations") or 0) if refinement_edge else 0}
            record = await self._open_interrupt(state["run_id"], node.id, "HITL_SELECT", request, "WAITING_INPUT")
            async with self.sessions() as session:
                attempt = await session.get(NodeAttempt, attempt_id)
                attempt.status = "WAITING"
                await session.commit()
        else:
            attempt_id = str(record.request_payload.get("attemptId") or "")
        response = interrupt({"interruptId": record.id, "kind": "HITL_SELECT", **record.request_payload})
        if not isinstance(response, dict):
            raise NodeExecutionError("HITL回复格式无效")
        action = str(response.get("action") or "")
        if action == "SELECT":
            selected = next((item for item in candidates if isinstance(item, dict) and item.get("id") == response.get("candidateId")), None)
            if selected is None:
                raise NodeExecutionError("candidateId不在候选集合中")
        elif action == "MANUAL_VALUE":
            selected = {"id": "manual", "value": response.get("value"), "reason": response.get("reason", "")}
        elif action == "CANCEL":
            await self._resolve_interrupt(record.id, response)
            raise NodeExecutionError("用户取消HITL选择")
        elif action == "REFINE":
            if not refinement_edge:
                raise NodeExecutionError("当前HITL未配置REFINEMENT边")
            current_iteration = int((state.get("refinements") or {}).get(node.id, 0))
            maximum = int(refinement_edge.get("maxIterations") or 0)
            feedback = str(response.get("feedback") or "").strip()
            if not feedback:
                raise NodeExecutionError("REFINE必须提供feedback")
            if current_iteration >= maximum:
                raise NodeExecutionError("REFINEMENT_LIMIT_REACHED")
            await self._resolve_interrupt(record.id, response)
            output = {"refinement": {"feedback": feedback, "iteration": current_iteration + 1}}
            await self._complete_output(state["run_id"], node, attempt_id, values, output, "用户补充条件，重新执行LLM提取")
            async with self.sessions() as session:
                run = await session.get(WorkflowRun, state["run_id"])
                statuses, refs = dict(run.node_statuses), dict(run.output_refs or {})
                statuses[refinement_edge["target"]] = "PENDING"
                statuses[node.id] = "PENDING"
                refs.pop(refinement_edge["target"], None)
                refs.pop(node.id, None)
                run.node_statuses, run.output_refs = statuses, refs
                await session.commit()
            feedback_name = str(refinement_edge.get("feedbackInputName"))
            return {"inputs": {**state["inputs"], feedback_name: feedback}, "output_refs": {key: value for key, value in state["output_refs"].items() if key not in {node.id, refinement_edge["target"]}}, "routes": {**state["routes"], node.id: refinement_edge["target"]}, "refinements": {**(state.get("refinements") or {}), node.id: current_iteration + 1}}
        else:
            raise NodeExecutionError("HITL action无效")
        await self._resolve_interrupt(record.id, response)
        output = {"selected": selected, "selectionMode": action}
        artifact_id = await self._complete_output(state["run_id"], node, attempt_id, values, output, "用户已完成候选选择")
        result = {"output_refs": {**state["output_refs"], node.id: artifact_id}}
        if normal_edge:
            result["routes"] = {**state["routes"], node.id: normal_edge["target"]}
        return result

    async def _handle_human_input(self, node: WorkflowNode, state: RuntimeState, _values: dict[str, Any], _outgoing, _all_outgoing) -> dict[str, Any]:
        await self._mark_simple_success(state["run_id"], node, "人工输入已完成")
        return {"inputs": state["inputs"]}

    async def _handle_noop(self, node: WorkflowNode, state: RuntimeState, _values: dict[str, Any], _outgoing, _all_outgoing) -> dict[str, Any]:
        await self._mark_simple_success(state["run_id"], node, node.title + "已完成")
        return {}

    async def _load_artifact(self, artifact_id: str, run_id: str) -> dict[str, Any]:
        async with self.sessions() as session:
            artifact = await session.get(EncryptedArtifact, artifact_id)
            if not artifact or artifact.run_id != run_id:
                raise NodeExecutionError("前置节点结果不存在")
            return SecretBox().open(artifact.ciphertext, purpose="artifact:" + artifact.id)

    async def _resolve_inputs(self, node: WorkflowNode, state: RuntimeState) -> tuple[dict[str, Any], list[str]]:
        values: dict[str, Any] = {}
        missing: list[str] = []
        for item in node.inputs:
            source = item.source
            try:
                if source.kind == "RUN_INPUT":
                    value = state["inputs"][str(source.key)]
                elif source.kind == "LITERAL":
                    value = source.value
                else:
                    artifact_id = state["output_refs"][str(source.nodeId)]
                    artifact = await self._load_artifact(artifact_id, state["run_id"])
                    value = pointer(artifact["output"], str(source.jsonPointer))
                values[item.name] = value
            except (KeyError, IndexError, ValueError, TypeError, NodeExecutionError):
                if item.required:
                    missing.append(item.name)
        return values, missing

    async def _open_interrupt(self, run_id: str, node_id: str, kind: str, request: dict[str, Any], run_status: str) -> InterruptRecord:
        async with self.sessions() as session:
            existing = await session.scalar(select(InterruptRecord).where(InterruptRecord.run_id == run_id, InterruptRecord.node_id == node_id, InterruptRecord.kind == kind, InterruptRecord.status == "OPEN"))
            run = await session.get(WorkflowRun, run_id)
            if existing:
                return existing
            record = InterruptRecord(id=_id("int_"), run_id=run_id, node_id=node_id, kind=kind, request_payload=request)
            session.add(record)
            run.status, run.waiting_reason = run_status, request.get("message", kind)
            statuses = dict(run.node_statuses)
            statuses[node_id] = "WAITING"
            run.node_statuses = statuses
            await emit_event(session, run, "INTERRUPT_OPENED", node_id=node_id, status=run_status, summary=run.waiting_reason, payload={"interruptId": record.id, "kind": kind, **request})
            await session.commit()
            return record

    async def _resolve_interrupt(self, record_id: str, response: Any) -> None:
        async with self.sessions() as session:
            record = await session.get(InterruptRecord, record_id)
            run = await session.get(WorkflowRun, record.run_id)
            record.status, record.response_payload, record.resolved_at = "RESOLVED", response if isinstance(response, dict) else {"value": response}, datetime.now(UTC)
            run.status, run.waiting_reason = "RUNNING", None
            await emit_event(session, run, "INTERRUPT_RESOLVED", node_id=record.node_id, status="RUNNING", summary=f"{record.kind} 已处理", payload={"interruptId": record.id})
            await session.commit()

    async def _safe_point(self, node: WorkflowNode, state: RuntimeState) -> None:
        async with self.sessions() as session:
            run = await session.get(WorkflowRun, state["run_id"])
            if run.status == "CANCEL_REQUESTED":
                statuses = dict(run.node_statuses)
                statuses[node.id] = "CANCELLED"
                run.node_statuses, run.status, run.finished_at = statuses, "CANCELLED", datetime.now(UTC)
                await emit_event(session, run, "RUN_CANCELLED", node_id=node.id, status="CANCELLED", summary="运行在节点安全边界取消")
                await session.commit()
                raise NodeExecutionError("运行已取消")
            open_pause = await session.scalar(select(InterruptRecord).where(InterruptRecord.run_id == run.id, InterruptRecord.node_id == node.id, InterruptRecord.kind == "PAUSE", InterruptRecord.status == "OPEN"))
            should_pause = run.status == "PAUSE_REQUESTED" or open_pause is not None
        if should_pause:
            record = open_pause or await self._open_interrupt(state["run_id"], node.id, "PAUSE", {"message": "工作流已在安全边界暂停"}, "PAUSED")
            response = interrupt({"interruptId": record.id, "kind": "PAUSE", "message": "工作流已暂停"})
            await self._resolve_interrupt(record.id, response)

    async def _node_approval(self, node: WorkflowNode, state: RuntimeState) -> None:
        if node.approvalPolicy != "NODE" and node.type != "approval":
            return
        record = await self._open_interrupt(state["run_id"], node.id, "NODE_APPROVAL", {"message": f"节点“{node.title}”等待批准", "node": node.model_dump(mode="json")}, "WAITING_NODE_APPROVAL")
        response = interrupt({"interruptId": record.id, "kind": "NODE_APPROVAL", **record.request_payload})
        if not isinstance(response, dict) or response.get("decision") != "approve":
            raise NodeExecutionError("节点未获批准")
        await self._resolve_interrupt(record.id, response)

    async def _missing_inputs(self, node: WorkflowNode, state: RuntimeState, missing: list[str]) -> dict[str, Any]:
        record = await self._open_interrupt(state["run_id"], node.id, "HUMAN_INPUT", {"message": f"节点“{node.title}”缺少输入", "fields": missing}, "WAITING_INPUT")
        response = interrupt({"interruptId": record.id, "kind": "HUMAN_INPUT", **record.request_payload})
        if not isinstance(response, dict) or not isinstance(response.get("inputs"), dict):
            raise NodeExecutionError("恢复输入格式无效")
        await self._resolve_interrupt(record.id, response)
        return response["inputs"]

    async def _credential(self, node: WorkflowNode, state: RuntimeState) -> str:
        async with self.sessions() as session:
            credential = await session.scalar(select(RunCredential).where(RunCredential.run_id == state["run_id"]))
            open_record = await session.scalar(select(InterruptRecord).where(InterruptRecord.run_id == state["run_id"], InterruptRecord.node_id == node.id, InterruptRecord.kind == "CREDENTIAL", InterruptRecord.status == "OPEN"))
            expires_at = credential.expires_at if credential else None
            comparison_now = datetime.now(UTC) if expires_at and expires_at.tzinfo else datetime.now(UTC).replace(tzinfo=None)
            valid = bool(credential and expires_at > comparison_now)
        if open_record or not valid:
            record = open_record or await self._open_interrupt(state["run_id"], node.id, "CREDENTIAL", {"message": "运行凭据已过期，请重新验证 AOPS API Key"}, "WAITING_CREDENTIAL")
            response = interrupt({"interruptId": record.id, "kind": "CREDENTIAL", **record.request_payload})
            await self._resolve_interrupt(record.id, response)
        async with self.sessions() as session:
            credential = await session.scalar(select(RunCredential).where(RunCredential.run_id == state["run_id"]))
            if not credential:
                raise NodeExecutionError("运行凭据不存在")
            return str(SecretBox().open(credential.ciphertext, purpose="run-credential:" + state["run_id"])["apiKey"])

    async def _mark_running(self, run_id: str, node: WorkflowNode) -> tuple[str, int]:
        async with self.sessions() as session:
            run = await session.get(WorkflowRun, run_id)
            count = await session.scalar(select(func.count()).select_from(NodeAttempt).where(NodeAttempt.run_id == run_id, NodeAttempt.node_id == node.id))
            attempt_id = _id("att_")
            attempt = NodeAttempt(id=attempt_id, run_id=run_id, node_id=node.id, attempt=int(count or 0) + 1, status="STARTED", command_summary=f"{node.type}: {node.title}")
            session.add(attempt)
            statuses = dict(run.node_statuses)
            statuses[node.id] = "RUNNING"
            run.node_statuses, run.current_node_id, run.status = statuses, node.id, "RUNNING"
            if run.started_at is None:
                run.started_at = datetime.now(UTC)
            await emit_event(session, run, "NODE_STARTED", node_id=node.id, attempt_id=attempt_id, status="RUNNING", summary=node.title)
            await session.commit()
            return attempt_id, attempt.attempt

    async def _mark_simple_success(self, run_id: str, node: WorkflowNode, summary: str) -> None:
        attempt_id, _ = await self._mark_running(run_id, node)
        async with self.sessions() as session:
            run = await session.get(WorkflowRun, run_id)
            attempt = await session.get(NodeAttempt, attempt_id)
            attempt.status, attempt.finished_at = "SUCCEEDED", datetime.now(UTC)
            statuses = dict(run.node_statuses)
            statuses[node.id] = "SUCCEEDED"
            run.node_statuses, run.current_node_id = statuses, node.id
            await emit_event(session, run, "NODE_SUCCEEDED", node_id=node.id, attempt_id=attempt_id, status="SUCCEEDED", summary=summary)
            await session.commit()

    async def _complete_output(self, run_id: str, node: WorkflowNode, attempt_id: str, inputs: dict[str, Any], output: dict[str, Any], summary: str) -> str:
        artifact_id = _id("art_")
        payload = {"input": inputs, "output": output}
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        async with self.sessions() as session:
            run = await session.get(WorkflowRun, run_id)
            attempt = await session.get(NodeAttempt, attempt_id)
            session.add(EncryptedArtifact(id=artifact_id, run_id=run_id, node_id=node.id, artifact_type="RESULT", ciphertext=SecretBox().seal(payload, purpose="artifact:" + artifact_id), content_hash=sha256_bytes(encoded), size_bytes=len(encoded), expires_at=datetime.now(UTC) + timedelta(days=settings.result_retention_days)))
            attempt.status, attempt.artifact_id, attempt.finished_at = "SUCCEEDED", artifact_id, datetime.now(UTC)
            statuses, refs = dict(run.node_statuses), dict(run.output_refs or {})
            statuses[node.id], refs[node.id] = "SUCCEEDED", artifact_id
            run.node_statuses, run.output_refs, run.current_node_id = statuses, refs, node.id
            await emit_event(session, run, "NODE_SUCCEEDED", node_id=node.id, attempt_id=attempt_id, status="SUCCEEDED", summary=summary)
            await session.commit()
        return artifact_id

    async def _fail_attempt(self, run_id: str, node: WorkflowNode, attempt_id: str, code: str, message: str) -> None:
        async with self.sessions() as session:
            run = await session.get(WorkflowRun, run_id)
            attempt = await session.get(NodeAttempt, attempt_id)
            attempt.status, attempt.error_code, attempt.error_message, attempt.finished_at = "FAILED", code, message[:500], datetime.now(UTC)
            statuses = dict(run.node_statuses)
            statuses[node.id] = "FAILED"
            run.node_statuses, run.status, run.finished_at = statuses, "FAILED", datetime.now(UTC)
            await emit_event(session, run, "NODE_FAILED", node_id=node.id, attempt_id=attempt_id, status="FAILED", summary=message[:500], payload={"errorCode": code, "errorMessage": message[:500]})
            await session.commit()

    async def _execute_sql(self, node: WorkflowNode, state: RuntimeState, values: dict[str, Any]) -> dict[str, Any]:
        api_key = await self._credential(node, state)
        attempt_id, _ = await self._mark_running(state["run_id"], node)
        sql = str(node.config["sqlTemplate"])
        for item in node.inputs:
            sql = sql.replace("{{" + item.name + "}}", _sql_literal(values[item.name], item.type))
        async with self.sessions() as session:
            run = await session.get(WorkflowRun, state["run_id"])

        async def cancelled() -> bool:
            async with self.sessions() as check:
                current = await check.get(WorkflowRun, state["run_id"])
                return current.status == "CANCEL_REQUESTED"

        try:
            result = await execute_sql_read(database_ref=str(node.config["databaseRef"]), sql=sql, ticket_id=run.ticket_id, api_key=api_key, timeout_seconds=node.timeoutSeconds, cancel_requested=cancelled)
            artifact_id = _id("art_")
            artifact_payload = {"input": {"databaseRef": node.config["databaseRef"], "sql": sql, "parameters": values}, "output": result.payload}
            encoded = json.dumps(artifact_payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            async with self.sessions() as session:
                current = await session.get(WorkflowRun, state["run_id"])
                attempt = await session.get(NodeAttempt, attempt_id)
                session.add(EncryptedArtifact(id=artifact_id, run_id=current.id, node_id=node.id, ciphertext=SecretBox().seal(artifact_payload, purpose="artifact:" + artifact_id), content_hash=sha256_bytes(encoded), size_bytes=len(encoded), expires_at=datetime.now(UTC) + timedelta(days=settings.result_retention_days)))
                attempt.status, attempt.exit_code, attempt.cli_version, attempt.cli_sha256, attempt.artifact_id, attempt.finished_at = "SUCCEEDED", result.exit_code, result.metadata.version, result.metadata.sha256, artifact_id, datetime.now(UTC)
                statuses, refs = dict(current.node_statuses), dict(current.output_refs)
                statuses[node.id], refs[node.id] = "SUCCEEDED", artifact_id
                current.node_statuses, current.output_refs, current.current_node_id = statuses, refs, node.id
                await emit_event(session, current, "NODE_SUCCEEDED", node_id=node.id, attempt_id=attempt_id, status="SUCCEEDED", summary=f"{node.title}执行成功")
                await session.commit()
            return {"inputs": state["inputs"], "output_refs": {**state["output_refs"], node.id: artifact_id}}
        except (CliExecutionError, ValueError, NodeExecutionError) as exc:
            async with self.sessions() as session:
                current = await session.get(WorkflowRun, state["run_id"])
                attempt = await session.get(NodeAttempt, attempt_id)
                code = exc.code if isinstance(exc, CliExecutionError) else "NODE_EXECUTION_ERROR"
                message = str(exc)[:500]
                diagnostic_id = None
                diagnostic_truncated = False
                if isinstance(exc, CliExecutionError) and (exc.stdout or exc.stderr):
                    from app.cli import redact_diagnostic
                    stdout_text, stdout_truncated = redact_diagnostic(exc.stdout, 64 * 1024)
                    stderr_text, stderr_truncated = redact_diagnostic(exc.stderr, 1024 * 1024)
                    diagnostic_truncated = exc.truncated or stdout_truncated or stderr_truncated
                    diagnostic_id = _id("art_")
                    diagnostic_payload = {"stdout": stdout_text, "stderr": stderr_text, "truncated": diagnostic_truncated}
                    encoded = json.dumps(diagnostic_payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                    session.add(EncryptedArtifact(id=diagnostic_id, run_id=current.id, node_id=node.id, artifact_type="DIAGNOSTIC", ciphertext=SecretBox().seal(diagnostic_payload, purpose="artifact:" + diagnostic_id), content_hash=sha256_bytes(encoded), size_bytes=len(encoded), expires_at=datetime.now(UTC) + timedelta(days=settings.result_retention_days)))
                attempt.status, attempt.error_code, attempt.error_message = "FAILED", code, message
                attempt.exit_code, attempt.diagnostic_artifact_id, attempt.diagnostic_truncated, attempt.finished_at = getattr(exc, "exit_code", None), diagnostic_id, diagnostic_truncated, datetime.now(UTC)
                statuses = dict(current.node_statuses)
                statuses[node.id] = "CANCELLED" if code == "CANCELLED" else "FAILED"
                current.node_statuses, current.status = statuses, "CANCELLED" if code == "CANCELLED" else "FAILED"
                current.finished_at = datetime.now(UTC)
                await emit_event(session, current, "NODE_FAILED", node_id=node.id, attempt_id=attempt_id, status=statuses[node.id], summary=message, payload={"errorCode": code, "errorMessage": message, "exitCode": getattr(exc, "exit_code", None), "diagnosticAvailable": diagnostic_id is not None})
                await session.commit()
            raise

    async def _condition(self, node: WorkflowNode, state: RuntimeState, outgoing: list[dict[str, Any]], all_outgoing: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
        document: dict[str, Any] = {"inputs": state["inputs"], "nodes": {}}
        for source_node, artifact_id in state["output_refs"].items():
            artifact = await self._load_artifact(artifact_id, state["run_id"])
            document["nodes"][source_node] = {"output": artifact["output"]}
        selected = None
        default = None
        for edge in outgoing:
            if edge.get("default"):
                default = edge["target"]
            elif edge.get("condition") and evaluate(edge["condition"], document):
                selected = edge["target"]
                break
        selected = selected or default
        if not selected:
            raise NodeExecutionError(f"条件节点 {node.id} 没有可用分支")
        await self._mark_simple_success(state["run_id"], node, f"条件路由到 {selected}")
        def descendants(start: str) -> set[str]:
            found, pending = {start}, [start]
            while pending:
                for edge in all_outgoing.get(pending.pop(), []):
                    if edge["target"] not in found:
                        found.add(edge["target"])
                        pending.append(edge["target"])
            return found
        chosen = descendants(selected)
        skipped: set[str] = set()
        for edge in outgoing:
            if edge["target"] != selected:
                skipped |= descendants(edge["target"]) - chosen
        if skipped:
            async with self.sessions() as session:
                run = await session.get(WorkflowRun, state["run_id"])
                statuses = dict(run.node_statuses)
                for skipped_id in skipped:
                    if statuses.get(skipped_id) == "PENDING":
                        statuses[skipped_id] = "SKIPPED"
                        run.node_statuses = dict(statuses)
                        await emit_event(session, run, "NODE_SKIPPED", node_id=skipped_id, status="SKIPPED", summary=f"条件分支未选择 {skipped_id}")
                run.node_statuses = statuses
                await session.commit()
        return {"routes": {**state["routes"], node.id: selected}}

    def compile(self, definition: WorkflowDefinition):
        builder = StateGraph(RuntimeState)
        edge_dicts = [item.model_dump(mode="json") for item in definition.edges]
        outgoing = {node.id: [edge for edge in edge_dicts if edge["source"] == node.id] for node in definition.nodes}

        for node in definition.nodes:
            async def execute(state: RuntimeState, current=node):
                await self._safe_point(current, state)
                await self._node_approval(current, state)
                values, missing = await self._resolve_inputs(current, state)
                if missing:
                    supplied = await self._missing_inputs(current, state, missing)
                    state = {**state, "inputs": {**state["inputs"], **supplied}}
                    values, missing = await self._resolve_inputs(current, state)
                    if missing:
                        raise NodeExecutionError("人工输入仍不完整")
                handler = self.handlers.get(current.type)
                if handler is None:
                    raise NodeExecutionError(f"未注册节点执行器：{current.type}")
                return await handler(current, state, values, outgoing[current.id], outgoing)
            builder.add_node(node.id, execute)

        builder.add_edge(START, definition.entryNodeId)
        for node in definition.nodes:
            edges = outgoing[node.id]
            if node.type in {"condition", "hitl_select"} and len(edges) > 1:
                targets = {edge["target"]: edge["target"] for edge in edges}
                builder.add_conditional_edges(node.id, lambda state, node_id=node.id: state["routes"][node_id], targets)
            elif edges:
                builder.add_edge(node.id, edges[0]["target"])
            else:
                builder.add_edge(node.id, END)
        return builder.compile(checkpointer=self.checkpointer)
