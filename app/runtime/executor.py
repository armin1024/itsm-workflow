from __future__ import annotations

import re
from typing import Any

from jsonschema import Draft202012Validator

from app.cli import CliExecutionError, execute_sql_read, redact_diagnostic
from app.conditions import evaluate
from app.runtime import NODE_REGISTRY
from app.runtime.contracts import ExecutionMode
from app.runtime.model_client import ModelInvocationError, invoke_structured


PLACEHOLDER = re.compile(r"\{\{([A-Za-z_][A-Za-z0-9_]*)\}\}")


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
    raise ValueError(f"SQL参数不支持类型 {value_type}")


def render_sql(node: dict[str, Any], inputs: dict[str, Any]) -> str:
    definitions = {str(item.get("name")): item for item in node.get("inputs", []) if isinstance(item, dict)}
    template = str((node.get("config") or {}).get("sqlTemplate") or "")
    missing = [name for name in PLACEHOLDER.findall(template) if name not in inputs]
    if missing:
        raise ValueError("缺少SQL输入参数：" + "、".join(dict.fromkeys(missing)))
    return PLACEHOLDER.sub(lambda match: _sql_literal(inputs[match.group(1)], str(definitions[match.group(1)].get("type") or "string")), template)


def _validate_output(manifest, output: dict[str, Any]) -> None:
    errors = sorted(Draft202012Validator(manifest.output_schema).iter_errors(output), key=lambda item: list(item.path))
    if errors:
        raise ValueError(f"节点输出不符合Schema：{errors[0].message}")


async def execute_single_node(*, node: dict[str, Any], inputs: dict[str, Any], mode: str, simulation: dict[str, Any] | None = None, api_key: str | None = None, ticket_id: int | None = None) -> dict[str, Any]:
    """Execute one Registry node; the AOPS key never enters output or storage."""
    execution_mode = ExecutionMode(mode)
    manifest = NODE_REGISTRY.validate_node(node)
    if execution_mode not in manifest.supported_modes or not manifest.allow_single_node_debug:
        raise ValueError(f"节点 {manifest.type} 不允许 {execution_mode.value} 单节点调试")
    if execution_mode == ExecutionMode.PRODUCTION:
        raise ValueError("Studio禁止PRODUCTION单节点调试")
    simulation = simulation or {}
    if execution_mode == ExecutionMode.DRY_RUN:
        return {"status": "SUCCEEDED", "output": {"plan": {"type": manifest.type, "title": node.get("title"), "config": node.get("config"), "inputs": inputs}}, "diagnostic": None, "interrupt": None, "handlerVersion": manifest.handler_version}

    output: dict[str, Any] | None = None
    if execution_mode == ExecutionMode.SIMULATION and manifest.type in {"sql_read", "llm_extract"}:
        output = simulation.get("fixtureOutput")
        if not isinstance(output, dict):
            raise ValueError(f"{manifest.type}模拟需要simulation.fixtureOutput对象")
    elif manifest.type == "sql_read" and execution_mode == ExecutionMode.TEST:
        if not api_key:
            raise ValueError("TEST模式执行SQL读必须提供AOPS_API_KEY")
        if not ticket_id:
            raise ValueError("TEST模式执行SQL读必须提供正整数工单ID")
        try:
            result = await execute_sql_read(database_ref=str(node["config"]["databaseRef"]), sql=render_sql(node, inputs), ticket_id=ticket_id, api_key=api_key, timeout_seconds=int(node.get("timeoutSeconds") or 600))
        except CliExecutionError as exc:
            stdout, stdout_cut = redact_diagnostic(exc.stdout, 32 * 1024)
            stderr, stderr_cut = redact_diagnostic(exc.stderr, 16 * 1024)
            return {"status": "FAILED", "output": None, "diagnostic": {"errorCode": exc.code, "message": str(exc), "exitCode": exc.exit_code, "stdout": stdout, "stderr": stderr, "truncated": exc.truncated or stdout_cut or stderr_cut}, "interrupt": None, "handlerVersion": manifest.handler_version}
        output = {key: result.payload[key] for key in ("status", "data", "rowCount") if key in result.payload}
    elif manifest.type == "llm_extract" and execution_mode == ExecutionMode.TEST:
        config = node.get("config") or {}
        try:
            output = await invoke_structured(prompt_template_id=str(config.get("promptTemplateId") or ""), inputs=inputs, response_schema=dict(config.get("responseSchema") or {}), timeout_seconds=int(node.get("timeoutSeconds") or 60))
        except ModelInvocationError as exc:
            return {"status": "FAILED", "output": None, "diagnostic": {"errorCode": "MODEL_INVOCATION_FAILED", "message": str(exc)}, "interrupt": None, "handlerVersion": manifest.handler_version}
    elif manifest.type == "hitl_select":
        candidates = inputs.get("candidates")
        if not isinstance(candidates, list):
            raise ValueError("hitl_select输入缺少candidates数组")
        if len(candidates) == 1 and (node.get("config") or {}).get("autoSelectSingle", True):
            output = {"selected": candidates[0]}
        elif len(candidates) > 1:
            return {"status": "WAITING_INPUT", "output": None, "diagnostic": None, "interrupt": {"kind": "HITL_SELECT", "title": (node.get("config") or {}).get("title", "请选择"), "actions": ["SELECT", "MANUAL_VALUE", "CANCEL"], "candidates": candidates}, "handlerVersion": manifest.handler_version}
        else:
            raise ValueError("没有可供选择的候选")
    elif manifest.type == "condition":
        rule = simulation.get("rule") or (node.get("config") or {}).get("rule")
        if not isinstance(rule, dict):
            raise ValueError("condition单节点调试需要config.rule")
        output = {"matched": evaluate(rule, inputs)}
    elif output is None:
        output = simulation.get("fixtureOutput") if isinstance(simulation.get("fixtureOutput"), dict) else {"ok": True}

    if not isinstance(output, dict):
        raise ValueError("节点没有生成JSON对象输出")
    _validate_output(manifest, output)
    return {"status": "SUCCEEDED", "output": output, "diagnostic": None, "interrupt": None, "handlerVersion": manifest.handler_version}
