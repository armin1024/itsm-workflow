from __future__ import annotations

from typing import Any

from jsonschema import Draft202012Validator

from app.conditions import evaluate
from app.runtime import NODE_REGISTRY
from app.runtime.contracts import ExecutionMode


SENSITIVE_KEYS = {"authorization", "aops_api_key", "api_key", "apikey", "token", "password", "passwd", "secret", "workflow_master_key"}


def reject_credentials(value: Any, path: str = "$") -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key).lower() in SENSITIVE_KEYS:
                raise ValueError(f"Studio禁止保存凭据字段：{path}.{key}")
            reject_credentials(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            reject_credentials(item, f"{path}[{index}]")


def run_single_node(*, node: dict[str, Any], inputs: dict[str, Any], mode: str, simulation: dict[str, Any] | None = None) -> dict[str, Any]:
    reject_credentials({"node": node, "inputs": inputs, "simulation": simulation or {}})
    execution_mode = ExecutionMode(mode)
    manifest = NODE_REGISTRY.validate_node(node)
    if execution_mode not in manifest.supported_modes or not manifest.allow_single_node_debug:
        raise ValueError(f"节点 {manifest.type} 不允许 {execution_mode} 单节点调试")
    simulation = simulation or {}
    if execution_mode == ExecutionMode.PRODUCTION:
        raise ValueError("Studio禁止PRODUCTION单节点调试")
    if execution_mode == ExecutionMode.DRY_RUN:
        return {"status": "SUCCEEDED", "output": {"plan": {"type": manifest.type, "title": node.get("title"), "config": node.get("config"), "inputs": inputs}}, "diagnostic": None, "interrupt": None, "handlerVersion": manifest.handler_version}
    if manifest.type == "sql_read":
        output = simulation.get("fixtureOutput")
        if not isinstance(output, dict):
            raise ValueError("sql_read模拟需要simulation.fixtureOutput对象")
    elif manifest.type == "llm_extract":
        output = simulation.get("fixtureOutput")
        if not isinstance(output, dict):
            raise ValueError("llm_extract模拟需要simulation.fixtureOutput对象")
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
    else:
        output = simulation.get("fixtureOutput") if isinstance(simulation.get("fixtureOutput"), dict) else {"ok": True}
    errors = sorted(Draft202012Validator(manifest.output_schema).iter_errors(output), key=lambda item: list(item.path))
    if errors:
        raise ValueError(f"模拟输出不符合节点输出Schema：{errors[0].message}")
    return {"status": "SUCCEEDED", "output": output, "diagnostic": None, "interrupt": None, "handlerVersion": manifest.handler_version}
