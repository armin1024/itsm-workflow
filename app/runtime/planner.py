from __future__ import annotations

import hashlib
import json
from typing import Any

from app.runtime import NODE_REGISTRY
from app.workflow import WorkflowDefinition


def canonical_hash(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def normalize_workflow(value: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    definition = WorkflowDefinition.model_validate(value)
    normalized = definition.model_dump(mode="json")
    normalized["schemaVersion"] = 2
    for node in normalized["nodes"]:
        manifest = NODE_REGISTRY.get(node["type"], int(node.get("schemaVersion") or 1)).manifest
        node["schemaVersion"] = manifest.schema_version
        node["handlerVersion"] = node.get("handlerVersion") or manifest.handler_version
    catalog = NODE_REGISTRY.catalog()
    normalized["catalogDigest"] = catalog["catalogDigest"]
    return normalized, catalog


def validate_workflow(value: dict[str, Any], mode: str = "DRAFT") -> dict[str, Any]:
    normalized, catalog = normalize_workflow(value)
    warnings: list[str] = []
    if mode == "PUBLISH":
        for node in normalized["nodes"]:
            manifest = NODE_REGISTRY.get(node["type"], node["schemaVersion"]).manifest
            if node["handlerVersion"] != manifest.handler_version:
                warnings.append(f"节点 {node['id']} 固定Handler {node['handlerVersion']}，当前实现为 {manifest.handler_version}")
    return {
        "valid": True,
        "normalizedDefinition": normalized,
        "catalogDigest": catalog["catalogDigest"],
        "workflowContentHash": canonical_hash(normalized),
        "requiredRuntimeVersion": ">=0.9.0,<1.0.0",
        "warnings": warnings,
    }


def render_plan(*, workflow_version_id: str, workflow_content_hash: str, workflow_snapshot: dict[str, Any], ticket_id: int, parameters: dict[str, Any], actor_uid: str) -> dict[str, Any]:
    validated = validate_workflow(workflow_snapshot, "EXECUTE")
    normalized = validated["normalizedDefinition"]
    required: dict[str, dict[str, Any]] = {}
    nodes = []
    risks = []
    for node in normalized["nodes"]:
        manifest = NODE_REGISTRY.get(node["type"], node["schemaVersion"]).manifest
        for item in node.get("inputs", []):
            source = item.get("source") or {}
            if source.get("kind") == "RUN_INPUT" and source.get("key"):
                key = str(source["key"])
                required.setdefault(key, {"key": key, "type": item.get("type", "string"), "description": item.get("description", ""), "required": item.get("required", True), "usedBy": []})["usedBy"].append(node["id"])
        if manifest.risk_level != "LOW" or node.get("approvalPolicy") == "NODE":
            risks.append({"nodeId": node["id"], "riskLevel": manifest.risk_level, "approvalPolicy": node.get("approvalPolicy")})
        nodes.append({
            "nodeId": node["id"], "type": node["type"], "title": node["title"], "schemaVersion": node["schemaVersion"],
            "handlerVersion": node["handlerVersion"], "approvalPolicy": node.get("approvalPolicy"), "databaseRef": (node.get("config") or {}).get("databaseRef"),
            "sqlTemplate": (node.get("config") or {}).get("sqlTemplate"), "inputs": node.get("inputs", []),
        })
    missing = [item["key"] for item in required.values() if item["required"] and item["key"] not in parameters]
    material = {
        "workflowVersionId": workflow_version_id, "workflowContentHash": workflow_content_hash, "ticketId": ticket_id,
        "actorUid": actor_uid, "parameters": parameters, "nodes": nodes, "edges": normalized["edges"], "requiredInputs": list(required.values()), "riskSummary": risks,
    }
    return {
        "valid": not missing,
        "missingInputs": missing,
        "renderedPlan": {"nodes": nodes, "edges": normalized["edges"], "riskSummary": risks, "requiredInputs": list(required.values())},
        "planMaterialHash": canonical_hash(material),
        "catalogDigest": validated["catalogDigest"],
        "runtimeVersion": "0.9.0",
    }
