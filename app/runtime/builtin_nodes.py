from __future__ import annotations

import re
from typing import Any

from sqlglot import exp, parse, parse_one

from app.runtime.contracts import ExecutionMode, IdempotencyClass, ResumeSemantics
from app.runtime.registry import NODE_REGISTRY, NodeManifest, NodeRegistration


PLACEHOLDER = re.compile(r"\{\{([A-Za-z_][A-Za-z0-9_]*)\}\}")
READ_ROOTS = (exp.Select, exp.Show, exp.Describe, exp.Union)
FORBIDDEN_NODES = (exp.Insert, exp.Update, exp.Delete, exp.Create, exp.Drop, exp.Alter)
ALL_MODES = tuple(ExecutionMode)


def _object_schema(properties: dict[str, Any] | None = None, required: list[str] | None = None, additional: bool = False) -> dict[str, Any]:
    return {"type": "object", "properties": properties or {}, "required": required or [], "additionalProperties": additional}


def _sql_validator(node: dict[str, Any]) -> None:
    config = node.get("config") or {}
    database = str(config.get("databaseRef") or "").strip()
    sql = str(config.get("sqlTemplate") or "").strip()
    if not database or len(database) > 1000:
        raise ValueError(f"节点 {node.get('id')} 数据库路径无效")
    names = {str(item.get("name")) for item in node.get("inputs", [])}
    if set(PLACEHOLDER.findall(sql)) != names:
        raise ValueError(f"节点 {node.get('id')} SQL占位符必须与inputs完全一致")
    rendered = PLACEHOLDER.sub("0", sql)
    if len(parse(rendered, read="mysql")) != 1:
        raise ValueError(f"节点 {node.get('id')} 只能包含一条SQL")
    expression = parse_one(rendered, read="mysql")
    if not isinstance(expression, READ_ROOTS) or any(expression.find_all(FORBIDDEN_NODES)):
        raise ValueError(f"节点 {node.get('id')} 不是只读SQL")


