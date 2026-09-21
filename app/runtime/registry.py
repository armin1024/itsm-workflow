from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from typing import Any, Callable

from jsonschema import Draft202012Validator

from app.runtime.contracts import ExecutionMode, IdempotencyClass, ResumeSemantics


NodeValidator = Callable[[dict[str, Any]], None]


@dataclass(frozen=True)
class NodeManifest:
    type: str
    schema_version: int
    handler_version: str
    name: str
    category: str
    description: str
    risk_level: str
    approval_policy: str
    idempotency_class: IdempotencyClass
    resume_semantics: ResumeSemantics
    supported_modes: tuple[ExecutionMode, ...]
    config_schema: dict[str, Any]
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    ui_schema: dict[str, Any] = field(default_factory=dict)
    result_sensitivity: str = "BUSINESS_DATA"
    allow_single_node_debug: bool = True
    input_types: tuple[str, ...] = ("string", "number", "integer", "boolean", "object", "array")

    def public_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value.update({
            "schemaVersion": value.pop("schema_version"),
            "handlerVersion": value.pop("handler_version"),
            "riskLevel": value.pop("risk_level"),
            "approvalPolicy": value.pop("approval_policy"),
            "idempotencyClass": value.pop("idempotency_class"),
            "resumeSemantics": value.pop("resume_semantics"),
            "supportedModes": value.pop("supported_modes"),
            "configSchema": value.pop("config_schema"),
            "inputSchema": value.pop("input_schema"),
            "outputSchema": value.pop("output_schema"),
            "uiSchema": value.pop("ui_schema"),
            "resultSensitivity": value.pop("result_sensitivity"),
            "allowSingleNodeDebug": value.pop("allow_single_node_debug"),
            "inputTypes": value.pop("input_types"),
        })
        value["idempotencyClass"] = str(value["idempotencyClass"])
        value["resumeSemantics"] = str(value["resumeSemantics"])
        value["supportedModes"] = [str(item) for item in value["supportedModes"]]
        return value


@dataclass(frozen=True)
class NodeRegistration:
    manifest: NodeManifest
    validator: NodeValidator | None = None


class NodeRegistry:
    def __init__(self) -> None:
        self._nodes: dict[tuple[str, int], NodeRegistration] = {}

    def register(self, registration: NodeRegistration) -> None:
        key = (registration.manifest.type, registration.manifest.schema_version)
        if key in self._nodes:
            raise ValueError(f"节点类型已经注册：{key[0]} v{key[1]}")
        Draft202012Validator.check_schema(registration.manifest.config_schema)
        Draft202012Validator.check_schema(registration.manifest.input_schema)
        Draft202012Validator.check_schema(registration.manifest.output_schema)
        self._nodes[key] = registration

    def get(self, node_type: str, schema_version: int = 1) -> NodeRegistration:
        try:
            return self._nodes[(node_type, schema_version)]
        except KeyError as exc:
            raise ValueError(f"不支持的节点类型或版本：{node_type} v{schema_version}") from exc

    def validate_node(self, node: dict[str, Any]) -> NodeManifest:
        manifest = self.get(str(node.get("type") or ""), int(node.get("schemaVersion") or 1)).manifest
        errors = sorted(Draft202012Validator(manifest.config_schema).iter_errors(node.get("config") or {}), key=lambda item: list(item.path))
        if errors:
            path = "/".join(str(item) for item in errors[0].path)
            raise ValueError(f"节点 {node.get('id')} 配置无效 {path}: {errors[0].message}")
        registration = self.get(manifest.type, manifest.schema_version)
        if registration.validator:
            registration.validator(node)
        return manifest

    def catalog(self) -> dict[str, Any]:
        nodes = [registration.manifest.public_dict() for _, registration in sorted(self._nodes.items())]
        encoded = json.dumps(nodes, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        digest = hashlib.sha256(encoded).hexdigest()
        return {"protocolVersion": 1, "catalogVersion": digest[:12], "catalogDigest": digest, "nodes": nodes}


NODE_REGISTRY = NodeRegistry()
