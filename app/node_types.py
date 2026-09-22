from __future__ import annotations

from dataclasses import dataclass
from typing import Any


HITL_SELECT_CONFIG = {
    "type": "object", "required": ["title", "selectionMode", "idPath", "labelTemplate", "displayFields", "outputFields"], "additionalProperties": False,
    "properties": {
        "title": {"type": "string", "minLength": 1}, "selectionMode": {"enum": ["SINGLE", "MULTIPLE"]}, "idPath": {"type": "string", "pattern": "^/"}, "labelTemplate": {"type": "string", "minLength": 1},
        "displayFields": {"type": "array", "minItems": 1, "items": {"type": "object", "required": ["name", "label", "path"], "properties": {"name": {"type": "string"}, "label": {"type": "string"}, "path": {"type": "string", "pattern": "^/"}}}},
        "outputFields": {"type": "array", "minItems": 1, "items": {"type": "object", "required": ["name", "path"], "properties": {"name": {"type": "string"}, "path": {"type": "string", "pattern": "^/"}}}},
        "minimumSelections": {"type": "integer", "minimum": 1}, "maximumSelections": {"type": "integer", "minimum": 1, "maximum": 100},
    },
}
HITL_FORM_CONFIG = {
    "type": "object", "required": ["title", "fields"], "additionalProperties": False,
    "properties": {"title": {"type": "string", "minLength": 1}, "description": {"type": "string"}, "fields": {"type": "array", "minItems": 1, "maxItems": 50, "items": {"type": "object", "required": ["name", "label", "type", "required"], "properties": {"name": {"type": "string"}, "label": {"type": "string"}, "type": {"enum": ["string", "integer", "number", "boolean"]}, "required": {"type": "boolean"}, "description": {"type": "string"}, "minimum": {"type": "number"}, "maximum": {"type": "number"}, "minLength": {"type": "integer"}, "maxLength": {"type": "integer"}, "enum": {"type": "array"}}}}},
}


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
    "hitl_select": NodeTypeManifest("hitl_select", 1, "LOW", HITL_SELECT_CONFIG, ("array",), {"type": "object", "required": ["selected"], "properties": {"selected": {"type": "array"}}}),
    "hitl_form": NodeTypeManifest("hitl_form", 1, "LOW", HITL_FORM_CONFIG, (), {"type": "object", "required": ["values"], "properties": {"values": {"type": "object"}}}),
    "human_input": NodeTypeManifest("human_input", 1, "LOW", {}, ("string", "number", "integer", "boolean", "object", "array"), {"type": "object"}),
    "approval": NodeTypeManifest("approval", 1, "HIGH", {}, (), {"type": "object"}),
    "end": NodeTypeManifest("end", 1, "LOW", {}, (), {"type": "object"}),
}


def register_node_type(manifest: NodeTypeManifest) -> None:
    if manifest.type in NODE_TYPES:
        raise ValueError(f"节点类型已经注册：{manifest.type}")
    NODE_TYPES[manifest.type] = manifest