def register_builtin_nodes() -> None:
    if NODE_REGISTRY._nodes:
        return
    NODE_REGISTRY.register(NodeRegistration(NodeManifest(
        type="sql_read", schema_version=1, handler_version="1.0.0", name="SQL只读查询", category="data",
        description="使用aops-cli执行单条安全只读SQL", risk_level="LOW", approval_policy="PLAN",
        idempotency_class=IdempotencyClass.READ_SAFE, resume_semantics=ResumeSemantics.UNKNOWN_REQUIRES_OPERATOR,
        supported_modes=ALL_MODES,
        config_schema=_object_schema({"databaseRef": {"type": "string", "minLength": 1, "maxLength": 1000}, "sqlTemplate": {"type": "string", "minLength": 1}}, ["databaseRef", "sqlTemplate"]),
        input_schema=_object_schema(additional=True),
        output_schema=_object_schema({"status": {"type": "integer"}, "data": {"type": "array"}, "rowCount": {"type": "integer"}}, ["status", "data", "rowCount"]),
        ui_schema={"icon": "database", "editor": "sql-read", "debugConfig": {"databaseRef": "test/test/test/readonly/service", "sqlTemplate": "SELECT 1 AS value"}, "debugInputs": {}},
        input_types=("string", "number", "integer", "boolean"),
    ), _sql_validator))
    NODE_REGISTRY.register(NodeRegistration(NodeManifest(
        type="condition", schema_version=1, handler_version="1.0.0", name="条件判断", category="control",
        description="根据前置结果选择分支", risk_level="LOW", approval_policy="NONE",
        idempotency_class=IdempotencyClass.PURE, resume_semantics=ResumeSemantics.SAFE_RETRY, supported_modes=ALL_MODES,
        config_schema=_object_schema(additional=True), input_schema=_object_schema(additional=True), output_schema=_object_schema({"selectedTarget": {"type": ["string", "null"]}, "matched": {"type": "boolean"}}, ["matched"], True),
        ui_schema={"icon": "branch", "editor": "condition", "debugConfig": {"rule": {"path": "/value", "op": "exists"}}, "debugInputs": {"value": "sample"}},
    )))
    display_field = _object_schema({"name": {"type": "string"}, "label": {"type": "string"}, "path": {"type": "string", "pattern": "^/"}}, ["name", "label", "path"])
    output_field = _object_schema({"name": {"type": "string"}, "path": {"type": "string", "pattern": "^/"}}, ["name", "path"])
    NODE_REGISTRY.register(NodeRegistration(NodeManifest(
        type="hitl_select", schema_version=1, handler_version="2.0.0", name="人工候选选择", category="human",
        description="将前置SQL行映射为候选并等待用户选择", risk_level="LOW", approval_policy="NONE",
        idempotency_class=IdempotencyClass.PURE, resume_semantics=ResumeSemantics.WAIT_FOR_INPUT, supported_modes=ALL_MODES,
        config_schema=_object_schema({
            "title": {"type": "string", "minLength": 1}, "selectionMode": {"enum": ["SINGLE", "MULTIPLE"]},
            "idPath": {"type": "string", "pattern": "^/"}, "labelTemplate": {"type": "string", "minLength": 1},
            "displayFields": {"type": "array", "items": display_field, "minItems": 1},
            "outputFields": {"type": "array", "items": output_field, "minItems": 1},
            "minimumSelections": {"type": "integer", "minimum": 1}, "maximumSelections": {"type": "integer", "minimum": 1, "maximum": 100},
        }, ["title", "selectionMode", "idPath", "labelTemplate", "displayFields", "outputFields"]),
        input_schema=_object_schema({"rows": {"type": "array", "items": {"type": "object"}}}, ["rows"]),
        output_schema=_object_schema({"selected": {"type": "array", "items": _object_schema({"candidateId": {"type": "string"}, "values": {"type": "object"}}, ["candidateId", "values"], True)}}, ["selected"]),
        ui_schema={"icon": "user-choice", "editor": "hitl-select", "debugConfig": {"title": "请选择客户", "selectionMode": "SINGLE", "idPath": "/customer_id", "labelTemplate": "{{customer_name}} / {{customer_id}}", "displayFields": [{"name": "customer_name", "label": "客户姓名", "path": "/customer_name"}, {"name": "customer_id", "label": "客户编号", "path": "/customer_id"}], "outputFields": [{"name": "customer_id", "path": "/customer_id"}]}, "debugInputs": {"rows": [{"customer_id": "C001", "customer_name": "王五"}, {"customer_id": "C002", "customer_name": "王五"}]}},
    )))
    form_field = _object_schema({"name": {"type": "string", "pattern": "^[A-Za-z_][A-Za-z0-9_]*$"}, "label": {"type": "string"}, "type": {"enum": ["string", "integer", "number", "boolean"]}, "required": {"type": "boolean"}, "description": {"type": "string"}, "minimum": {"type": "number"}, "maximum": {"type": "number"}, "minLength": {"type": "integer", "minimum": 0}, "maxLength": {"type": "integer", "minimum": 1}, "enum": {"type": "array"}}, ["name", "label", "type", "required"], True)
    NODE_REGISTRY.register(NodeRegistration(NodeManifest(
        type="hitl_form", schema_version=1, handler_version="1.0.0", name="人工参数输入", category="human",
        description="等待用户填写一个或多个结构化参数", risk_level="LOW", approval_policy="NONE",
        idempotency_class=IdempotencyClass.PURE, resume_semantics=ResumeSemantics.WAIT_FOR_INPUT, supported_modes=ALL_MODES,
        config_schema=_object_schema({"title": {"type": "string", "minLength": 1}, "description": {"type": "string"}, "fields": {"type": "array", "items": form_field, "minItems": 1, "maxItems": 50}}, ["title", "fields"]),
        input_schema=_object_schema(additional=True), output_schema=_object_schema({"values": {"type": "object"}}, ["values"]),
        ui_schema={"icon": "form", "editor": "hitl-form", "debugConfig": {"title": "填写查询参数", "description": "请确认后续查询参数", "fields": [{"name": "customer_id", "label": "客户编号", "type": "string", "required": True}]}, "debugInputs": {}},
    )))
    NODE_REGISTRY.register(NodeRegistration(NodeManifest(
        type="end", schema_version=1, handler_version="1.0.0", name="结束", category="control", description="汇总并结束运行",
        risk_level="LOW", approval_policy="NONE", idempotency_class=IdempotencyClass.PURE, resume_semantics=ResumeSemantics.SAFE_RETRY,
        supported_modes=ALL_MODES, config_schema=_object_schema(additional=True), input_schema=_object_schema(additional=True), output_schema=_object_schema(additional=True),
        ui_schema={"icon": "end", "editor": "end", "debugConfig": {}, "debugInputs": {}},
    )))


register_builtin_nodes()
