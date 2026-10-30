"""Bound heavy JSON ingress without changing endpoint schemas or wire fields."""
from __future__ import annotations

import asyncio
import json
import re
import secrets
import weakref

from fastapi import HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute

from app.config import settings
from app.resources import CPU_SCRATCH_BYTES, MIB, ResourceError, cpu_run, object_size
from app.studio.auth import COOKIE_NAME, valid_session

_ingress: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()
_SCALAR = re.compile(rb"[^ \t\r\n,\]\}]+")


def _preflight_json(raw: bytes):
    """Reject high-expansion trees before json.loads allocates their DOM.

    Strings are skipped using byte searches (not a regex with an unbounded
    backtracking stack). Quotes/brackets in SQL/text are not containers.
    This is conservative resource admission, not a replacement JSON parser.
    """
    estimated, depth, index = len(raw) * 4, 0, 0
    while index < len(raw):
        char = raw[index]
        if char == 34:
            estimated += 96
            end = index
            while True:
                end = raw.find(b'"', end + 1)
                if end < 0:
                    return  # json.loads supplies the normal syntax error.
                escaped, position = 0, end - 1
                while position > index and raw[position] == 92:
                    escaped += 1
                    position -= 1
                if escaped % 2 == 0:
                    break
            index = end + 1
        elif char in (123, 91):
            depth += 1
            estimated += 128 if char == 123 else 96
            if depth > 128:
                raise ResourceError("JSON_COMPLEXITY_LIMIT", "JSON嵌套超过128层，请简化数据")
            index += 1
        elif char in (125, 93):
            depth -= 1
            index += 1
        elif char == 58:
            estimated += 96
            index += 1
        elif char in (32, 9, 10, 13, 44):
            index += 1
        else:
            match = _SCALAR.match(raw, index)
            end = match.end() if match else index + 1
            if end - index > 4096:
                raise ResourceError("JSON_COMPLEXITY_LIMIT", "JSON标量过长，请将长内容作为字符串传递")
            estimated += 64
            index = end
        if estimated > CPU_SCRATCH_BYTES:
            raise ResourceError("REQUEST_MEMORY_LIMIT", "JSON容器数量或展开估算超出处理预算，请拆分数据", status_code=413)


def _decode(chunks: list[bytes], decode_json: bool):
    raw = b"".join(chunks)
    if raw and decode_json:
        _preflight_json(raw)
    value = json.loads(raw) if raw and decode_json else None
    if value is not None and object_size(value, limit=CPU_SCRATCH_BYTES - len(raw)) + len(raw) > CPU_SCRATCH_BYTES:
        raise ResourceError("REQUEST_MEMORY_LIMIT", "请求展开后过大，请拆分数据", status_code=413)
    return raw, value


class BoundedJsonRoute(APIRoute):
    def get_route_handler(self):
        original = super().get_route_handler()
        heavy = any(part in self.path for part in ("/workflows/", "/extractions", "/compiler/")) or self.path.endswith("/executions/dispatch")
        evidence = "/extractions" in self.path or "/compiler/" in self.path

        async def handler(request: Request):
            if request.method not in {"POST", "PUT", "PATCH"}:
                return await original(request)
            # Reject unauthenticated large bodies before allocating or decoding.
            if self.path.startswith("/internal/"):
                header = request.headers.get("Authorization", "")
                if not settings.runtime_service_token or not secrets.compare_digest(header, "Bearer " + settings.runtime_service_token):
                    raise HTTPException(401, {"code": "RUNTIME_AUTH_FAILED", "message": "Runtime服务身份无效", "retryable": False})
            elif self.path.startswith("/api/v1/studio/") and self.path != "/api/v1/studio/session":
                if not valid_session(request.cookies.get(COOKIE_NAME)):
                    raise HTTPException(401, "需要管理Token或登录已过期")
            loop = asyncio.get_running_loop()
            reserved = False
            def reserve_ingress():
                nonlocal reserved
                if reserved:
                    return
                count = _ingress.get(loop, 0)
                if count >= settings.cpu_max_workers:
                    raise ResourceError("REQUEST_NO_CAPACITY", "大请求处理名额已满，请稍后重试", status_code=429, retryable=True)
                _ingress[loop], reserved = count + 1, True
            # Small dispatches should not become serial merely because there
            # is one CPU worker. The separate gate protects genuinely large
            # payloads; actual streamed bytes override any claimed length.
            declared = request.headers.get("content-length", "")
            if heavy and (not declared.isdigit() or int(declared) > 65536):
                reserve_ingress()
            try:
                maximum = min(settings.request_max_body_mb, settings.compiler_evidence_max_mb if evidence else settings.request_max_body_mb) * MIB
                if not heavy:
                    maximum = min(maximum, 64 * 1024)
                chunks, size = [], 0
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > maximum:
                        raise ResourceError("REQUEST_TOO_LARGE", f"请求超过{maximum // MIB}MiB，请拆分数据", status_code=413)
                    if heavy and size > 65536:
                        reserve_ingress()
                    chunks.append(chunk)
                content_type = request.headers.get("content-type", "application/json").split(";", 1)[0].strip().lower()
                decode_json = content_type == "application/json" or content_type.endswith("+json")
                try:
                    raw, value = await cpu_run(_decode, chunks, decode_json) if heavy else _decode(chunks, decode_json)
                except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
                    raise RequestValidationError([{"type": "json_invalid", "loc": ("body",), "msg": "请求不是有效JSON"}]) from exc
                except RecursionError as exc:
                    raise ResourceError("JSON_COMPLEXITY_LIMIT", "JSON结构过深，请简化数据") from exc
                request._body = raw
                if decode_json and raw:
                    request._json = value
                # Keep the ingress reservation through validation/preparation;
                # otherwise decoded payloads could queue unbounded behind CPU.
                return await original(request)
            finally:
                if reserved:
                    _ingress[loop] = max(0, _ingress.get(loop, 1) - 1)

        return handler
