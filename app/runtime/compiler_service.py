from __future__ import annotations

import asyncio
import copy
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from app.extraction import compile_ticket_evidence, validate_ticket_evidence
from app.runtime.planner import canonical_hash, validate_workflow
from app.tec01_client import Tec01Client, Tec01Error


EventSink = Callable[[dict[str, Any]], Awaitable[None]]


class CompilerService:
    """Asynchronous compiler with temporary status only; tec01/browser owns storage."""

    def __init__(self, client: Tec01Client | None = None, *, max_completed: int = 64, retention_seconds: float = 3600):
        self.client = client or Tec01Client()
        self.max_completed = max_completed
        self.retention_seconds = retention_seconds
        self.tasks: dict[str, asyncio.Task] = {}
        self.statuses: dict[str, str] = {}
        self.records: dict[str, dict[str, Any]] = {}

    def _cleanup(self) -> None:
        now = time.monotonic()
        completed = sorted(((key, value) for key, value in self.records.items() if key not in self.tasks), key=lambda item: item[1]["_updated"])
        overflow = max(0, len(completed) - self.max_completed)
        for index, (key, value) in enumerate(completed):
            if index < overflow or now - value["_updated"] > self.retention_seconds:
                self.records.pop(key, None)
                self.statuses.pop(key, None)

    def get(self, knowledge_id: str) -> dict[str, Any] | None:
        self._cleanup()
        value = self.records.get(knowledge_id)
        return copy.deepcopy({key: item for key, item in value.items() if not key.startswith("_")}) if value else None

    def submit(self, job: dict[str, Any], *, event_sink: EventSink | None = None, sync_tec01: bool = True) -> bool:
        self._cleanup()
        knowledge_id = str(job.get("knowledgeId") or "").strip()
        request_id = str(job.get("requestId") or "").strip()
        if not knowledge_id or not request_id:
            raise ValueError("knowledgeId和requestId不能为空")
        if "ticketId" not in job:
            raise ValueError("ticketId必须是正整数")
        validate_ticket_evidence(job.get("ticketInfo"), job.get("auditTimeline"), job.get("ticketId"))
        fingerprint = canonical_hash({key: job.get(key) for key in ("ticketId", "ticketInfo", "auditTimeline")})
        previous = self.records.get(knowledge_id)
        if previous:
            if previous["requestId"] == request_id:
                if previous["_fingerprint"] != fingerprint:
                    raise ValueError("EXTRACTION_CONFLICT：相同requestId不能提交不同证据")
                # A network retry may re-deliver an unconfirmed final callback,
                # but must never re-run extraction for this identity.
                if sync_tec01 and previous.get("syncStatus") == "FAILED" and previous.get("_finalCallback") and knowledge_id not in self.tasks:
                    task = asyncio.create_task(self._redeliver_final(knowledge_id), name="compiler-redelivery:" + knowledge_id)
                    self.tasks[knowledge_id] = task
                    task.add_done_callback(lambda completed: self.tasks.pop(knowledge_id, None) if self.tasks.get(knowledge_id) is completed else None)
                return False
            if knowledge_id in self.tasks or previous["status"] != "FAILED":
                raise ValueError("EXTRACTION_CONFLICT：该经验已受理，请使用原requestId重试查询")
        self.records[knowledge_id] = {"knowledgeId": knowledge_id, "requestId": request_id, "status": "ACCEPTED", "syncStatus": "PENDING" if sync_tec01 else "DISABLED", "seq": previous["seq"] if previous else 0, "events": [], "_fingerprint": fingerprint, "_updated": time.monotonic()}
        self.statuses[knowledge_id] = "ACCEPTED"
        task = asyncio.create_task(self._run(copy.deepcopy(job), event_sink, sync_tec01), name="compiler:" + knowledge_id)
        self.tasks[knowledge_id] = task

        def finished(completed: asyncio.Task) -> None:
            if self.tasks.get(knowledge_id) is completed:
                self.tasks.pop(knowledge_id, None)
            self._cleanup()

        task.add_done_callback(finished)
        return True

    async def shutdown(self) -> None:
        tasks = list(self.tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _deliver(self, knowledge_id: str, event: dict[str, Any]) -> None:
        record = self.records[knowledge_id]
        try:
            await self.client.extraction_callback(knowledge_id, event)
            record["syncStatus"] = "SYNCED"
            record.pop("callbackError", None)
        except Tec01Error as exc:
            # Knowledge extraction is side-effect free. Keep its result available
            # to the browser even if the optional state synchronization fails.
            record["syncStatus"] = "FAILED"
            record["callbackError"] = {"code": exc.code, "message": "tec01提取回调未确认，请检查连接后重试提交", "retryable": exc.retryable}

    async def _redeliver_final(self, knowledge_id: str) -> None:
        record = self.records[knowledge_id]
        await self._deliver(knowledge_id, record["_finalCallback"])
        record["_updated"] = time.monotonic()

    async def _run(self, job: dict[str, Any], event_sink: EventSink | None, sync_tec01: bool) -> None:
        knowledge_id = str(job["knowledgeId"])
        record = self.records[knowledge_id]
        lock = asyncio.Lock()

        async def emit(payload: dict[str, Any]) -> None:
            # Keep stage and final events ordered, including their delivery.
            async with lock:
                record["seq"] += 1
                event = {**payload, "requestId": job["requestId"], "seq": record["seq"], "occurredAt": datetime.now(UTC).isoformat()}
                record["_updated"] = time.monotonic()
                record["events"].append(copy.deepcopy(event))
                record["events"] = record["events"][-256:]
                if event["type"] == "FINISHED":
                    record["_finalCallback"] = copy.deepcopy(event)
                    if sync_tec01:
                        record["syncStatus"] = "PENDING"
                if event_sink:
                    await event_sink({"knowledgeId": knowledge_id, **copy.deepcopy(event)})
                if sync_tec01:
                    await self._deliver(knowledge_id, event)

        async def progress(stage: str, message: str, current: int | None, total: int | None) -> None:
            record["status"] = stage
            self.statuses[knowledge_id] = stage
            await emit({"type": "PROGRESS", "stage": stage, "message": message, "progress": {"current": current, "total": total}})

        try:
            proposal, diagnostics = await compile_ticket_evidence(job["ticketInfo"], job["auditTimeline"], progress=progress)
            validated = validate_workflow(proposal["workflowDefinition"], "DRAFT")
            proposal["workflowDefinition"] = validated["normalizedDefinition"]
            validation = {key: validated[key] for key in ("catalogDigest", "workflowContentHash", "valid")}
            record.update({"status": "SUCCEEDED", "proposal": proposal, "validation": validation, "diagnostics": diagnostics})
            self.statuses[knowledge_id] = "SUCCEEDED"
            await emit({"type": "FINISHED", "status": "SUCCEEDED", "proposal": proposal, "validation": validation, "diagnostics": diagnostics})
        except asyncio.CancelledError:
            record["status"] = "INTERRUPTED"
            self.statuses[knowledge_id] = "INTERRUPTED"
            raise
        except Tec01Error as exc:
            # Transport failure is not a second compilation terminal event.
            record["callbackError"] = {"code": exc.code, "message": "tec01提取回调未确认，请检查连接后重新提交", "retryable": exc.retryable}
            if record["status"] != "SUCCEEDED":
                record["status"] = "FAILED"
                self.statuses[knowledge_id] = "FAILED"
        except Exception as exc:
            error = self._safe_error(exc)
            record.update({"status": "FAILED", "error": error})
            self.statuses[knowledge_id] = "FAILED"
            try:
                await emit({"type": "FINISHED", "status": "FAILED", "error": error})
            except Tec01Error as callback_error:
                record["callbackError"] = {"code": callback_error.code, "message": "tec01提取失败回调未确认", "retryable": callback_error.retryable}
        finally:
            record["_updated"] = time.monotonic()

    @staticmethod
    def _safe_error(exc: Exception) -> dict[str, Any]:
        # HTTP bodies, prompts and ticket values never enter error diagnostics.
        text = str(exc)
        if text.startswith("NO_VALID_OPERATIONS"):
            return {"code": "NO_VALID_OPERATIONS", "message": "没有成功且安全的 SQL 读操作", "retryable": False}
        if text.startswith(("LLM_ANALYSIS_FAILED", "LLM_ANALYSIS_UNAVAILABLE")):
            return {"code": "LLM_ANALYSIS_FAILED", "message": "模型未返回有效的结构化结果，请检查内网模型配置后重试", "retryable": True}
        return {"code": "COMPILER_FAILED", "message": "草稿编译或校验失败，请检查输入证据及节点定义后重试", "retryable": False}


compiler_service = CompilerService()
