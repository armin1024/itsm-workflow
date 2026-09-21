from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Awaitable, Callable

from jsonschema import Draft202012Validator

from app.cli import CliExecutionError, execute_sql_read, redact_diagnostic
from app.conditions import evaluate, pointer
from app.runtime import NODE_REGISTRY
from app.runtime.contracts import ExecutionMode


PLACEHOLDER = re.compile(r"\{\{([A-Za-z_][A-Za-z0-9_]*)\}\}")


def _sql_literal(value: Any, value_type: str) -> str:
    if value_type == "string": return "'" + str(value).replace("'", "''") + "'"
    if value_type == "integer":
        if isinstance(value, bool): raise ValueError("布尔值不能作为整数")
        return str(int(value))
    if value_type == "number":
        if isinstance(value, bool): raise ValueError("布尔值不能作为数字")
        return str(float(value))
    if value_type == "boolean": return "1" if bool(value) else "0"
    raise ValueError(f"SQL参数不支持类型 {value_type}")


def render_sql(node: dict[str, Any], inputs: dict[str, Any]) -> str:
    definitions = {str(item.get("name")): item for item in node.get("inputs", []) if isinstance(item, dict)}
    template = str((node.get("config") or {}).get("sqlTemplate") or "")
    missing = [name for name in PLACEHOLDER.findall(template) if name not in inputs]
    if missing: raise ValueError("缺少SQL输入参数：" + "、".join(dict.fromkeys(missing)))
    return PLACEHOLDER.sub(lambda match: _sql_literal(inputs[match.group(1)], str(definitions[match.group(1)].get("type") or "string")), template)


def _validate_output(manifest, output: dict[str, Any]) -> None:
    errors = sorted(Draft202012Validator(manifest.output_schema).iter_errors(output), key=lambda item: list(item.path))
    if errors: raise ValueError(f"节点输出不符合Schema：{errors[0].message}")


