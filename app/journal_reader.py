"""Read only this service's journal, without shell, sudo or privilege changes."""
from __future__ import annotations

import asyncio
import json
import re
import shutil
import sys
from datetime import UTC, datetime

from app.call_logs import safe_text
from app.cli import redact_diagnostic

UNIT = "itsm-workflow-api.service"
BODY_KEYS = {"authorization", "cookie", "token", "apikey", "aopsapikey", "aopsbottoken", "password", "passwd", "secret", "sql", "sqltext", "sqltemplate", "command", "arguments", "prompt", "messages", "requestbody", "responsebody", "payload", "result", "data", "stdout", "stderr", "body", "request", "response", "choices", "content", "input", "output", "inputs", "outputs", "ticketinfo", "audittimeline", "formdata"}


def redact_raw(text: str, maximum=16384) -> str:
    changed = False
    def scrub(value):
        nonlocal changed
        if isinstance(value, dict):
            output = {}
            for key, item in value.items():
                normalized = re.sub(r"[_-]", "", key.lower())
                if normalized in BODY_KEYS or any(word in normalized for word in ("token", "password", "apikey", "secret")):
                    output[key] = "[敏感字段已隐藏]"
                    changed = True
                else:
                    output[key] = scrub(item)
            return output
        if isinstance(value, list):
            return [scrub(item) for item in value]
        return value
    try:
        value = scrub(json.loads(text))
        if changed:
            text = json.dumps(value, ensure_ascii=False)
    except RecursionError:
        return "[日志结构过深，内容已隐藏]"
    except (ValueError, TypeError):
        pass
    # Also remove configured credentials, including formats not covered by regex.
    from app.config import settings
    for secret in (settings.studio_admin_token, settings.runtime_service_token, settings.tec01_service_token, settings.llm_api_key):
        if secret and secret != "change-me":
            text = text.replace(secret, "***")
    text = re.sub(r"(?i)\b(?:select|insert|update|delete)\s+[^\n]+", "[SQL内容已隐藏]", text)
    text = re.sub(r"(?im)((?:request|response)\s*(?:body|payload)|请求正文|响应正文|提示词)\s*[:=][^\r\n]+", r"\1: [敏感正文已隐藏]", text)
    text = re.sub(r"(?i)(https?://)[^/\s:@]+:[^@\s]+@", r"\1***:***@", text)
    text = re.sub(r"(?im)(cookie\s*[:=]\s*)[^\r\n]+", r"\1***", text)
    text = re.sub(r"(?i)\b(?:ak-|sk-)[A-Za-z0-9._-]{12,}", "***", text)
    # Mask full messages before truncation; otherwise a long SQL/body prefix
    # could be removed while its sensitive tail remains visible.
    text, truncated = redact_diagnostic(text, maximum)
    return text + (" [内容已截断]" if truncated else "")


class OutputLimit(Exception):
    pass


def is_linux():
    return sys.platform == "linux"


async def _bounded(stream, maximum):
    chunks, count = [], 0
    while data := await stream.read(65536):
        count += len(data)
        if count > maximum:
            raise OutputLimit()
        chunks.append(data)
    return b"".join(chunks)


async def read_journal(*, limit=100, keyword="", from_time: datetime | None = None, to_time: datetime | None = None):
    base = {"unit": UNIT, "source": "journalctl", "items": [], "readOnly": True, "limit": limit, "permissionLimited": False}
    if not is_linux():
        return {**base, "available": False, "reason": "NOT_LINUX", "message": "服务控制台日志仅在Linux systemd部署中可用；本地调试请查看应用日志原文。"}
    binary = shutil.which("journalctl")
    if not binary:
        return {**base, "available": False, "reason": "JOURNAL_NOT_FOUND", "message": "服务器未安装journalctl或不是systemd部署，无法读取控制台日志。"}
    args = [binary, "--unit=" + UNIT, "--no-pager", "--output=json", "--reverse", "--lines=" + str(limit)]
    for flag, value in (("--since=", from_time), ("--until=", to_time)):
        if value:
            args.append(flag + value.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S.%f UTC"))
    try:
        process = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env={"PATH": "/usr/bin:/bin", "LANG": "C"})
    except OSError:
        return {**base, "available": False, "reason": "JOURNAL_START_FAILED", "message": "无法启动journalctl，请检查系统安装与执行权限。"}
    tasks = [asyncio.create_task(_bounded(process.stdout, 2 * 1024 * 1024)), asyncio.create_task(_bounded(process.stderr, 65536)), asyncio.create_task(process.wait())]
    try:
        stdout, stderr, code = await asyncio.wait_for(asyncio.gather(*tasks), 5)
    except (TimeoutError, OutputLimit) as exc:
        if process.returncode is None:
            process.kill()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        try:
            await asyncio.wait_for(process.communicate(), 1)
        except TimeoutError:
            pass
        return {**base, "available": False, "reason": "JOURNAL_TIMEOUT" if isinstance(exc, TimeoutError) else "JOURNAL_OUTPUT_LIMIT", "message": "控制台日志读取超时或内容超限，请缩小时间范围后重试。"}
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    diagnostic = stderr.decode("utf-8", "replace").lower()
    limited = any(word in diagnostic for word in ("permission", "not seeing messages", "access denied"))
    if code != 0:
        return {**base, "available": False, "reason": "JOURNAL_PERMISSION_DENIED" if limited else "JOURNAL_READ_FAILED", "message": "服务账号没有完整journal读取权限，请参考页面授权说明。" if limited else "控制台日志读取失败，请在主机上核实systemd日志服务。"}
    rows = []
    for line in stdout.splitlines():
        try:
            entry = json.loads(line)
            message = entry.get("MESSAGE", "")
            if isinstance(message, list) and all(isinstance(value, int) and 0 <= value <= 255 for value in message):
                message = bytes(message).decode("utf-8", "replace")
            message = redact_raw(message if isinstance(message, str) else json.dumps(message, ensure_ascii=False))
            if keyword and keyword.casefold() not in message.casefold():
                continue
            instant = datetime.fromtimestamp(int(entry["__REALTIME_TIMESTAMP"]) / 1000000, UTC)
            if from_time and instant < from_time or to_time and instant >= to_time:
                continue
            timestamp = instant.isoformat()
            rows.append({"timestamp": timestamp, "message": message, "priority": safe_text(entry.get("PRIORITY", "6")), "unit": UNIT})
        except (ValueError, TypeError, KeyError, OverflowError):
            continue
    return {**base, "items": rows, "available": True, "permissionLimited": limited, "message": "当前账号仅能读取部分journal，可能缺少系统启动或异常日志。" if limited else "仅展示所选时间范围最新一批；关键词只过滤本批，不搜索整机日志。"}
