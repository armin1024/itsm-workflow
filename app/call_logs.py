"""Best-effort, bounded operational logs. Never a workflow state store."""
from __future__ import annotations

import asyncio
import base64
import contextvars
import functools
import hashlib
import json
import math
import logging
import os
import queue
import re
import stat
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from app.config import settings
from app.log_descriptions import describe

_context = contextvars.ContextVar("call_log_context", default={})
_secrets = contextvars.ContextVar("call_log_secrets", default=())
FIELDS = {"traceId", "requestId", "runId", "dispatchId", "knowledgeId", "nodeId", "attemptId", "ticketId", "spanId", "callId", "direction", "peer", "operation", "method", "url", "stage", "businessStatus", "businessErrorCode", "httpStatus", "exitCode", "durationMs", "attempt", "maxAttempts", "retryDelaySeconds", "errorCode", "model", "acknowledged"}
FILE_PATTERN = re.compile(r"^calls-\d{8}T\d{12}Z-[a-f0-9]{12}\.jsonl$")
LEVELS = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40}


class LogCursorExpired(ValueError):
    pass


def utcnow() -> datetime:
    return datetime.now(UTC)


@contextmanager
def log_context(**values):
    token = _context.set({**_context.get(), **{key: value for key, value in values.items() if key in FIELDS}})
    try:
        yield
    finally:
        _context.reset(token)


@contextmanager
def hide_credentials(*values):
    token = _secrets.set((*_secrets.get(), *(value for value in values if isinstance(value, str) and value)))
    try:
        yield
    finally:
        _secrets.reset(token)


def safe_text(value: Any) -> str:
    text = str(value)
    for secret in (*_secrets.get(), settings.tec01_service_token, settings.runtime_service_token, settings.studio_admin_token, settings.llm_api_key):
        if secret and secret not in {"change-me"}:
            text = text.replace(secret, "***")
    text = re.sub(r"(?i)Bearer\s+\S+", "Bearer ***", text)
    text = re.sub(r"(?i)(?:ak-|sk-)[A-Za-z0-9._-]{12,}", "***", text)
    return "".join(c if ord(c) >= 32 and ord(c) != 127 else " " for c in text)[:500]


def safe_url(value: str) -> str:
    try:
        parts = urlsplit(value)
        return safe_text(urlunsplit((parts.scheme, parts.netloc.rsplit("@", 1)[-1], parts.path, "", "")))
    except ValueError:
        return "[invalid-url]"


def error_code(exc: BaseException) -> str:
    # Do not trust exception text or remote-provided error codes.
    code = getattr(exc, "code", "")
    allowed = {"CLI_NOT_FOUND", "CLI_VERSION_FAILED", "CLI_VERSION_MISMATCH", "NON_ZERO_EXIT", "TIMEOUT", "CANCELLED", "OUTPUT_LIMIT_EXCEEDED", "INVALID_JSON", "AOPS_BUSINESS_ERROR", "TEC01_ACK_INVALID", "TEC01_INVALID_RESPONSE", "TEC01_ACK_NOT_SAVED", "TEC01_ACK_MISMATCH", "TEC01_EXTRACTION_ACK_INVALID", "TEC01_EXTRACTION_ACK_MISMATCH", "TEC01_ACK_WAIT_EXCEEDED", "TEC01_NOT_CONFIGURED", "TEC01_TIMEOUT", "TEC01_UNAVAILABLE"}
    return code if code in allowed else "CALL_FAILED"


@contextmanager
def call_span(**fields):
    start = time.monotonic()
    outcome: dict[str, Any] = {"spanId": uuid.uuid4().hex}
    with log_context(**fields, spanId=outcome["spanId"]):
        emit("CALL_STARTED")
        try:
            yield outcome
        except BaseException as exc:
            outcome["errorCode"] = error_code(exc)
            raise
        finally:
            failed = bool(outcome.get("errorCode"))
            emit("CALL_FAILED" if failed else "CALL_COMPLETED", "WARNING" if failed else "INFO", **outcome, durationMs=round((time.monotonic() - start) * 1000, 2))


