from __future__ import annotations

import math
import re
from typing import Any, Awaitable, Callable

from jsonschema import Draft202012Validator

from app.cli import CliExecutionError, execute_sql_read, redact_diagnostic
from app.conditions import evaluate
from app.runtime import NODE_REGISTRY
from app.runtime.contracts import ExecutionMode
from app.resources import cpu_run


PLACEHOLDER = re.compile(r"\{\{([A-Za-z_][A-Za-z0-9_]*)\}\}")


def _sql_literal(value: Any, value_type: str) -> str:
    if value_type == "string": return "'" + str(value).replace("'", "''") + "'"
    if value_type == "integer":
        if isinstance(value, bool): raise ValueError("布尔值不能作为整数")
        return str(int(value))
    if value_type == "number":
        if isinstance(value, bool): raise ValueError("布尔值不能作为数字")
        number = float(value)
        if not math.isfinite(number): raise ValueError("SQL数字参数必须是有限数值")
        return str(number)
    if value_type == "boolean":
        if not isinstance(value, bool): raise ValueError("SQL布尔参数必须是true或false")
        return "1" if value else "0"
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


def form_schema(fields: list[dict[str, Any]]) -> dict[str, Any]:
    properties: dict[str, Any] = {}
    required: list[str] = []
    for field in fields:
        name, kind = str(field["name"]), str(field["type"])
        if name in properties: raise ValueError(f"HITL表单字段名称重复：{name}")
        schema: dict[str, Any] = {"type": kind}
        for key in ("minimum", "maximum", "minLength", "maxLength", "enum"):
            if key in field: schema[key] = field[key]
        properties[name] = schema
        if field.get("required"): required.append(name)
    return {"type": "object", "properties": properties, "required": required, "additionalProperties": False}


def _validate_inputs(node: dict[str, Any], inputs: dict[str, Any]):
    manifest = NODE_REGISTRY.validate_node(node)
    if node.get("handlerVersion") and node["handlerVersion"] != manifest.handler_version:
        raise ValueError(f"节点 {node.get('id')} 的Handler版本不可用：{node['handlerVersion']}")
    input_errors = list(Draft202012Validator(manifest.input_schema).iter_errors(inputs))
    for item in node.get("inputs", []):
        if item.get("name") in inputs:
            input_errors.extend(Draft202012Validator({"type": item.get("type", "string")}).iter_errors(inputs[item["name"]]))
        elif item.get("required", True):
            raise ValueError(f"缺少节点输入：{item['name']}")
    if input_errors: raise ValueError(f"节点输入不符合Schema：{input_errors[0].message}")
    return manifest


def _cli_diagnostic(exc: CliExecutionError) -> dict[str, Any]:
    stdout, stdout_cut = redact_diagnostic(exc.stdout, 32 * 1024)
    stderr, stderr_cut = redact_diagnostic(exc.stderr, 16 * 1024)
    return {"errorCode": exc.code, "message": str(exc), "exitCode": exc.exit_code, "stdout": stdout, "stderr": stderr, "truncated": exc.truncated or stdout_cut or stderr_cut}


async def execute_single_node(*, node: dict[str, Any], inputs: dict[str, Any], mode: str, simulation: dict[str, Any] | None = None, api_key: str | None = None, ticket_id: int | None = None, resume_payload: dict[str, Any] | None = None, cancel_requested: Callable[[], Awaitable[bool]] | None = None) -> dict[str, Any]:
    """Execute one registered node. Credentials never enter output or storage."""
    execution_mode = ExecutionMode(mode)
    manifest = await cpu_run(_validate_inputs, node, inputs)
    if execution_mode not in manifest.supported_modes:
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
            sql = await cpu_run(render_sql, node, inputs)
            result = await execute_sql_read(database_ref=str(node["config"]["databaseRef"]), sql=sql, ticket_id=ticket_id, api_key=api_key, timeout_seconds=int(node.get("timeoutSeconds") or 600), cancel_requested=cancel_requested)
        except CliExecutionError as exc:
            status = "TERMINATED" if exc.code == "CANCELLED" else "FAILED"
            return {"status": status, "output": None, "diagnostic": await cpu_run(_cli_diagnostic, exc), "interrupt": None, "handlerVersion": manifest.handler_version}
        output = {key: result.payload[key] for key in ("status", "data", "rowCount") if key in result.payload}
        if isinstance(output.get("data"), list): output.setdefault("rowCount", len(output["data"]))
    elif manifest.type == "hitl_form":
        fields = list((node.get("config") or {}).get("fields") or [])
        schema = form_schema(fields)
        if resume_payload and resume_payload.get("nodeId") == node.get("id") and resume_payload.get("action") == "SUBMIT":
            values = resume_payload.get("values")
            errors = sorted(Draft202012Validator(schema).iter_errors(values), key=lambda item: list(item.path))
            if errors: raise ValueError(f"HITL表单输入无效：{errors[0].message}")
            output = {"values": values}
        else:
            return {"status": "WAITING_INPUT", "output": None, "diagnostic": None, "interrupt": {"kind": "HITL_FORM", "title": (node.get("config") or {}).get("title"), "description": (node.get("config") or {}).get("description", ""), "fields": fields, "context": inputs}, "handlerVersion": manifest.handler_version}
    elif manifest.type == "condition":
        rule = simulation.get("rule") or (node.get("config") or {}).get("rule")
        if not isinstance(rule, dict): raise ValueError("condition节点需要config.rule")
        output = {"matched": await cpu_run(evaluate, rule, inputs)}
    else:
        output = {"ok": True}

    if not isinstance(output, dict): raise ValueError("节点没有生成JSON对象输出")
    await cpu_run(_validate_output, manifest, output)
    return {"status": "SUCCEEDED", "output": output, "diagnostic": None, "interrupt": None, "handlerVersion": manifest.handler_version}
