from __future__ import annotations

import asyncio
import json
import uuid
import time
from typing import Any, Awaitable, Callable
from urllib.parse import quote

import httpx

from app.cli import redact_diagnostic
from app.config import settings
from app.call_logs import call_span, emit, hide_credentials
from app.log_descriptions import BUSINESS_ERRORS
from app.http_clients import get_tec01_client
from app.resources import cpu_run


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

    async def _post(self, path: str, payload: dict[str, Any], request_id: str, *, expected_ack: dict[str, str] | None = None, extraction_id: str | None = None, call_id: str | None = None) -> dict[str, Any]:
        if not self.base_url or not self.token:
            raise Tec01Error("TEC01_NOT_CONFIGURED", "TEC01_BASE_URL或TEC01_SERVICE_TOKEN未配置", status_code=503)
        headers = {"Authorization": "Bearer " + self.token, "Content-Type": "application/json", "X-Request-Id": request_id}
        call_id = call_id or uuid.uuid4().hex
        failure_code = (payload.get("error") or {}).get("code") if isinstance(payload.get("error"), dict) else None
        failure_code = failure_code if isinstance(failure_code, str) and failure_code in BUSINESS_ERRORS else None
        # Encode exactly once; transport retries reuse the identical bytes.
        encoded = await cpu_run(lambda: json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8"))
        async with get_tec01_client(transport=self.transport) as client:
            attempts = settings.tec01_callback_max_attempts if expected_ack is not None else 6
            for attempt in range(attempts):
                with hide_credentials(self.token), call_span(peer="TEC01", callId=call_id, businessErrorCode=failure_code, direction="OUTBOUND", operation=payload.get("type", "callback"), stage=payload.get("stage"), businessStatus=payload.get("status"), method="POST", url=self.base_url + path, requestId=payload.get("requestId") or request_id, runId=(expected_ack or {}).get("runId"), knowledgeId=extraction_id, dispatchId=payload.get("dispatchId"), nodeId=payload.get("nodeId"), attemptId=payload.get("attemptId"), attempt=attempt + 1, maxAttempts=attempts) as outcome:
                    try:
                        response = await client.post(self.base_url + path, headers=headers, content=encoded, timeout=min(settings.tec01_timeout_seconds, 30))
                        outcome["httpStatus"] = response.status_code
                    except httpx.TimeoutException as exc:
                        error = Tec01Error("TEC01_TIMEOUT", "tec01回调超时", status_code=503, retryable=True)
                        error.__cause__ = exc
                        outcome["errorCode"] = error.code
                    except httpx.HTTPError as exc:
                        error = Tec01Error("TEC01_UNAVAILABLE", "无法连接tec01回调接口", status_code=503, retryable=True)
                        error.__cause__ = exc
                        outcome["errorCode"] = error.code
                    else:
                        if not response.is_error:
                            if response.status_code != 200:
                                raise Tec01Error("TEC01_ACK_INVALID", "回报必须返回HTTP 200及有效确认", status_code=502)
                            try:
                                value = response.json()
                            except ValueError as exc:
                                raise Tec01Error("TEC01_INVALID_RESPONSE", "tec01响应不是有效JSON对象", status_code=502) from exc
                            if not isinstance(value, dict):
                                raise Tec01Error("TEC01_INVALID_RESPONSE", "tec01响应必须是JSON对象", status_code=502)
                            if expected_ack is not None:
                                data = value.get("data")
                                if type(value.get("status")) is not int or value.get("status") != 200 or not isinstance(data, dict):
                                    raise Tec01Error("TEC01_ACK_INVALID", "运行回报必须返回status=200及对应运行确认", status_code=502)
                                if data.get("runId") != expected_ack["runId"]:
                                    raise Tec01Error("TEC01_ACK_MISMATCH", "回报确认与本次运行不匹配", status_code=409)
                            elif extraction_id is not None:
                                data = value.get("data")
                                if type(value.get("status")) is not int or value.get("status") != 200 or not isinstance(data, dict):
                                    raise Tec01Error("TEC01_EXTRACTION_ACK_INVALID", "提取回报必须返回status=200及对应知识确认", status_code=502)
                                if data.get("knowledgeId") != extraction_id:
                                    raise Tec01Error("TEC01_EXTRACTION_ACK_MISMATCH", "提取确认与本次知识不匹配", status_code=409)
                            outcome["acknowledged"] = True
                            return value
                        try:
                            value = response.json()
                        except ValueError:
                            value = {}
                        if not isinstance(value, dict):
                            value = {}
                        error = Tec01Error(str(value.get("code") or f"TEC01_HTTP_{response.status_code}"), self._message(value.get("message") or value.get("msg") or "tec01回调失败"), status_code=response.status_code, retryable=response.status_code >= 500)
                    outcome.setdefault("errorCode", "TEC01_REMOTE_ERROR")
                if not error.retryable or attempt == attempts - 1:
                    raise error
                emit("RETRY_SCHEDULED", "WARNING", peer="TEC01", spanId=outcome["spanId"], callId=call_id, runId=(expected_ack or {}).get("runId"), knowledgeId=extraction_id, nodeId=payload.get("nodeId"), businessStatus=payload.get("status"), businessErrorCode=failure_code, stage=payload.get("stage"), direction="OUTBOUND", operation=payload.get("type", "callback"), requestId=payload.get("requestId") or request_id, attempt=attempt + 1, maxAttempts=attempts, retryDelaySeconds=2 ** attempt)
                await self.sleep(2 ** attempt)
        raise AssertionError("callback retry loop exhausted")

    async def _execution_callback(self, run_id: str, payload: dict[str, Any], request_id: str) -> dict[str, Any]:
        call_id, started = uuid.uuid4().hex, time.monotonic()
        # tec01's HTTP 200 AopsResult means this exact request was committed
        # and adopted. Stale dispatches/conflicts must instead return HTTP 409.
        expected = {"runId": run_id}
        try:
            async with asyncio.timeout(settings.tec01_callback_wait_seconds):
                path = settings.tec01_run_callback_path.format(runId=quote(run_id, safe=""))
                return await self._post(path, payload, request_id, expected_ack=expected, call_id=call_id)
        except TimeoutError as exc:
            emit("CALL_FAILED", "ERROR", peer="TEC01", direction="OUTBOUND", operation="callback_ack_wait", callId=call_id, spanId=uuid.uuid4().hex, requestId=request_id, runId=run_id, dispatchId=payload.get("dispatchId"), nodeId=payload.get("nodeId"), attemptId=payload.get("attemptId"), durationMs=round((time.monotonic() - started) * 1000, 2), errorCode="TEC01_ACK_WAIT_EXCEEDED")
            raise Tec01Error("TEC01_ACK_WAIT_EXCEEDED", "保存确认总等待时间已到，停止后续执行", status_code=503) from exc

    async def extraction_callback(self, knowledge_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        extraction_request_id = payload.get("requestId")
        if not isinstance(extraction_request_id, str) or not extraction_request_id.strip():
            raise Tec01Error("TEC01_EXTRACTION_REQUEST_ID_MISSING", "提取回报缺少原提交requestId", status_code=422)
        request_id = f"{knowledge_id}:{extraction_request_id}:finished" if payload["type"] == "FINISHED" else f"{knowledge_id}:{extraction_request_id}:{payload['seq']}"
        path = settings.tec01_extraction_callback_path.format(knowledgeId=quote(knowledge_id, safe=""))
        return await self._post(path, payload, request_id, extraction_id=knowledge_id)

    async def node_started(self, run_id: str, node_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._execution_callback(run_id, {**payload, "type": "NODE_STARTED", "nodeId": node_id}, f"{payload['attemptId']}:started")

    async def node_completed(self, run_id: str, node_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._execution_callback(run_id, {**payload, "type": "NODE_COMPLETED", "nodeId": node_id}, f"{payload['attemptId']}:completed")

    async def run_released(self, run_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._execution_callback(run_id, {**payload, "type": "RUN_RELEASED"}, f"{payload['dispatchId']}:released")