def _candidate_id(node_id: str, raw_id: Any, index: int) -> str:
    digest = hashlib.sha256(json.dumps([node_id, raw_id, index], ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()[:20]
    return "candidate_" + digest


def build_candidates(node: dict[str, Any], rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    config = node.get("config") or {}
    candidates: list[dict[str, Any]] = []
    for index, row in enumerate(rows[:1000]):
        try:
            raw_id = pointer(row, str(config["idPath"]))
            display = {str(field["name"]): pointer(row, str(field["path"])) for field in config["displayFields"]}
            values = {str(field["name"]): pointer(row, str(field["path"])) for field in config["outputFields"]}
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise ValueError(f"第{index + 1}行无法按HITL映射配置提取字段") from exc
        label = str(config["labelTemplate"])
        for name, value in display.items(): label = label.replace("{{" + name + "}}", str(value))
        candidates.append({"candidateId": _candidate_id(str(node.get("id")), raw_id, index), "label": label, "display": display, "values": values})
    return candidates


def form_schema(fields: list[dict[str, Any]]) -> dict[str, Any]:
    properties: dict[str, Any] = {}
    required: list[str] = []
    for field in fields:
        name, kind = str(field["name"]), str(field["type"])
        schema: dict[str, Any] = {"type": kind}
        for key in ("minimum", "maximum", "minLength", "maxLength", "enum"):
            if key in field: schema[key] = field[key]
        properties[name] = schema
        if field.get("required"): required.append(name)
    return {"type": "object", "properties": properties, "required": required, "additionalProperties": False}


async def execute_single_node(*, node: dict[str, Any], inputs: dict[str, Any], mode: str, simulation: dict[str, Any] | None = None, api_key: str | None = None, ticket_id: int | None = None, resume_payload: dict[str, Any] | None = None, cancel_requested: Callable[[], Awaitable[bool]] | None = None) -> dict[str, Any]:
    """Execute one registered node. Credentials never enter output or storage."""
    execution_mode = ExecutionMode(mode)
    manifest = NODE_REGISTRY.validate_node(node)
    if execution_mode not in manifest.supported_modes or not manifest.allow_single_node_debug:
        raise ValueError(f"节点 {manifest.type} 不允许 {execution_mode.value} 执行")
    simulation = simulation or {}
    if execution_mode == ExecutionMode.DRY_RUN:
        return {"status": "SUCCEEDED", "output": {"plan": {"type": manifest.type, "title": node.get("title"), "config": node.get("config"), "inputs": inputs}}, "diagnostic": None, "interrupt": None, "handlerVersion": manifest.handler_version}

    output: dict[str, Any] | None = None
    if execution_mode == ExecutionMode.SIMULATION and manifest.type == "sql_read":
        output = simulation.get("fixtureOutput")
        if not isinstance(output, dict): raise ValueError("sql_read模拟需要simulation.fixtureOutput对象")
    elif manifest.type == "sql_read":
        if not api_key: raise ValueError("SQL读执行必须提供AOPS_API_KEY")
        if not ticket_id: raise ValueError("SQL读执行必须提供正整数工单ID")
        try:
            result = await execute_sql_read(database_ref=str(node["config"]["databaseRef"]), sql=render_sql(node, inputs), ticket_id=ticket_id, api_key=api_key, timeout_seconds=int(node.get("timeoutSeconds") or 600), cancel_requested=cancel_requested)
        except CliExecutionError as exc:
            stdout, stdout_cut = redact_diagnostic(exc.stdout, 32 * 1024); stderr, stderr_cut = redact_diagnostic(exc.stderr, 16 * 1024)
            status = "CANCELLED" if exc.code == "CANCELLED" else "FAILED"
            return {"status": status, "output": None, "diagnostic": {"errorCode": exc.code, "message": str(exc), "exitCode": exc.exit_code, "stdout": stdout, "stderr": stderr, "truncated": exc.truncated or stdout_cut or stderr_cut}, "interrupt": None, "handlerVersion": manifest.handler_version}
        output = {key: result.payload[key] for key in ("status", "data", "rowCount") if key in result.payload}
    elif manifest.type == "hitl_select":
        rows = inputs.get("rows")
        if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows): raise ValueError("hitl_select输入缺少rows对象数组")
        candidates = build_candidates(node, rows)
        if not candidates: raise ValueError("没有可供选择的候选")
        if resume_payload and resume_payload.get("nodeId") == node.get("id") and resume_payload.get("action") == "SELECT":
            selected_ids = resume_payload.get("candidateIds") or []
            selected = [item for item in candidates if item["candidateId"] in selected_ids]
            minimum = int((node.get("config") or {}).get("minimumSelections") or 1)
            maximum = int((node.get("config") or {}).get("maximumSelections") or (1 if (node.get("config") or {}).get("selectionMode") == "SINGLE" else 100))
            if len(selected) != len(selected_ids) or not minimum <= len(selected) <= maximum: raise ValueError("HITL候选选择无效")
            output = {"selected": [{"candidateId": item["candidateId"], "values": item["values"]} for item in selected]}
        else:
            return {"status": "WAITING_INPUT", "output": None, "diagnostic": None, "interrupt": {"kind": "HITL_SELECT", "title": (node.get("config") or {}).get("title"), "selectionMode": (node.get("config") or {}).get("selectionMode"), "candidates": candidates}, "handlerVersion": manifest.handler_version}
    elif manifest.type == "hitl_form":
        fields = list((node.get("config") or {}).get("fields") or [])
        if resume_payload and resume_payload.get("nodeId") == node.get("id") and resume_payload.get("action") == "SUBMIT":
            values = resume_payload.get("values")
            errors = sorted(Draft202012Validator(form_schema(fields)).iter_errors(values), key=lambda item: list(item.path))
            if errors: raise ValueError(f"HITL表单输入无效：{errors[0].message}")
            output = {"values": values}
        else:
            return {"status": "WAITING_INPUT", "output": None, "diagnostic": None, "interrupt": {"kind": "HITL_FORM", "title": (node.get("config") or {}).get("title"), "description": (node.get("config") or {}).get("description", ""), "fields": fields, "context": inputs}, "handlerVersion": manifest.handler_version}
    elif manifest.type == "condition":
        rule = simulation.get("rule") or (node.get("config") or {}).get("rule")
        if not isinstance(rule, dict): raise ValueError("condition节点需要config.rule")
        output = {"matched": evaluate(rule, inputs)}
    else:
        output = {"ok": True}

    if not isinstance(output, dict): raise ValueError("节点没有生成JSON对象输出")
    _validate_output(manifest, output)
    return {"status": "SUCCEEDED", "output": output, "diagnostic": None, "interrupt": None, "handlerVersion": manifest.handler_version}
