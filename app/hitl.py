from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from jsonschema import Draft202012Validator

from app.conditions import pointer


MAX_CANDIDATES = 5000
LABEL_TOKEN = re.compile(r"\{\{([A-Za-z_][A-Za-z0-9_]*)\}\}")
FIELD_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,79}$")
FORM_TYPES = {"string", "integer", "number", "boolean"}


class HitlError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _field_list(values: Any, label: str) -> list[dict[str, Any]]:
    if not isinstance(values, list) or not values or not all(isinstance(item, dict) for item in values):
        raise ValueError(f"{label}必须是非空对象数组")
    names = [str(item.get("name") or "") for item in values]
    if len(names) != len(set(names)) or any(not FIELD_NAME.fullmatch(name) for name in names):
        raise ValueError(f"{label}字段名必须唯一且格式合法")
    return values


def form_schema(fields: list[dict[str, Any]]) -> dict[str, Any]:
    properties: dict[str, Any] = {}
    required: list[str] = []
    for field in fields:
        name, kind = str(field.get("name") or ""), str(field.get("type") or "")
        if not FIELD_NAME.fullmatch(name) or kind not in FORM_TYPES:
            raise ValueError("HITL表单字段名称或类型无效")
        schema: dict[str, Any] = {"type": kind}
        for key in ("minimum", "maximum", "minLength", "maxLength", "enum"):
            if key in field:
                schema[key] = field[key]
        properties[name] = schema
        if field.get("required"):
            required.append(name)
    return {"type": "object", "properties": properties, "required": required, "additionalProperties": False}


def validate_form(fields: list[dict[str, Any]], values: Any) -> dict[str, Any]:
    errors = sorted(Draft202012Validator(form_schema(fields)).iter_errors(values), key=lambda item: list(item.path))
    if errors:
        raise HitlError("HITL_FORM_INVALID", f"HITL表单输入无效：{errors[0].message}")
    return dict(values)


def validate_hitl_node(node: dict[str, Any]) -> None:
    config = node.get("config") or {}
    if node.get("type") == "hitl_select":
        if config.get("selectionMode") not in {"SINGLE", "MULTIPLE"}:
            raise ValueError("hitl_select.selectionMode只允许SINGLE或MULTIPLE")
        if not str(config.get("title") or "").strip() or not str(config.get("idPath") or "").startswith("/"):
            raise ValueError("hitl_select缺少标题或有效idPath")
        display = _field_list(config.get("displayFields"), "displayFields")
        outputs = _field_list(config.get("outputFields"), "outputFields")
        if any(not str(item.get("path") or "").startswith("/") or not str(item.get("label") or "").strip() for item in display):
            raise ValueError("displayFields必须包含label和有效JSON Pointer")
        if any(not str(item.get("path") or "").startswith("/") for item in outputs):
            raise ValueError("outputFields必须包含有效JSON Pointer")
        display_names = {str(item["name"]) for item in display}
        tokens = set(LABEL_TOKEN.findall(str(config.get("labelTemplate") or "")))
        if not tokens or not tokens <= display_names:
            raise ValueError("labelTemplate只能引用displayFields且至少包含一个变量")
        inputs = node.get("inputs") or []
        if len(inputs) != 1 or inputs[0].get("name") != "rows" or inputs[0].get("type") != "array" or (inputs[0].get("source") or {}).get("kind") != "NODE_OUTPUT":
            raise ValueError("hitl_select必须且只能声明一个绑定前置节点输出的array类型rows输入")
        minimum = int(config.get("minimumSelections") or 1)
        maximum = int(config.get("maximumSelections") or (1 if config["selectionMode"] == "SINGLE" else 100))
        if minimum < 1 or maximum < minimum or maximum > 100 or config["selectionMode"] == "SINGLE" and (minimum != 1 or maximum != 1):
            raise ValueError("hitl_select选择数量约束无效")
    elif node.get("type") == "hitl_form":
        fields = _field_list(config.get("fields"), "fields")
        if len(fields) > 50 or not str(config.get("title") or "").strip():
            raise ValueError("hitl_form标题不能为空且字段最多50项")
        Draft202012Validator.check_schema(form_schema(fields))


def _candidate_id(node_id: str, raw_id: Any, index: int) -> str:
    digest = hashlib.sha256(json.dumps([node_id, raw_id, index], ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()).hexdigest()
    return "candidate_" + digest[:24]


def build_candidates(node: dict[str, Any], rows: Any) -> list[dict[str, Any]]:
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise HitlError("HITL_ROWS_INVALID", "hitl_select输入必须是对象数组")
    if not rows:
        raise HitlError("NO_HITL_CANDIDATES", "没有可供选择的候选")
    if len(rows) > MAX_CANDIDATES:
        raise HitlError("HITL_CANDIDATE_LIMIT", f"候选数量超过{MAX_CANDIDATES}条")
    config = node["config"]
    candidates: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        try:
            raw_id = pointer(row, str(config["idPath"]))
            display = {str(item["name"]): pointer(row, str(item["path"])) for item in config["displayFields"]}
            values = {str(item["name"]): pointer(row, str(item["path"])) for item in config["outputFields"]}
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise HitlError("HITL_MAPPING_INVALID", f"第{index + 1}行无法按候选映射配置提取字段") from exc
        label = str(config["labelTemplate"])
        for name, value in display.items():
            label = label.replace("{{" + name + "}}", str(value))
        candidates.append({"candidateId": _candidate_id(str(node["id"]), raw_id, index), "label": label[:500], "display": display, "values": values})
    return candidates
