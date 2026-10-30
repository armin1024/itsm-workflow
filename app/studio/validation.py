from __future__ import annotations

from typing import Any


SENSITIVE_KEYS = {"authorization", "aops_api_key", "api_key", "apikey", "token", "password", "passwd", "secret"}


def reject_credentials(value: Any, path: str = "$") -> None:
    """Credentials are accepted through headers, never saved as workflow data."""
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key).lower() in SENSITIVE_KEYS:
                raise ValueError(f"Studio禁止保存凭据字段：{path}.{key}")
            reject_credentials(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            reject_credentials(item, f"{path}[{index}]")
