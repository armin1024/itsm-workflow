from __future__ import annotations

from typing import Any

import httpx

from app.config import settings


class Tec01Error(RuntimeError):
    def __init__(self, code: str, message: str, *, status_code: int = 500, retryable: bool = False, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.code, self.status_code, self.retryable, self.details = code, status_code, retryable, details or {}


class Tec01Client:
    def __init__(self, *, base_url: str | None = None, token: str | None = None, transport: httpx.AsyncBaseTransport | None = None):
        self.base_url = (base_url or settings.tec01_base_url).rstrip("/")
        self.token = token or settings.tec01_service_token
        self.transport = transport

    async def _request(self, method: str, path: str, *, json: dict[str, Any] | None = None, params: dict[str, Any] | None = None, headers: dict[str, str] | None = None, timeout: float | None = None, allow_no_content: bool = False) -> dict[str, Any] | None:
        if not self.base_url or not self.token:
            raise Tec01Error("TEC01_NOT_CONFIGURED", "TEC01_BASE_URL或TEC01_SERVICE_TOKEN未配置", status_code=503)
        request_headers = {"Authorization": "Bearer " + self.token, "Content-Type": "application/json", **(headers or {})}
        try:
            async with httpx.AsyncClient(transport=self.transport, timeout=timeout or settings.tec01_timeout_seconds) as client:
                response = await client.request(method, self.base_url + path, headers=request_headers, json=json, params=params)
        except httpx.TimeoutException as exc:
            raise Tec01Error("TEC01_TIMEOUT", "tec01请求超时", status_code=503, retryable=True) from exc
        except httpx.HTTPError as exc:
            raise Tec01Error("TEC01_UNAVAILABLE", type(exc).__name__, status_code=503, retryable=True) from exc
        if response.status_code == 204 and allow_no_content:
            return None
        if response.is_error:
            try:
                payload = response.json()
            except ValueError:
                payload = {}
            raise Tec01Error(str(payload.get("code") or f"TEC01_HTTP_{response.status_code}"), str(payload.get("message") or "tec01请求失败"), status_code=response.status_code, retryable=bool(payload.get("retryable")), details=payload.get("details"))
        try:
            value = response.json()
        except ValueError as exc:
            raise Tec01Error("TEC01_INVALID_RESPONSE", "tec01响应不是JSON", status_code=502) from exc
        if not isinstance(value, dict):
            raise Tec01Error("TEC01_INVALID_RESPONSE", "tec01响应必须是JSON对象", status_code=502)
        return value

    async def claim_compiler_job(self, compiler_id: str, compiler_version: str, catalog_digests: list[str], wait_seconds: int = 15) -> dict[str, Any] | None:
        return await self._request("POST", "/internal/v1/compiler/claims", json={"compilerId": compiler_id, "compilerVersion": compiler_version, "supportedCatalogDigests": catalog_digests, "waitSeconds": wait_seconds}, timeout=wait_seconds + 5, allow_no_content=True)

    async def heartbeat_compiler(self, lease_token: str) -> dict[str, Any]:
        return dict(await self._request("POST", f"/internal/v1/compiler/claims/{lease_token}/heartbeat", json={}) or {})

    async def complete_extraction(self, extraction_id: str, payload: dict[str, Any], idempotency_key: str) -> dict[str, Any]:
        return dict(await self._request("POST", f"/internal/v1/compiler/extractions/{extraction_id}/complete", json=payload, headers={"Idempotency-Key": idempotency_key}, timeout=30) or {})

    async def fail_extraction(self, extraction_id: str, payload: dict[str, Any], idempotency_key: str) -> dict[str, Any]:
        return dict(await self._request("POST", f"/internal/v1/compiler/extractions/{extraction_id}/fail", json=payload, headers={"Idempotency-Key": idempotency_key}) or {})

    async def register_executor(self, payload: dict[str, Any]) -> dict[str, Any]:
        return dict(await self._request("POST", "/internal/v1/executors", json=payload, headers={"Idempotency-Key": str(payload.get("executorId"))}) or {})

    async def claim_run(self, payload: dict[str, Any], wait_seconds: int = 15) -> dict[str, Any] | None:
        return await self._request("POST", "/internal/v1/execution/claims", json={**payload, "waitSeconds": wait_seconds}, timeout=wait_seconds + 5, allow_no_content=True)

    async def heartbeat_run(self, lease_token: str, payload: dict[str, Any]) -> dict[str, Any]:
        return dict(await self._request("POST", f"/internal/v1/execution/claims/{lease_token}/heartbeat", json=payload) or {})

    async def start_attempt(self, run_id: str, payload: dict[str, Any], idempotency_key: str) -> dict[str, Any]:
        return dict(await self._request("POST", f"/internal/v1/runs/{run_id}/attempts", json=payload, headers={"Idempotency-Key": idempotency_key}) or {})

    async def commit_attempt(self, run_id: str, attempt_id: str, payload: dict[str, Any], idempotency_key: str) -> dict[str, Any]:
        return dict(await self._request("POST", f"/internal/v1/runs/{run_id}/attempts/{attempt_id}/commit", json=payload, headers={"Idempotency-Key": idempotency_key}) or {})

    async def get_checkpoint(self, thread_id: str, checkpoint_id: str | None = None, checkpoint_namespace: str = "") -> dict[str, Any] | None:
        return await self._request("GET", f"/internal/v1/checkpoints/{thread_id}/latest", params={"checkpointId": checkpoint_id, "checkpointNamespace": checkpoint_namespace}, allow_no_content=True)

    async def list_checkpoints(self, thread_id: str, checkpoint_namespace: str = "", before: str | None = None, limit: int | None = None) -> dict[str, Any]:
        return dict(await self._request("GET", f"/internal/v1/checkpoints/{thread_id}", params={"checkpointNamespace": checkpoint_namespace, "before": before, "limit": limit}) or {"items": []})

    async def stage_checkpoint(self, thread_id: str, checkpoint_id: str, payload: dict[str, Any], idempotency_key: str) -> dict[str, Any]:
        return dict(await self._request("PUT", f"/internal/v1/runs/{thread_id}/staged-checkpoints/{checkpoint_id}", json=payload, headers={"Idempotency-Key": idempotency_key}) or {})

    async def stage_checkpoint_writes(self, thread_id: str, checkpoint_id: str, payload: dict[str, Any], idempotency_key: str) -> dict[str, Any]:
        return dict(await self._request("POST", f"/internal/v1/runs/{thread_id}/staged-checkpoints/{checkpoint_id}/writes", json=payload, headers={"Idempotency-Key": idempotency_key}) or {})

    async def delete_checkpoint_thread(self, thread_id: str) -> None:
        await self._request("DELETE", f"/internal/v1/checkpoints/{thread_id}", json={}, allow_no_content=True)