class CallLogStore:
    def __init__(self, directory: Path, *, retention_days=7, max_bytes=512 * 1024 * 1024, file_bytes=16 * 1024 * 1024, cleanup_seconds=60, queue_size=2048, level="INFO", console=None):
        self.directory = Path(directory).absolute()
        self.retention_days, self.max_bytes, self.file_bytes = retention_days, max_bytes, min(file_bytes, max_bytes)
        self.cleanup_seconds, self.level = cleanup_seconds, LEVELS[level]
        self.console = console if console is not None else sys.stdout
        self.queue: queue.Queue = queue.Queue(maxsize=queue_size)
        self.lock = threading.RLock()
        self.stopping = threading.Event()
        self.thread: threading.Thread | None = None
        self.active: Path | None = None
        self.day = ""
        self.dropped = 0
        self.write_errors = 0
        self.cleanup_errors = 0
        self.last_error: str | None = None
        self.last_cleanup: str | None = None
        self.last_deleted = 0
        self._last_warning = 0.0
        self._last_maintenance = 0.0
        self._query_lock = threading.Lock()

    def start(self):
        self.stopping.clear()
        self.cleanup()
        self.thread = threading.Thread(target=self._worker, name="call-log-writer", daemon=True)
        try:
            self.thread.start()
        except RuntimeError:
            self.write_errors += 1
            self._warning("LOG_WRITER_START_FAILED")

    def stop(self, timeout=3):
        self.stopping.set()
        if self.thread and self.thread.ident is not None:
            self.thread.join(timeout)

    def _warning(self, code: str):
        self.last_error = code
        if time.monotonic() - self._last_warning > 30:
            self._last_warning = time.monotonic()
            try:
                sys.stderr.write(json.dumps({"timestamp": utcnow().isoformat(), "level": "ERROR", "event": "LOG_STORAGE_WARNING", "errorCode": code}) + "\n")
            except Exception:
                pass

    def emit(self, event: str, level="INFO", **fields):
        try:
            if LEVELS.get(level, 20) < self.level:
                return
            record = {"timestamp": utcnow().isoformat(), "logId": uuid.uuid4().hex, "level": level, "event": event}
            for key, value in {**_context.get(), **fields}.items():
                if key in FIELDS and type(value) in (str, bool, int, float) and not (isinstance(value, float) and not math.isfinite(value)):
                    record[key] = value if type(value) in (bool, int, float) else safe_url(str(value)) if key == "url" else safe_text(value)
            label, description = describe(record)
            record["operationLabel"], record["operationDescription"] = label, description
            self.queue.put_nowait(record)
        except queue.Full:
            self.dropped += 1
            self._warning("LOG_QUEUE_FULL")
        except Exception:
            self.dropped += 1
            self._warning("LOG_ENQUEUE_FAILED")

    def _files(self) -> list[Path]:
        if not self.directory.exists():
            return []
        if self.directory.is_symlink() or any(p.is_symlink() for p in self.directory.parents):
            raise OSError("symlink log directory")
        return sorted((p for p in self.directory.iterdir() if FILE_PATTERN.fullmatch(p.name) and not p.is_symlink() and p.is_file()), key=lambda p: p.name)

    def _open_read(self, path: Path):
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            os.close(fd)
            raise OSError("not regular file")
        return os.fdopen(fd, "rb")

    def cleanup(self, reserve=0, now: datetime | None = None):
        with self.lock:
            try:
                current = now or utcnow()
                self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
                files = self._files()
                deleted = 0
                cutoff = current - timedelta(days=self.retention_days)
                for path in files:
                    created = datetime.strptime(path.name[6:28], "%Y%m%dT%H%M%S%fZ").replace(tzinfo=UTC)
                    if path != self.active and created < cutoff:
                        path.unlink()
                        deleted += 1
                files = self._files()
                size = sum(path.stat().st_size for path in files)
                for path in files:
                    if size + reserve <= self.max_bytes:
                        break
                    if path == self.active:
                        continue
                    length = path.stat().st_size
                    path.unlink()
                    size -= length
                    deleted += 1
                if size + reserve > self.max_bytes:
                    raise OSError("log capacity unavailable")
                self.last_cleanup, self.last_deleted = current.isoformat(), deleted
                self._last_maintenance = time.monotonic()
            except Exception:
                self.cleanup_errors += 1
                self._warning("LOG_CLEANUP_FAILED")

    def _write(self, record: dict):
        encoded = (json.dumps(record, ensure_ascii=False, allow_nan=False, separators=(",", ":")) + "\n").encode("utf-8")
        try:
            self.console.write(encoded.decode("utf-8"))
            self.console.flush()
        except Exception:
            self._warning("LOG_CONSOLE_FAILED")
        with self.lock:
            try:
                now = utcnow()
                if self.active is None or self.day != now.strftime("%Y%m%d") or not self.active.exists() or self.active.stat().st_size + len(encoded) > self.file_bytes:
                    self.active = None
                    self.cleanup(reserve=len(encoded), now=now)
                    self.active = self.directory / f"calls-{now.strftime('%Y%m%dT%H%M%S%fZ')}-{uuid.uuid4().hex[:12]}.jsonl"
                    self.day = now.strftime("%Y%m%d")
                files = self._files()
                if sum(path.stat().st_size for path in files) + len(encoded) > self.max_bytes:
                    self.cleanup(reserve=len(encoded), now=now)
                if sum(path.stat().st_size for path in self._files()) + len(encoded) > self.max_bytes or len(encoded) > self.file_bytes:
                    raise OSError("log capacity exceeded")
                fd = os.open(self.active, os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
                with os.fdopen(fd, "ab") as stream:
                    stream.write(encoded)
            except Exception:
                self.write_errors += 1
                self.dropped += 1
                self._warning("LOG_WRITE_FAILED")

    def _worker(self):
        while not self.stopping.is_set() or not self.queue.empty():
            try:
                record = self.queue.get(timeout=min(self.cleanup_seconds, 0.25))
            except queue.Empty:
                record = None
            if time.monotonic() - self._last_maintenance >= self.cleanup_seconds:
                # Close the daily segment even while idle, so expired data can be removed.
                with self.lock:
                    if self.day != utcnow().strftime("%Y%m%d"):
                        self.active = None
                self.cleanup()
            if record is not None:
                self._write(record)
                self.queue.task_done()

    def status(self):
        with self.lock:
            try:
                files = self._files()
                size = sum(path.stat().st_size for path in files)
                available = self.directory.is_dir() and not self.directory.is_symlink() and os.access(self.directory, os.W_OK) and (self.thread is None or self.thread.is_alive())
            except OSError:
                files, size, available = [], 0, False
            return {"directory": str(self.directory), "level": next(key for key, value in LEVELS.items() if value == self.level), "retentionDays": self.retention_days, "maxTotalBytes": self.max_bytes, "maxFileBytes": self.file_bytes, "cleanupIntervalSeconds": self.cleanup_seconds, "usedBytes": size, "fileCount": len(files), "lastCleanupAt": self.last_cleanup, "lastDeletedFiles": self.last_deleted, "writeErrors": self.write_errors, "cleanupErrors": self.cleanup_errors, "droppedRecords": self.dropped, "queuedRecords": self.queue.qsize(), "lastError": self.last_error, "available": available, "consoleRetention": "managed-by-system-journald"}

    def query(self, *, limit=100, cursor=None, raw=False, **filters):
        # Queries are bounded in both memory and bytes scanned, never getAll().
        with self._query_lock, self.lock:
            try:
                with self.lock:
                    files = list(reversed(self._files()))
            except OSError:
                return {"items": [], "nextCursor": None, "scannedBytes": 0, "unavailable": True}
            position = None
            filter_hash = hashlib.sha256(json.dumps(filters, sort_keys=True).encode()).hexdigest()[:16]
            if cursor:
                try:
                    decoded = json.loads(base64.urlsafe_b64decode(cursor))
                    name, offset, filter_id = decoded
                    if not isinstance(name, str) or not FILE_PATTERN.fullmatch(name) or type(offset) is not int or offset < 0 or filter_id != filter_hash:
                        raise ValueError()
                    position = next(i for i, path in enumerate(files) if path.name == name)
                    if offset > files[position].stat().st_size:
                        raise ValueError()
                except Exception as exc:
                    raise LogCursorExpired("日志游标已失效，请从最新日志重新查询") from exc
                files = files[position:]
            else:
                offset = files[0].stat().st_size if files else 0
            items, scanned, raw_lines = [], 0, []
            budget = 8 * 1024 * 1024
            for index, path in enumerate(files):
                try:
                    with self._open_read(path) as stream:
                        end = min(offset, os.fstat(stream.fileno()).st_size) if index == 0 else os.fstat(stream.fileno()).st_size
                        while end > 0 and scanned < budget and len(items) < limit:
                            if budget - scanned < 65536 and end > budget - scanned:
                                break
                            start = max(0, end - min(65536, budget - scanned))
                            stream.seek(start)
                            chunk = stream.read(end - start)
                            scanned += len(chunk)
                            # A 64KiB block always exceeds the maximum emitted record.
                            if start:
                                boundary = chunk.find(b"\n")
                                if boundary < 0:
                                    end = start
                                    continue
                                start += boundary + 1
                                chunk = chunk[boundary + 1:]
                            segments = chunk.splitlines(keepends=True)
                            for line in reversed(segments):
                                end -= len(line)
                                try:
                                    record = json.loads(line)
                                    if isinstance(record, dict):
                                        label, description = describe(record)
                                        record.setdefault("operationLabel", label)
                                        record.setdefault("operationDescription", description)
                                    if self._matches(record, filters):
                                        items.append(record)
                                        if raw:
                                            raw_lines.append({"fileName": path.name, "text": line.decode("utf-8", "replace").rstrip("\r\n"), "logId": record["logId"], "timestamp": record["timestamp"]})
                                except (ValueError, TypeError):
                                    continue
                                if len(items) >= limit:
                                    break
                            if end == start and not chunk:
                                end = max(0, start - 1)
                        if end > 0:
                            return {"items": items, "rawLines": raw_lines, "nextCursor": self._cursor(path.name, end, filter_hash), "scannedBytes": scanned}
                except FileNotFoundError as exc:
                    raise LogCursorExpired("日志已被自动清理，请重新查询") from exc
                except OSError:
                    continue
                if scanned >= budget:
                    if index + 1 < len(files):
                        nxt = files[index + 1]
                        return {"items": items, "rawLines": raw_lines, "nextCursor": self._cursor(nxt.name, nxt.stat().st_size, filter_hash), "scannedBytes": scanned}
                    break
            return {"items": items, "rawLines": raw_lines, "nextCursor": None, "scannedBytes": scanned}

    @staticmethod
    def _cursor(name, offset, filter_id):
        return base64.urlsafe_b64encode(json.dumps([name, offset, filter_id]).encode()).decode()

    @staticmethod
    def _matches(record, filters):
        if not isinstance(record, dict) or not {"timestamp", "event", "logId", "level"}.issubset(record):
            return False
        for key in ("level", "peer", "direction", "event", "operation", "runId", "knowledgeId", "nodeId", "requestId", "spanId", "callId", "traceId"):
            if filters.get(key) and record.get(key) != filters[key]:
                return False
        if filters.get("fromTime") and record["timestamp"] < filters["fromTime"]:
            return False
        if filters.get("toTime") and record["timestamp"] >= filters["toTime"]:
            return False
        return not filters.get("keyword") or filters["keyword"].casefold() in json.dumps(record, ensure_ascii=False).casefold()


call_logs: CallLogStore | None = None


def emit(event: str, level="INFO", **fields):
    if call_logs is not None:
        call_logs.emit(event, level, **fields)


def initialize():
    global call_logs
    # Raw access logs include query strings; network debug logs may include URLs.
    logging.getLogger("uvicorn.access").disabled = True
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    call_logs = CallLogStore(settings.log_dir, retention_days=settings.log_retention_days, max_bytes=settings.log_max_total_mb * 1024 * 1024, file_bytes=settings.log_max_file_mb * 1024 * 1024, cleanup_seconds=settings.log_cleanup_interval_seconds, level=settings.log_level)
    call_logs.start()


def logged_cli(operation):
    def decorate(function):
        @functools.wraps(function)
        async def wrapped(*args, **kwargs):
            ticket = kwargs.get("ticket_id")
            arguments = args[0] if args and isinstance(args[0], list) else []
            if not ticket and "--id" in arguments:
                candidate = arguments[arguments.index("--id") + 1:][:1]
                ticket = int(candidate[0]) if candidate and str(candidate[0]).isdigit() else None
            op = operation
            if arguments[:2] == ["event-center", "info"]:
                op = "ticket_info"
            elif arguments[:2] == ["event-center", "audit_timeline"]:
                op = "audit_timeline"
            start = time.monotonic()
            with hide_credentials(kwargs.get("api_key")), log_context(peer="AOPS_CLI", direction="OUTBOUND", operation=op, method="CLI", ticketId=ticket, spanId=uuid.uuid4().hex):
                emit("CALL_STARTED")
                try:
                    result = await function(*args, **kwargs)
                except BaseException as exc:
                    emit("CALL_FAILED", "ERROR", errorCode=error_code(exc), exitCode=getattr(exc, "exit_code", None), durationMs=round((time.monotonic() - start) * 1000, 2))
                    raise
                emit("CALL_COMPLETED", exitCode=result.exit_code, durationMs=round((time.monotonic() - start) * 1000, 2))
                return result
        return wrapped
    return decorate
