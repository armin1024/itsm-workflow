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
    if NODE_REGISTRY._nodes:  # idempotent module import
        return
    NODE_REGISTRY.register(NodeRegistration(NodeManifest(
        type="sql_read", schema_version=1, handler_version="1.0.0", name="SQL只读查询", category="data",
        description="使用aops-cli执行单条安全只读SQL", risk_level="LOW", approval_policy="PLAN",
        idempotency_class=IdempotencyClass.READ_SAFE, resume_semantics=ResumeSemantics.UNKNOWN_REQUIRES_OPERATOR,
        supported_modes=ALL_MODES,
        config_schema=_object_schema({"databaseRef": {"type": "string", "minLength": 1, "maxLength": 1000}, "sqlTemplate": {"type": "string", "minLength": 1}}, ["databaseRef", "sqlTemplate"]),
        input_schema=_object_schema(additional=True),
        output_schema=_object_schema({"status": {"type": "integer"}, "data": {"type": "array"}, "rowCount": {"type": "integer"}}, ["status", "data"]),
        ui_schema={"icon": "database", "editor": "sql-read", "debugConfig": {"databaseRef": "test/test/test/readonly/service", "sqlTemplate": "SELECT 1 AS value"}, "debugInputs": {}, "debugFixture": {"status": 0, "data": [{"value": 1}], "rowCount": 1}}, input_types=("string", "number", "integer", "boolean"),
    ), _sql_validator))
    NODE_REGISTRY.register(NodeRegistration(NodeManifest(
        type="condition", schema_version=1, handler_version="1.0.0", name="条件判断", category="control",
        description="根据JSON Pointer和受限运算符选择分支", risk_level="LOW", approval_policy="NONE",
        idempotency_class=IdempotencyClass.PURE, resume_semantics=ResumeSemantics.SAFE_RETRY, supported_modes=ALL_MODES,
        config_schema=_object_schema(), input_schema=_object_schema(), output_schema=_object_schema({"selectedTarget": {"type": "string"}, "matched": {"type": "boolean"}}, [], True),
        ui_schema={"icon": "branch", "editor": "condition", "debugConfig": {}, "debugInputs": {"value": "sample"}, "debugRule": {"path": "/value", "op": "exists"}},
    )))
    candidate_schema = {"type": "array", "items": _object_schema({"id": {"type": "string"}, "label": {"type": "string"}, "value": {}, "reason": {"type": "string"}}, ["id", "label", "value"], True)}
    NODE_REGISTRY.register(NodeRegistration(NodeManifest(
        type="llm_extract", schema_version=1, handler_version="1.0.0", name="LLM结构化提取", category="transform",
        description="将前置结果转换为符合Schema的候选参数", risk_level="LOW", approval_policy="PLAN",
        idempotency_class=IdempotencyClass.REPLAY_WITH_STORED_RESULT, resume_semantics=ResumeSemantics.CHECK_RESULT_THEN_RETRY, supported_modes=ALL_MODES,
        config_schema=_object_schema({"modelProfile": {"type": "string", "minLength": 1}, "promptTemplateId": {"type": "string", "minLength": 1}, "responseSchema": {"type": "object"}, "maxInputRows": {"type": "integer", "minimum": 1, "maximum": 1000}, "temperature": {"type": "number", "minimum": 0, "maximum": 1}, "dataPolicy": {"type": "string"}}, ["modelProfile", "promptTemplateId", "responseSchema"]),
        input_schema=_object_schema(additional=True), output_schema=_object_schema({"candidates": candidate_schema}, ["candidates"]),
        ui_schema={"icon": "model", "editor": "llm-extract", "debugConfig": {"modelProfile": "fixture-model", "promptTemplateId": "fixture-prompt", "responseSchema": {"type": "object"}}, "debugInputs": {"rows": [{"customer_id": "C000244"}]}, "debugFixture": {"candidates": [{"id": "candidate-1", "label": "客户 C000244", "value": "C000244", "reason": "测试候选"}]}},
    )))
    NODE_REGISTRY.register(NodeRegistration(NodeManifest(
        type="hitl_select", schema_version=1, handler_version="1.0.0", name="人工候选选择", category="human",
        description="单候选自动通过，多候选持久化等待用户选择", risk_level="LOW", approval_policy="NONE",
        idempotency_class=IdempotencyClass.PURE, resume_semantics=ResumeSemantics.WAIT_FOR_INPUT, supported_modes=ALL_MODES,
        config_schema=_object_schema({"selectionMode": {"enum": ["SINGLE"]}, "autoSelectSingle": {"type": "boolean"}, "zeroCandidatePolicy": {"enum": ["REQUEST_MANUAL_INPUT", "FAIL"]}, "title": {"type": "string"}, "valueSchema": {"type": "object"}, "refinement": {"type": "object"}}, ["selectionMode", "autoSelectSingle", "zeroCandidatePolicy", "title"]),
        input_schema=_object_schema({"candidates": candidate_schema}, ["candidates"]),
        output_schema=_object_schema({"selected": _object_schema({"id": {"type": "string"}, "value": {}}, ["id", "value"], True)}, ["selected"]),
        ui_schema={"icon": "user-choice", "editor": "hitl-select", "debugConfig": {"selectionMode": "SINGLE", "autoSelectSingle": True, "zeroCandidatePolicy": "REQUEST_MANUAL_INPUT", "title": "请选择测试候选"}, "debugInputs": {"candidates": [{"id": "candidate-1", "label": "候选一", "value": "one"}, {"id": "candidate-2", "label": "候选二", "value": "two"}]}},
    )))
    for node_type, name, category, risk, approval, resume in (
        ("human_input", "人工输入", "human", "LOW", "NONE", ResumeSemantics.WAIT_FOR_INPUT),
        ("approval", "人工审批", "human", "HIGH", "NODE", ResumeSemantics.WAIT_FOR_INPUT),
        ("end", "结束", "control", "LOW", "NONE", ResumeSemantics.SAFE_RETRY),
    ):
        NODE_REGISTRY.register(NodeRegistration(NodeManifest(
            type=node_type, schema_version=1, handler_version="1.0.0", name=name, category=category,
            description=name, risk_level=risk, approval_policy=approval, idempotency_class=IdempotencyClass.PURE,
            resume_semantics=resume, supported_modes=ALL_MODES, config_schema=_object_schema(additional=True),
            input_schema=_object_schema(additional=True), output_schema=_object_schema(additional=True),
            ui_schema={"icon": node_type, "editor": node_type},
        )))


register_builtin_nodes()
