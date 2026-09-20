from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from app.conditions import evaluate, pointer
from app.runtime.executor import execute_single_node
from app.runtime.planner import validate_workflow
from app.workflow import WorkflowDefinition, WorkflowEdge, WorkflowNode


def _resolve_inputs(node: WorkflowNode, run_inputs: dict[str, Any], outputs: dict[str, dict[str, Any]]) -> dict[str, Any]:
    values: dict[str, Any] = {}
    missing: list[str] = []
    for item in node.inputs:
        source = item.source
        try:
            if source.kind == "RUN_INPUT":
                value = run_inputs[str(source.key)]
            elif source.kind == "LITERAL":
                value = source.value
            else:
                value = pointer(outputs[str(source.nodeId)], str(source.jsonPointer))
            values[item.name] = value
        except (KeyError, IndexError, TypeError, ValueError):
            if item.required:
                missing.append(item.name)
    if missing:
        raise ValueError(f"节点 {node.id} 缺少输入：{'、'.join(missing)}")
    return values


def _next_edge(node: WorkflowNode, outgoing: list[WorkflowEdge], state: dict[str, Any]) -> WorkflowEdge | None:
    normal = [edge for edge in outgoing if edge.kind != "REFINEMENT"]
    if not normal:
        return None
    if node.type != "condition":
        return normal[0]
    default = next((edge for edge in normal if edge.default), None)
    for edge in normal:
        if not edge.default and edge.condition and evaluate(edge.condition, state):
            return edge
    return default


async def debug_workflow(*, workflow: dict[str, Any], run_inputs: dict[str, Any], mode: str, simulation: dict[str, Any] | None, ticket_id: int | None, api_key: str | None) -> dict[str, Any]:
    validated = validate_workflow(workflow, "TEST")
    definition = WorkflowDefinition.model_validate(validated["normalizedDefinition"])
    node_map = {node.id: node for node in definition.nodes}
    outgoing: dict[str, list[WorkflowEdge]] = {node.id: [] for node in definition.nodes}
    for edge in definition.edges:
        outgoing[edge.source].append(edge)
    statuses = {node.id: "PENDING" for node in definition.nodes}
    results: list[dict[str, Any]] = []
    outputs: dict[str, dict[str, Any]] = {}
    current = definition.entryNodeId
    last_edge: WorkflowEdge | None = None
    simulation_nodes = (simulation or {}).get("nodes") if isinstance((simulation or {}).get("nodes"), dict) else {}
    started_at = datetime.now(UTC)
    for _ in range(len(definition.nodes) + 1):
        node = node_map[current]
        statuses[node.id] = "RUNNING"
        values = _resolve_inputs(node, run_inputs, outputs)
        if node.type == "condition":
            state = {"inputs": run_inputs, "nodes": {key: {"output": value} for key, value in outputs.items()}}
            edge = _next_edge(node, outgoing[node.id], state)
            output = {"selectedTarget": edge.target if edge else None, "matched": bool(edge and not edge.default)}
            result = {"status": "SUCCEEDED", "output": output, "diagnostic": None, "interrupt": None, "handlerVersion": node.handlerVersion or "1.0.0"}
        else:
            result = await execute_single_node(node=node.model_dump(mode="json"), inputs=values, mode=mode, simulation=dict(simulation_nodes.get(node.id) or {}), api_key=api_key, ticket_id=ticket_id)
            edge = _next_edge(node, outgoing[node.id], {"inputs": run_inputs, "nodes": {key: {"output": value} for key, value in outputs.items()}})
        statuses[node.id] = str(result["status"])
        if isinstance(result.get("output"), dict):
            outputs[node.id] = result["output"]
        results.append({"nodeId": node.id, "type": node.type, "title": node.title, "inputs": values, **result})
        if result["status"] != "SUCCEEDED":
            break
        last_edge = edge
        if edge is None:
            break
        current = edge.target
    else:
        raise ValueError("工作流调试超过节点上限，可能存在非法循环")
    for node_id, status in list(statuses.items()):
        if status == "PENDING":
            statuses[node_id] = "SKIPPED"
    final = "SUCCEEDED" if results and results[-1]["status"] == "SUCCEEDED" and last_edge is None else str(results[-1]["status"] if results else "FAILED")
    return {
        "debugRunId": "test_flow_" + uuid.uuid4().hex,
        "mode": mode,
        "status": final,
        "catalogDigest": validated["catalogDigest"],
        "workflowContentHash": validated["workflowContentHash"],
        "nodeStatuses": statuses,
        "nodeResults": results,
        "startedAt": started_at.isoformat(),
        "finishedAt": datetime.now(UTC).isoformat(),
        "testOnly": True,
    }
