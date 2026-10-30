from __future__ import annotations

import asyncio
import copy
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from app.config import settings
from app.extraction import compile_ticket_evidence, validate_evidence_limits, extraction_outcome
from app.resources import ResourceError, cpu_run, data_budget, object_size
from app.runtime.planner import canonical_hash, validate_workflow
from app.tec01_client import Tec01Client, Tec01Error
from app.compiler_errors import failure_details, log_compiler_failure


EventSink = Callable[[dict[str, Any]], Awaitable[None]]


@dataclass
class CompilerReservation:
    service: Any
    owner: str
    active: bool = False
    transferred: bool = False


class CompilerService:
    """Bounded compilation; tec01/browser remains the owner of durable data."""

    def __init__(self, client: Tec01Client | None = None, *, max_completed: int = 64, retention_seconds: float = 3600, max_cache_bytes: int | None = None):
        self.client = client or Tec01Client()
        self.max_completed, self.retention_seconds = max_completed, retention_seconds
        self.max_cache_bytes = max_cache_bytes
        self.tasks: dict[str, asyncio.Task] = {}
        self.statuses: dict[str, str] = {}
        self.records: dict[str, dict[str, Any]] = {}
        self._identity = uuid.uuid4().hex
        self._reservations: dict[str, CompilerReservation] = {}
        self._cache_sizes: dict[str, int] = {}

    @property
    def cache_bytes(self) -> int:
        return sum(self._cache_sizes.values())

    @property
    def active_jobs(self) -> int:
        return len(self._reservations)

    def _cache_owner(self, knowledge_id: str) -> str:
        return f"compiler-cache:{self._identity}:{knowledge_id}"

    def _evict(self, knowledge_id: str) -> None:
        self.records.pop(knowledge_id, None)
        self.statuses.pop(knowledge_id, None)
        self._cache_sizes.pop(knowledge_id, None)
        data_budget.release(self._cache_owner(knowledge_id))

    def _cleanup(self) -> None:
        now = time.monotonic()
        limit = self.max_cache_bytes if self.max_cache_bytes is not None else settings.compiler_cache_max_mb * 1024 * 1024
        completed = sorted(((key, value) for key, value in self.records.items() if key not in self.tasks), key=lambda item: item[1]["_updated"])
        remaining = len(completed)
        for key, value in completed:
            if remaining > self.max_completed or now - value["_updated"] > self.retention_seconds or self.cache_bytes > limit:
                self._evict(key)
                remaining -= 1

    @staticmethod
    def _public(record: dict[str, Any]) -> dict[str, Any]:
        return {key: value for key, value in record.items() if not key.startswith("_")}

    def get(self, knowledge_id: str) -> dict[str, Any] | None:
        """Compatibility helper; HTTP routes use get_async for off-loop copying."""
        self._cleanup()
        record = self.records.get(knowledge_id)
        return copy.deepcopy(self._public(record)) if record else None

    async def get_async(self, knowledge_id: str) -> dict[str, Any] | None:
        self._cleanup()
        record = self.records.get(knowledge_id)
        return await cpu_run(copy.deepcopy, self._public(record)) if record else None

    def sync_state(self, knowledge_id: str) -> dict[str, Any] | None:
        """Polling needs only small transport status, never a copied proposal."""
        self._cleanup()
        record = self.records.get(knowledge_id)
        return {key: copy.deepcopy(record[key]) for key in ("syncStatus", "callbackError") if key in record} if record else None

    def _claim(self) -> CompilerReservation:
        self._cleanup()
        if len(self._reservations) >= settings.compiler_max_active_jobs:
            raise ResourceError("COMPILER_NO_CAPACITY", "提取任务容量已满，请稍后重试", status_code=429, retryable=True)
        token = CompilerReservation(self, f"compiler-active:{self._identity}:{uuid.uuid4().hex}", active=True)
        data_budget.reserve(token.owner, 0)
        self._reservations[token.owner] = token
        return token

    def _release(self, token: CompilerReservation) -> None:
        if token.active:
            self._reservations.pop(token.owner, None)
            data_budget.release(token.owner)
            token.active = False

    @staticmethod
    def _prepare(job: dict[str, Any]) -> tuple[str, str, str, int]:
        knowledge_id, request_id = str(job.get("knowledgeId") or "").strip(), str(job.get("requestId") or "").strip()
        if not knowledge_id or not request_id:
            raise ValueError("knowledgeId和requestId不能为空")
        if "ticketId" not in job:
            raise ValueError("ticketId必须是正整数")
        length = validate_evidence_limits(job.get("ticketInfo"), job.get("auditTimeline"), job.get("ticketId"))
        fingerprint = canonical_hash({key: job.get(key) for key in ("ticketId", "ticketInfo", "auditTimeline")})
        return knowledge_id, request_id, fingerprint, length

    def _duplicate(self, prepared: tuple[str, str, str, int]) -> bool:
        knowledge_id, request_id, fingerprint, _length = prepared
        previous = self.records.get(knowledge_id)
        if not previous:
            return False
        if previous["requestId"] == request_id:
            if previous["_fingerprint"] != fingerprint:
                raise ValueError("EXTRACTION_CONFLICT：相同requestId不能提交不同证据")
            return True
        if knowledge_id in self.tasks or previous["status"] != "FAILED":
            raise ValueError("EXTRACTION_CONFLICT：该经验已受理，请使用原requestId重试查询")
        return False

    @asynccontextmanager
    async def admission(self, job: dict[str, Any] | None = None, **_context):
        """Reserve before ticket CLI/preview; transferred submissions keep the slot."""
        prepared = await cpu_run(self._prepare, job) if job is not None else None
        self._cleanup()
        duplicate = prepared is not None and self._duplicate(prepared)
        token = CompilerReservation(self, "") if duplicate else self._claim()
        try:
            yield token
        finally:
            if not token.transferred:
                self._release(token)

    async def charge_evidence(self, token: CompilerReservation, ticket, rows):
        """Preview owns evidence too, even when no background job is launched."""
        if token.service is not self or not token.active or token.transferred:
            raise ValueError("提取容量预留无效")
        length = await cpu_run(validate_evidence_limits, ticket, rows)
        data_budget.reserve(token.owner, length * 2)

    def _redeliver_if_needed(self, knowledge_id: str, sync_tec01: bool) -> None:
        previous = self.records[knowledge_id]
        if sync_tec01 and previous.get("syncStatus") == "FAILED" and previous.get("_finalCallback") and knowledge_id not in self.tasks:
            # Idempotent submit still succeeds when a new compilation owns the
            # only slot; cached final can be re-delivered on a later submit.
            try:
                token = self._claim()
            except ResourceError:
                return
            token.transferred = True
            work = self._redeliver_final(knowledge_id, token)
            try:
                task = asyncio.create_task(work, name="compiler-redelivery:" + knowledge_id)
            except BaseException:
                work.close()
                self._release(token)
                raise
            self._track(knowledge_id, task, token)

    def _track(self, knowledge_id: str, task: asyncio.Task, token: CompilerReservation) -> None:
        self.tasks[knowledge_id] = task

        def finished(completed: asyncio.Task) -> None:
            # Cancellation before the coroutine's first instruction bypasses
            # its try/finally. Done cleanup is deliberately idempotent.
            self._release(token)
            if self.tasks.get(knowledge_id) is completed:
                self.tasks.pop(knowledge_id, None)
            if completed.cancelled() and knowledge_id in self.records:
                self.records[knowledge_id]["status"] = "INTERRUPTED"
                self.statuses[knowledge_id] = "INTERRUPTED"
            if not completed.cancelled():
                completed.exception()
            self._cleanup()

        task.add_done_callback(finished)

    def _launch(self, job: dict[str, Any], prepared: tuple[str, str, str, int], token: CompilerReservation, event_sink: EventSink | None, sync_tec01: bool) -> bool:
        knowledge_id, request_id, fingerprint, _length = prepared
        previous = self.records.get(knowledge_id)
        sequence = previous["seq"] if previous else 0
        if previous:
            self._evict(knowledge_id)
        self.records[knowledge_id] = {"knowledgeId": knowledge_id, "requestId": request_id, "status": "ACCEPTED", "syncStatus": "PENDING" if sync_tec01 else "DISABLED", "seq": sequence, "events": [], "_fingerprint": fingerprint, "_updated": time.monotonic()}
        self.statuses[knowledge_id] = "ACCEPTED"
        token.transferred = True
        work = self._run(job, event_sink, sync_tec01, token)
        try:
            task = asyncio.create_task(work, name="compiler:" + knowledge_id)
        except BaseException:
            work.close()
            self._evict(knowledge_id)
            token.transferred = False
            raise
        self._track(knowledge_id, task, token)
        return True

    async def submit_async(self, job: dict[str, Any], *, reservation: CompilerReservation | None = None, event_sink: EventSink | None = None, sync_tec01: bool = True) -> bool:
        prepared = await cpu_run(self._prepare, job)
        self._cleanup()
        if self._duplicate(prepared):
            self._redeliver_if_needed(prepared[0], sync_tec01)
            return False
        token = reservation or self._claim()
        if token.service is not self or not token.active or token.transferred:
            raise ValueError("提取容量预留无效")
        try:
            data_budget.reserve(token.owner, prepared[3] * 2)
            cloned = await cpu_run(copy.deepcopy, job)
            # Recheck after await: another submit may have won this identity.
            if self._duplicate(prepared):
                self._release(token)
                self._redeliver_if_needed(prepared[0], sync_tec01)
                return False
            return self._launch(cloned, prepared, token, event_sink, sync_tec01)
        except BaseException:
            self._release(token)
            raise

    def submit(self, job: dict[str, Any], *, event_sink: EventSink | None = None, sync_tec01: bool = True) -> bool:
        """Legacy library entry point; network APIs must use submit_async."""
        self._cleanup()
        prepared = self._prepare(job)
        if self._duplicate(prepared):
            self._redeliver_if_needed(prepared[0], sync_tec01)
            return False
        token = self._claim()
        try:
            data_budget.reserve(token.owner, prepared[3] * 2)
            return self._launch(copy.deepcopy(job), prepared, token, event_sink, sync_tec01)
        except BaseException:
            self._release(token)
            raise

    async def shutdown(self) -> None:
        tasks = list(self.tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for knowledge_id, record in self.records.items():
            data_budget.release(self._cache_owner(knowledge_id))
            for field in ("proposal", "validation", "diagnostics", "_finalCallback"):
                record.pop(field, None)
        self._cache_sizes.clear()
        for token in list(self._reservations.values()):
            self._release(token)

    async def _deliver(self, knowledge_id: str, event: dict[str, Any]) -> None:
        record = self.records[knowledge_id]
        try:
            await self.client.extraction_callback(knowledge_id, event)
            record["syncStatus"] = "SYNCED"
            record.pop("callbackError", None)
        except Tec01Error as exc:
            record["syncStatus"] = "FAILED"
            record["callbackError"] = {"code": exc.code, "message": "tec01提取回调未确认，请检查连接后重试提交", "retryable": exc.retryable}

    async def _redeliver_final(self, knowledge_id: str, token: CompilerReservation) -> None:
        try:
            record = self.records[knowledge_id]
            await self._deliver(knowledge_id, record["_finalCallback"])
            record["_updated"] = time.monotonic()
        finally:
            self._release(token)

    async def _retain(self, knowledge_id: str, token: CompilerReservation) -> None:
        record = self.records.get(knowledge_id)
        if record is None:
            return
        limit = self.max_cache_bytes if self.max_cache_bytes is not None else settings.compiler_cache_max_mb * 1024 * 1024
        length = await cpu_run(object_size, record, limit=limit)
        if length > limit:
            self._evict(knowledge_id)
            return
        while self.cache_bytes + length > limit:
            oldest = next((key for key in self._cache_sizes if key not in self.tasks and key != knowledge_id), None)
            if oldest is None:
                self._evict(knowledge_id)
                return
            self._evict(oldest)
        try:
            data_budget.transfer(token.owner, self._cache_owner(knowledge_id), length)
        except ResourceError:
            self._evict(knowledge_id)
            return
        self._cache_sizes[knowledge_id] = length

    async def _run(self, job: dict[str, Any], event_sink: EventSink | None, sync_tec01: bool, token: CompilerReservation) -> None:
        knowledge_id = str(job["knowledgeId"])
        record = self.records[knowledge_id]
        current_stage = "ACCEPTED"

        async def emit(payload: dict[str, Any]) -> None:
            record["seq"] += 1
            event = {**payload, "requestId": job["requestId"], "seq": record["seq"], "occurredAt": datetime.now(UTC).isoformat()}
            record["_updated"] = time.monotonic()
            # History keeps metadata only; final payload is one shared graph.
            record["events"].append({key: value for key, value in event.items() if key not in {"proposal", "validation", "diagnostics"}})
            record["events"] = record["events"][-256:]
            if event["type"] == "FINISHED":
                record["_finalCallback"] = event
                if sync_tec01:
                    record["syncStatus"] = "PENDING"
            if event_sink:
                await event_sink({"knowledgeId": knowledge_id, **event})
            if sync_tec01:
                await self._deliver(knowledge_id, event)

        async def progress(stage: str, message: str, current: int | None, total: int | None) -> None:
            nonlocal current_stage
            current_stage = stage
            record["status"] = stage
            self.statuses[knowledge_id] = stage
            await emit({"type": "PROGRESS", "stage": stage, "message": message, "progress": {"current": current, "total": total}})

        try:
            proposal, diagnostics = await compile_ticket_evidence(job["ticketInfo"], job["auditTimeline"], progress=progress)
            validated = await cpu_run(validate_workflow, proposal["workflowDefinition"], "DRAFT")
            proposal["workflowDefinition"] = validated["normalizedDefinition"]
            validation = {key: validated[key] for key in ("catalogDigest", "workflowContentHash", "valid")}
            outcome = extraction_outcome(diagnostics)
            if outcome["extractionOutcome"] == "PARTIAL_SUCCESS":
                from app.call_logs import emit as log_event
                reasons: dict[str, int] = {}
                for item in diagnostics["failedOperations"]:
                    reasons[item["code"]] = reasons.get(item["code"], 0) + 1
                log_event("COMPILER_PARTIAL_SUCCESS", "WARNING", peer="COMPILER", direction="INTERNAL", operation="compile_evidence",
                          method=None, url=None, httpStatus=None, exitCode=None, spanId=None, callId=None,
                          knowledgeId=knowledge_id, requestId=job["requestId"], businessStatus="PARTIAL_SUCCESS", reasonCode="EXTRACTION_INCOMPLETE",
                          errorMessage=f"草稿提取部分成功：处理异常{diagnostics['failedOperationCount']}条，待核实依赖{len(diagnostics['blockingIssues'])}项，请审核后再发布。",
                          filterSummary=";".join(f"{code}={count}" for code, count in reasons.items()))
            record.update({"status": "SUCCEEDED", "proposal": proposal, "validation": validation, "diagnostics": diagnostics, **outcome})
            self.statuses[knowledge_id] = "SUCCEEDED"
            current_stage = "REPORTING_RESULT"
            await emit({"type": "FINISHED", "status": "SUCCEEDED", "proposal": proposal, "validation": validation, "diagnostics": diagnostics, **outcome})
        except asyncio.CancelledError:
            record["status"] = "INTERRUPTED"
            self.statuses[knowledge_id] = "INTERRUPTED"
            raise
        except Exception as exc:
            error = self._safe_error(exc, current_stage)
            record.update({"status": "FAILED", "error": error})
            self.statuses[knowledge_id] = "FAILED"
            # This local ERROR exists even when callbacks are disabled or fail.
            log_compiler_failure(error, knowledge_id=knowledge_id, request_id=job["requestId"])
            try:
                await emit({"type": "FINISHED", "status": "FAILED", "error": error})
            except Exception as report_exc:
                report_error = failure_details(report_exc, "REPORTING_RESULT")
                report_error["errorId"] = error["errorId"]
                log_compiler_failure(report_error, knowledge_id=knowledge_id, request_id=job["requestId"], event="COMPILER_REPORT_FAILED")
                if sync_tec01:
                    record["syncStatus"] = "FAILED"
                record["callbackError"] = {"code": "COMPILER_REPORT_FAILED", "message": "提取失败原因已记录，但终态同步失败，请按错误编号核查。", "errorId": error["errorId"]}
        finally:
            record["_updated"] = time.monotonic()
            try:
                await self._retain(knowledge_id, token)
            except BaseException:
                # A second cancellation or estimator error must not leave a
                # completed record alive without a data-budget owner.
                self._evict(knowledge_id)
                raise
            finally:
                self._release(token)

    @staticmethod
    def _safe_error(exc: Exception, stage="ACCEPTED") -> dict[str, Any]:
        return failure_details(exc, stage)


compiler_service = CompilerService()
