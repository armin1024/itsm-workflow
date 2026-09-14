from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class NodeTypeManifest:
    type: str
    schema_version: int
    risk_level: str
    config_schema: dict[str, Any]
    input_types: tuple[str, ...]
    output_schema: dict[str, Any]


NODE_TYPES: dict[str, NodeTypeManifest] = {
    "sql_read": NodeTypeManifest("sql_read", 1, "LOW", {"required": ["databaseRef", "sqlTemplate"]}, ("string", "number", "integer", "boolean"), {"type": "object"}),
    "condition": NodeTypeManifest("condition", 1, "LOW", {}, (), {"type": "object"}),
    "human_input": NodeTypeManifest("human_input", 1, "LOW", {}, ("string", "number", "integer", "boolean", "object", "array"), {"type": "object"}),
    "approval": NodeTypeManifest("approval", 1, "HIGH", {}, (), {"type": "object"}),
    "end": NodeTypeManifest("end", 1, "LOW", {}, (), {"type": "object"}),
}


def register_node_type(manifest: NodeTypeManifest) -> None:
    if manifest.type in NODE_TYPES:
        raise ValueError(f"节点类型已经注册：{manifest.type}")
    NODE_TYPES[manifest.type] = manifest
