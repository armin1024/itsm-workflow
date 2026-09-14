from __future__ import annotations

from typing import Any


def pointer(document: Any, path: str) -> Any:
    if path == "":
        return document
    if not path.startswith("/"):
        raise ValueError("JSON Pointer 必须以 / 开始")
    value = document
    for raw in path[1:].split("/"):
        key = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(value, list):
            value = value[int(key)]
        elif isinstance(value, dict):
            value = value[key]
        else:
            raise KeyError(path)
    return value


def evaluate(rule: dict[str, Any], state: dict[str, Any]) -> bool:
    if "all" in rule:
        return all(evaluate(item, state) for item in rule["all"])
    if "any" in rule:
        return any(evaluate(item, state) for item in rule["any"])
    if "not" in rule:
        return not evaluate(rule["not"], state)
    op = rule.get("op")
    try:
        actual = pointer(state, str(rule.get("path") or ""))
        exists = True
    except (KeyError, IndexError, ValueError, TypeError):
        actual, exists = None, False
    expected = rule.get("value")
    operations = {
        "eq": lambda: actual == expected,
        "ne": lambda: actual != expected,
        "gt": lambda: actual > expected,
        "gte": lambda: actual >= expected,
        "lt": lambda: actual < expected,
        "lte": lambda: actual <= expected,
        "in": lambda: actual in expected,
        "contains": lambda: expected in actual,
        "exists": lambda: exists,
        "empty": lambda: not exists or actual in (None, "", [], {}),
    }
    if op not in operations:
        raise ValueError(f"不支持的条件操作符：{op}")
    try:
        return bool(operations[op]())
    except (TypeError, ValueError):
        return False
