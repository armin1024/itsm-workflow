from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import signal
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable

from app.config import settings
from app.call_logs import logged_cli
from app.resources import cpu_run, cpu_slot


class CliExecutionError(RuntimeError):
    def __init__(self, code: str, message: str, *, exit_code: int | None = None, stdout: bytes = b"", stderr: bytes = b"", truncated: bool = False):
        super().__init__(message)
        self.code = code
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr
        self.truncated = truncated


_SECRET_PATTERNS = (
    (re.compile(r"(?i)(authorization\s*[:=]\s*bearer\s+)[^\s,;]+"), r"\1***"),
    (re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+"), r"\1***"),
    (re.compile(r"(?i)((?:aops[_-]?api[_-]?key|api[_-]?key|token|password|passwd)\s*[:=]\s*)[^\s,;]+"), r"\1***"),
    (re.compile(r"(postgres(?:ql)?(?:\+asyncpg)?://[^:/\s]+:)[^@\s]+@", re.I), r"\1***@"),
)


def _sql_error_summary(value: object) -> str:
    """Extract an explicit upstream error, never serialize its whole envelope."""
    item = value
    if isinstance(value, str):
        try:
            item = json.loads(value)
        except (ValueError, RecursionError):
            pass
    for _ in range(4):
        if not isinstance(item, dict):
            break
        item = next((item[key] for key in ("message", "msg", "error", "detail", "reason") if item.get(key)), None)
    if not isinstance(item, (str, int, float)) or isinstance(item, bool):
        return "服务返回错误，未提供可读说明"
    text, _ = redact_diagnostic(str(item), 4096)
    return re.sub(r"\s+", " ", text).strip()[:500] or "服务返回错误，未提供可读说明"


def redact_diagnostic(value: bytes | str, limit: int) -> tuple[str, bool]:
    text = value.decode("utf-8", "replace") if isinstance(value, bytes) else value
    for pattern, replacement in _SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    encoded = text.encode("utf-8")
    if len(encoded) <= limit:
        return text, False
    tail = encoded[-limit:].decode("utf-8", "replace")
    return "[前部内容已截断]\n" + tail, True


def _failure_message(stdout: bytes, stderr: bytes, fallback: str) -> str:
    for source in (stdout, stderr):
        text, _ = redact_diagnostic(source, 64 * 1024)
        stripped = text.strip()
        if not stripped:
            continue
        try:
            payload = json.loads(stripped)
            if isinstance(payload, dict):
                for key in ("message", "error", "msg", "detail"):
                    if payload.get(key):
                        return re.sub(r"\s+", " ", str(payload[key])).strip()[:500]
        except json.JSONDecodeError:
            pass
        event_values = []
        current = ""
        for line in stripped.replace("\r", "").split("\n"):
            if line.startswith("event:"):
                current = line[6:].strip().lower()
            elif line.startswith("data:") and current in {"error", "failed", "failure", "message"}:
                value = line[5:].strip().strip('"')
                if value and value not in {"<nil>", "null", "!ok"}:
                    event_values.append(value)
        if event_values:
            return re.sub(r"\s+", " ", event_values[-1]).strip()[:500]
        lines = [line.strip() for line in stripped.splitlines() if line.strip()]
        if lines:
            return re.sub(r"\s+", " ", lines[-1]).strip()[:500]
    return fallback


@dataclass(frozen=True)
class CliMetadata:
    version: str
    sha256: str


@dataclass(frozen=True)
class CliResult:
    payload: dict
    stdout: bytes
    stderr: bytes
    exit_code: int
    metadata: CliMetadata


def _cli_environment(api_key: str) -> dict[str, str]:
    environment = {
        "AOPS_BASE_URL": settings.aops_base_url,
        "AOPS_API_KEY": api_key,
        "LANG": os.environ.get("LANG", "C.UTF-8"),
    }
    for key in ("SSL_CERT_FILE", "SSL_CERT_DIR", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy"):
        if key in os.environ:
            environment[key] = os.environ[key]
    if settings.aops_ca_bundle:
        environment["SSL_CERT_FILE"] = settings.aops_ca_bundle
    return environment


def parse_sql_read_output(stdout: bytes) -> dict:
    """Parse either the legacy JSON envelope or db-read's SSE transcript."""
    try:
        text = stdout.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise CliExecutionError("INVALID_OUTPUT", "aops-cli 输出不是 UTF-8") from exc
    stripped = text.strip()
    if not stripped:
        raise CliExecutionError("INVALID_SSE", "aops-cli SQL 读操作没有返回内容")
    if stripped.startswith("{"):
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise CliExecutionError("INVALID_JSON", "aops-cli 未返回有效 JSON") from exc
        if not isinstance(payload, dict) or type(payload.get("status")) is not int or payload.get("status") != 0:
            status = payload.get("status") if isinstance(payload, dict) else None
            message = _sql_error_summary(payload)
            status_note = str(status) if type(status) is int else "缺失或类型非法"
            raise CliExecutionError("AOPS_BUSINESS_ERROR", f"AOPS 返回失败状态：{status_note}；{message}")
        return payload

    if stripped.startswith("<"):
        raise CliExecutionError("NON_SSE_RESPONSE", "aops-cli返回HTML/XML页面而非SQL读结果，请核查AOPS鉴权、接口地址及代理；CLI未提供实际HTTP状态")

    events: list[tuple[str, str]] = []
    event_name = "message"
    data_lines: list[str] = []

    def flush() -> None:
        nonlocal event_name, data_lines
        if data_lines or event_name != "message":
            events.append((event_name, "\n".join(data_lines)))
        event_name, data_lines = "message", []

    for raw_line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = raw_line.rstrip("\n")
        if not line:
            flush()
            continue
        if line.startswith(":") or line.startswith("retry:") or line.startswith("id:"):
            continue
        if line.startswith("event:"):
            if data_lines:
                flush()
            event_name = line[6:].strip() or "message"
        elif line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
    flush()
    # A server may close an error stream without emitting done. The explicit
    # error is the cause; do not replace it with a secondary framing failure.
    for name, data in events:
        if name.lower() in {"error", "failed", "failure"}:
            raise CliExecutionError("AOPS_BUSINESS_ERROR", "AOPS SQL读返回错误：" + _sql_error_summary(data))
    if not events:
        raise CliExecutionError("NON_SSE_RESPONSE", "aops-cli返回非JSON且非SSE内容，请查看节点脱敏stdout/stderr并核查鉴权、接口地址和CLI版本")
    if not any(name == "done" for name, _ in events):
        raise CliExecutionError("INVALID_SSE", "SQL读SSE已结束但缺少done终止事件，结果可能不完整；请核查AOPS或代理断流及CLI输出，勿自动重试SQL")

    titles: list[str] = []
    fields: list[dict] = []
    messages: list[object] = []
    stream_uuid: str | None = None
    done_value: str | None = None
    for name, data in events:
        value = data.strip()
        if name == "done":
            done_value = value.strip('"')
            continue
        if value in {"", "<nil>", "null"}:
            continue
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            decoded = value
        if name == "uuid":
            stream_uuid = str(decoded)
        elif name == "title" and isinstance(decoded, list):
            titles = [str(item) for item in decoded]
        elif name == "fieldtype" and isinstance(decoded, list):
            fields = [item for item in decoded if isinstance(item, dict)]
        elif name == "message":
            messages.append(decoded)
    if done_value != "!ok":
        raise CliExecutionError("AOPS_BUSINESS_ERROR", "AOPS SQL读SSE终止但未成功：" + _sql_error_summary(done_value or "未返回成功标记"))

    rows: list[dict | object] = []
    for message in messages:
        if isinstance(message, dict):
            rows.append(message)
        elif isinstance(message, list) and titles and len(message) == len(titles):
            rows.append(dict(zip(titles, message, strict=True)))
        else:
            rows.append({"values": message})
    return {
        "status": 0,
        "data": rows,
        "rowCount": len(rows),
        "stream": {"uuid": stream_uuid, "title": titles, "fieldtype": fields, "done": done_value},
    }


_metadata_cache: OrderedDict[tuple, CliMetadata] = OrderedDict()
_metadata_flights: dict[tuple, asyncio.Task] = {}


def _cli_signature(executable: Path) -> tuple:
    executable = executable.resolve()
    if not executable.is_file() or not os.access(executable, os.X_OK):
        raise CliExecutionError("CLI_NOT_FOUND", f"aops-cli 不可执行：{executable}")
    info = executable.stat()
    return (str(executable), info.st_dev, info.st_ino, info.st_mtime_ns, info.st_size)


def _binary_digest(executable: Path) -> str:
    digest = hashlib.sha256()
    with executable.open("rb") as stream:
        for chunk in iter(lambda: stream.read(256 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


async def _inspect_uncached(executable: Path, signature: tuple) -> CliMetadata:
    digest = await cpu_run(_binary_digest, executable)
    process = await asyncio.create_subprocess_exec(str(executable), "--version", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    stdout_task = asyncio.create_task(_read_limited(process.stdout, 64 * 1024))
    stderr_task = asyncio.create_task(_read_limited(process.stderr, 64 * 1024))
    try:
        stdout, _, _ = await asyncio.wait_for(asyncio.gather(stdout_task, stderr_task, process.wait()), timeout=10)
    finally:
        if process.returncode is None:
            await _terminate(process)
        for task in (stdout_task, stderr_task):
            if not task.done():
                task.cancel()
        await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
    if process.returncode != 0:
        raise CliExecutionError("CLI_VERSION_FAILED", "无法读取 aops-cli 版本", exit_code=process.returncode)
    version = stdout.decode("utf-8", "replace").strip()[:120]
    result = CliMetadata(version, digest)
    if await cpu_run(_cli_signature, executable) == signature:
        _metadata_cache[signature] = result
        _metadata_cache.move_to_end(signature)
        while len(_metadata_cache) > 8:
            _metadata_cache.popitem(last=False)
    return result


async def inspect_cli(path: Path | None = None) -> CliMetadata:
    # Metadata stat is I/O, not parsing work. Keep health checks off the CPU
    # parsing queue so a large result does not delay cached CLI readiness.
    signature = await asyncio.to_thread(_cli_signature, path or settings.aops_cli_path)
    result = _metadata_cache.get(signature)
    if result is None:
        task = _metadata_flights.get(signature)
        if task is None:
            task = asyncio.create_task(_inspect_uncached(Path(signature[0]), signature), name="cli-metadata")
            _metadata_flights[signature] = task

            def finished(done: asyncio.Task) -> None:
                if _metadata_flights.get(signature) is done:
                    _metadata_flights.pop(signature, None)
                if not done.cancelled():
                    done.exception()  # Consume failures even if all callers disconnect.

            task.add_done_callback(finished)
        try:
            result = await asyncio.shield(task)
        except asyncio.CancelledError:
            # Metadata work is shared; do not cancel another caller's probe or
            # release this run while its owned offloaded work is still active.
            await asyncio.gather(asyncio.shield(task), return_exceptions=True)
            raise
    if settings.aops_cli_version_requirement and settings.aops_cli_version_requirement not in result.version:
        raise CliExecutionError("CLI_VERSION_MISMATCH", f"aops-cli 版本不满足要求：{settings.aops_cli_version_requirement}")
    return result


async def _read_limited(stream: asyncio.StreamReader, limit: int) -> bytes:
    chunks: list[bytes] = []
    size = 0
    while True:
        chunk = await stream.read(65536)
        if not chunk:
            return b"".join(chunks)
        size += len(chunk)
        if size > limit:
            raise CliExecutionError("OUTPUT_LIMIT_EXCEEDED", f"aops-cli 输出超过限制 {limit} bytes")
        chunks.append(chunk)


async def _terminate(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        await asyncio.wait_for(process.wait(), timeout=3)
    except TimeoutError:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        await process.wait()


@logged_cli("sql_read")
async def execute_sql_read(
    *,
    database_ref: str,
    sql: str,
    ticket_id: int,
    api_key: str,
    timeout_seconds: int,
    cancel_requested: Callable[[], Awaitable[bool]] | None = None,
) -> CliResult:
    metadata = await inspect_cli()
    async with cpu_slot() as lease:
        return await _execute_sql_read(database_ref=database_ref, sql=sql, ticket_id=ticket_id, api_key=api_key, timeout_seconds=timeout_seconds, cancel_requested=cancel_requested, metadata=metadata, lease=lease)


async def _execute_sql_read(*, database_ref: str, sql: str, ticket_id: int, api_key: str, timeout_seconds: int, cancel_requested: Callable[[], Awaitable[bool]] | None, metadata: CliMetadata, lease) -> CliResult:
    environment = _cli_environment(api_key)
    process = await asyncio.create_subprocess_exec(
        str(settings.aops_cli_path), "db", "read",
        "--db", database_ref,
        "--sqltext", sql,
        "--comment", f"#uatu-{ticket_id}",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=environment,
        start_new_session=True,
    )
    stdout_task = asyncio.create_task(_read_limited(process.stdout, 20 * 1024 * 1024))
    stderr_task = asyncio.create_task(_read_limited(process.stderr, 1024 * 1024))
    started = asyncio.get_running_loop().time()
    try:
        while process.returncode is None:
            if cancel_requested and await cancel_requested():
                await _terminate(process)
                raise CliExecutionError("CANCELLED", "运行已取消", exit_code=process.returncode)
            if asyncio.get_running_loop().time() - started > timeout_seconds:
                await _terminate(process)
                raise CliExecutionError("TIMEOUT", "aops-cli 执行超时", exit_code=process.returncode)
            if stdout_task.done() and stdout_task.exception():
                await _terminate(process)
                raise stdout_task.exception()
            if stderr_task.done() and stderr_task.exception():
                await _terminate(process)
                raise stderr_task.exception()
            await asyncio.sleep(0.2)
        stdout, stderr = await asyncio.gather(stdout_task, stderr_task)
    finally:
        if process.returncode is None:
            await _terminate(process)
        for task in (stdout_task, stderr_task):
            if not task.done():
                task.cancel()
        await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
    if process.returncode != 0:
        message = await lease.run(_failure_message, stdout, stderr, "aops-cli 返回非零退出码")
        raise CliExecutionError("NON_ZERO_EXIT", message, exit_code=process.returncode, stdout=stdout, stderr=stderr)
    try:
        payload = await lease.run(parse_sql_read_output, stdout)
    except CliExecutionError as exc:
        exc.exit_code = process.returncode
        exc.stdout = stdout
        exc.stderr = stderr
        raise
    return CliResult(payload, stdout, stderr, process.returncode, metadata)


@logged_cli("aops_command")
async def execute_json_command(arguments: list[str], *, api_key: str, timeout_seconds: int) -> CliResult:
    """Execute a fixed aops-cli subcommand and require a successful JSON envelope.

    Callers own the argument allowlist. This helper never invokes a shell and never
    places the credential in argv or logs.
    """
    metadata = await inspect_cli()
    async with cpu_slot() as lease:
        return await _execute_json_command(arguments, api_key=api_key, timeout_seconds=timeout_seconds, metadata=metadata, lease=lease)


async def _execute_json_command(arguments: list[str], *, api_key: str, timeout_seconds: int, metadata: CliMetadata, lease) -> CliResult:
    environment = _cli_environment(api_key)
    process = await asyncio.create_subprocess_exec(
        str(settings.aops_cli_path), *arguments,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        env=environment, start_new_session=True,
    )
    stdout_task = asyncio.create_task(_read_limited(process.stdout, 20 * 1024 * 1024))
    stderr_task = asyncio.create_task(_read_limited(process.stderr, 1024 * 1024))
    try:
        stdout, stderr, _ = await asyncio.wait_for(asyncio.gather(stdout_task, stderr_task, process.wait()), timeout=timeout_seconds)
    except TimeoutError as exc:
        await _terminate(process)
        raise CliExecutionError("TIMEOUT", "aops-cli 获取工单证据超时") from exc
    finally:
        if process.returncode is None:
            await _terminate(process)
        for task in (stdout_task, stderr_task):
            if not task.done():
                task.cancel()
        await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
    if process.returncode != 0:
        message = await lease.run(_failure_message, stdout, stderr, "aops-cli 获取工单证据失败")
        raise CliExecutionError("NON_ZERO_EXIT", message, exit_code=process.returncode, stdout=stdout, stderr=stderr)
    try:
        payload = await lease.run(json.loads, stdout)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CliExecutionError("INVALID_JSON", "aops-cli 获取工单证据时未返回有效 JSON") from exc
    if not isinstance(payload, dict) or payload.get("status") != 0:
        status = payload.get("status") if isinstance(payload, dict) else None
        raise CliExecutionError("AOPS_BUSINESS_ERROR", f"AOPS 返回失败状态：{status!r}")
    return CliResult(payload, stdout, stderr, process.returncode, metadata)
