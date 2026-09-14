from __future__ import annotations

import asyncio
import hashlib
import json
import os
import signal
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable

from app.config import settings


class CliExecutionError(RuntimeError):
    def __init__(self, code: str, message: str, *, exit_code: int | None = None):
        super().__init__(message)
        self.code = code
        self.exit_code = exit_code


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
        if not isinstance(payload, dict) or payload.get("status") != 0:
            status = payload.get("status") if isinstance(payload, dict) else None
            raise CliExecutionError("AOPS_BUSINESS_ERROR", f"AOPS 返回失败状态：{status!r}")
        return payload

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
    if not events or not any(name == "done" for name, _ in events):
        raise CliExecutionError("INVALID_SSE", "aops-cli SQL 读 SSE 缺少 done 终止事件")

    titles: list[str] = []
    fields: list[dict] = []
    messages: list[object] = []
    stream_uuid: str | None = None
    done_value: str | None = None
    for name, data in events:
        value = data.strip()
        if name in {"error", "failed", "failure"}:
            raise CliExecutionError("AOPS_BUSINESS_ERROR", f"AOPS SQL 读 SSE 返回错误事件：{value[:300]}")
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
        raise CliExecutionError("AOPS_BUSINESS_ERROR", f"AOPS SQL 读 SSE 未成功结束：{done_value or '<empty>'}")

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


async def inspect_cli(path: Path | None = None) -> CliMetadata:
    executable = path or settings.aops_cli_path
    if not executable.is_file() or not os.access(executable, os.X_OK):
        raise CliExecutionError("CLI_NOT_FOUND", f"aops-cli 不可执行：{executable}")
    digest = hashlib.sha256(executable.read_bytes()).hexdigest()
    process = await asyncio.create_subprocess_exec(str(executable), "--version", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    stdout, _ = await asyncio.wait_for(process.communicate(), timeout=10)
    if process.returncode != 0:
        raise CliExecutionError("CLI_VERSION_FAILED", "无法读取 aops-cli 版本", exit_code=process.returncode)
    version = stdout.decode("utf-8", "replace").strip()[:120]
    if settings.aops_cli_version_requirement and settings.aops_cli_version_requirement not in version:
        raise CliExecutionError("CLI_VERSION_MISMATCH", f"aops-cli 版本不满足要求：{settings.aops_cli_version_requirement}")
    return CliMetadata(version, digest)


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
    environment = {
        "AOPS_BASE_URL": settings.aops_base_url,
        "AOPS_API_KEY": api_key,
        "LANG": os.environ.get("LANG", "C.UTF-8"),
    }
    for key in ("SSL_CERT_FILE", "SSL_CERT_DIR", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy"):
        if key in os.environ:
            environment[key] = os.environ[key]
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
    if process.returncode != 0:
        raise CliExecutionError("NON_ZERO_EXIT", "aops-cli 返回非零退出码", exit_code=process.returncode)
    try:
        payload = parse_sql_read_output(stdout)
    except CliExecutionError as exc:
        exc.exit_code = process.returncode
        raise
    return CliResult(payload, stdout, stderr, process.returncode, metadata)


async def execute_json_command(arguments: list[str], *, api_key: str, timeout_seconds: int) -> CliResult:
    """Execute a fixed aops-cli subcommand and require a successful JSON envelope.

    Callers own the argument allowlist. This helper never invokes a shell and never
    places the credential in argv or logs.
    """
    metadata = await inspect_cli()
    environment = {
        "AOPS_BASE_URL": settings.aops_base_url,
        "AOPS_API_KEY": api_key,
        "LANG": os.environ.get("LANG", "C.UTF-8"),
    }
    for key in ("SSL_CERT_FILE", "SSL_CERT_DIR", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy"):
        if key in os.environ:
            environment[key] = os.environ[key]
    process = await asyncio.create_subprocess_exec(
        str(settings.aops_cli_path), *arguments,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        env=environment, start_new_session=True,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout_seconds)
    except TimeoutError as exc:
        await _terminate(process)
        raise CliExecutionError("TIMEOUT", "aops-cli 获取工单证据超时") from exc
    if len(stdout) > 20 * 1024 * 1024 or len(stderr) > 1024 * 1024:
        raise CliExecutionError("OUTPUT_LIMIT_EXCEEDED", "aops-cli 获取工单证据的输出超过限制")
    if process.returncode != 0:
        raise CliExecutionError("NON_ZERO_EXIT", "aops-cli 获取工单证据失败", exit_code=process.returncode)
    try:
        payload = json.loads(stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CliExecutionError("INVALID_JSON", "aops-cli 获取工单证据时未返回有效 JSON") from exc
    if not isinstance(payload, dict) or payload.get("status") != 0:
        status = payload.get("status") if isinstance(payload, dict) else None
        raise CliExecutionError("AOPS_BUSINESS_ERROR", f"AOPS 返回失败状态：{status!r}")
    return CliResult(payload, stdout, stderr, process.returncode, metadata)
