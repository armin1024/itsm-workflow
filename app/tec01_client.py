from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable
from urllib.parse import quote

import httpx

from app.cli import redact_diagnostic
from app.config import settings


class Tec01Error(RuntimeError):
    def __init__(self, code: str, message: str, *, status_code: int = 500, retryable: bool = False):
        super().__init__(message)
        self.code, self.status_code, self.retryable = code, status_code, retryable


class Tec01Client:
    """Unified tec01 callbacks; the same request id is retained across retries."""

    def __init__(self, *, base_url: str | None = None, token: str | None = None, transport: httpx.AsyncBaseTransport | None = None, sleep: Callable[[float], Awaitable[None]] = asyncio.sleep):
        self.base_url = (base_url if base_url is not None else settings.tec01_base_url).rstrip("/")
        self.token = token if token is not None else settings.tec01_service_token
        self.transport, self.sleep = transport, sleep

    def _message(self, value: Any) -> str:
        text = str(value).replace(self.token, "***") if self.token else str(value)
        return redact_diagnostic(text, 1000)[0]

    async def _post(self, path: str, payload: dict[str, Any], request_id: str) -> dict[str, Any]:
        if not self.base_url or not self.token:
            raise Tec01Error("TEC01_NOT_CONFIGURED", "TEC01_BASE_URL或TEC01_SERVICE_TOKEN未配置", status_code=503)
        headers = {"Authorization": "Bearer " + self.token, "Content-Type": "application/json", "X-Request-Id": request_id}
        async with httpx.AsyncClient(transport=self.transport, timeout=min(settings.tec01_timeout_seconds, 30)) as client:
            for attempt in range(6):
                try:
                    response = await client.post(self.base_url + path, headers=headers, json=payload)
                except httpx.TimeoutException as exc:
                    error = Tec01Error("TEC01_TIMEOUT", "tec01回调超时", status_code=503, retryable=True)
                    error.__cause__ = exc
                except httpx.HTTPError as exc:
                    error = Tec01Error("TEC01_UNAVAILABLE", "无法连接tec01回调接口", status_code=503, retryable=True)
                    error.__cause__ = exc
                else:
                    if not response.is_error:
                        if response.status_code == 204:
                            return {}
                        try:
                            value = response.json()
                        except ValueError as exc:
                            raise Tec01Error("TEC01_INVALID_RESPONSE", "tec01响应不是有效JSON对象", status_code=502) from exc
                        if not isinstance(value, dict):
                            raise Tec01Error("TEC01_INVALID_RESPONSE", "tec01响应必须是JSON对象", status_code=502)
                        return value
                    try:
                        value = response.json()
                    except ValueError:
                        value = {}
                    if not isinstance(value, dict):
                        value = {}
                    error = Tec01Error(str(value.get("code") or f"TEC01_HTTP_{response.status_code}"), self._message(value.get("message") or value.get("msg") or "tec01回调失败"), status_code=response.status_code, retryable=response.status_code >= 500)
                if not error.retryable or attempt == 5:
                    raise error
                await self.sleep(2 ** attempt)
        raise AssertionError("callback retry loop exhausted")

    async def extraction_callback(self, knowledge_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        request_id = f"{knowledge_id}:finished" if payload["type"] == "FINISHED" else f"{knowledge_id}:{payload['seq']}"
        return await self._post(f"/internal/v1/extractions/{quote(knowledge_id, safe='')}/callbacks", payload, request_id)

    async def node_started(self, run_id: str, node_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._post(f"/internal/v1/runs/{quote(run_id, safe='')}/callbacks", {**payload, "type": "NODE_STARTED", "nodeId": node_id}, f"{payload['attemptId']}:started")

    async def node_completed(self, run_id: str, node_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._post(f"/internal/v1/runs/{quote(run_id, safe='')}/callbacks", {**payload, "type": "NODE_COMPLETED", "nodeId": node_id}, f"{payload['attemptId']}:completed")

    async def run_released(self, run_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._post(f"/internal/v1/runs/{quote(run_id, safe='')}/callbacks", {**payload, "type": "RUN_RELEASED"}, f"{payload['dispatchId']}:released")
