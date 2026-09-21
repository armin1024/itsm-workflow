from __future__ import annotations

from typing import Any

import httpx

from app.config import settings


class Tec01Error(RuntimeError):
    def __init__(self, code: str, message: str, *, status_code: int = 500, retryable: bool = False):
        super().__init__(message); self.code, self.status_code, self.retryable = code, status_code, retryable


class Tec01Client:
    """Small callback client. tec01 actively dispatches work to this service."""

    def __init__(self, *, base_url: str | None = None, token: str | None = None, transport: httpx.AsyncBaseTransport | None = None):
        self.base_url = (base_url or settings.tec01_base_url).rstrip("/"); self.token = token or settings.tec01_service_token; self.transport = transport

    async def _post(self, path: str, payload: dict[str, Any], request_id: str) -> dict[str, Any]:
        if not self.base_url or not self.token: raise Tec01Error("TEC01_NOT_CONFIGURED", "TEC01_BASE_URL或TEC01_SERVICE_TOKEN未配置", status_code=503)
        headers = {"Authorization": "Bearer " + self.token, "Content-Type": "application/json", "X-Request-Id": request_id}
        try:
            async with httpx.AsyncClient(transport=self.transport, timeout=settings.tec01_timeout_seconds) as client:
                response = await client.post(self.base_url + path, headers=headers, json=payload)
        except httpx.TimeoutException as exc: raise Tec01Error("TEC01_TIMEOUT", "tec01回调超时", status_code=503, retryable=True) from exc
        except httpx.HTTPError as exc: raise Tec01Error("TEC01_UNAVAILABLE", type(exc).__name__, status_code=503, retryable=True) from exc
        if response.is_error:
            try: value = response.json()
            except ValueError: value = {}
            raise Tec01Error(str(value.get("code") or f"TEC01_HTTP_{response.status_code}"), str(value.get("message") or "tec01回调失败"), status_code=response.status_code, retryable=response.status_code >= 500)
        if response.status_code == 204: return {}
        value = response.json()
        if not isinstance(value, dict): raise Tec01Error("TEC01_INVALID_RESPONSE", "tec01响应必须是JSON对象", status_code=502)
        return value

    async def compiler_progress(self, job_id: str, payload: dict[str, Any]) -> dict[str, Any]: return await self._post(f"/internal/v1/compiler/jobs/{job_id}/progress", payload, f"{job_id}:{payload.get('stage')}")
    async def compiler_complete(self, job_id: str, payload: dict[str, Any]) -> dict[str, Any]: return await self._post(f"/internal/v1/compiler/jobs/{job_id}/complete", payload, f"{job_id}:complete")
    async def compiler_fail(self, job_id: str, payload: dict[str, Any]) -> dict[str, Any]: return await self._post(f"/internal/v1/compiler/jobs/{job_id}/fail", payload, f"{job_id}:fail")
    async def node_started(self, run_id: str, node_id: str, payload: dict[str, Any]) -> dict[str, Any]: return await self._post(f"/internal/v1/runs/{run_id}/nodes/{node_id}/started", payload, f"{payload['attemptId']}:started")
    async def node_completed(self, run_id: str, node_id: str, payload: dict[str, Any]) -> dict[str, Any]: return await self._post(f"/internal/v1/runs/{run_id}/nodes/{node_id}/completed", payload, f"{payload['attemptId']}:completed")
    async def run_released(self, run_id: str, payload: dict[str, Any]) -> dict[str, Any]: return await self._post(f"/internal/v1/runs/{run_id}/released", payload, f"{payload['dispatchId']}:released")
