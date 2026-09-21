from __future__ import annotations

import asyncio
from typing import Any

from app import __version__
from app.extraction import compile_ticket_evidence, extract_operations
from app.runtime.planner import validate_workflow
from app.tec01_client import Tec01Client


class CompilerService:
    def __init__(self, client: Tec01Client | None = None):
        self.client = client or Tec01Client(); self.tasks: dict[str, asyncio.Task] = {}; self.statuses: dict[str, str] = {}

    def submit(self, job: dict[str, Any]) -> bool:
        job_id = str(job["jobId"])
        if job_id in self.statuses: return False
        self.statuses[job_id] = "ACCEPTED"
        task = asyncio.create_task(self._run(job), name="compiler:" + job_id)
        self.tasks[job_id] = task
        task.add_done_callback(lambda _: self.tasks.pop(job_id, None))
        return True

    async def _progress(self, job_id: str, stage: str, message: str, current: int | None = None, total: int | None = None) -> None:
        self.statuses[job_id] = stage
        payload: dict[str, Any] = {"stage": stage, "message": message}
        if current is not None: payload["current"] = current
        if total is not None: payload["total"] = total
        await self.client.compiler_progress(job_id, payload)

    async def _run(self, job: dict[str, Any]) -> None:
        job_id = str(job["jobId"])
        try:
            rows = list(job.get("auditTimeline") or [])
            await self._progress(job_id, "FILTERING_OPERATIONS", "正在过滤有效SQL", 0, len(rows))
            valid, ignored = extract_operations(rows)
            await self._progress(job_id, "BUILDING_WORKFLOW", "正在生成步骤和参数", len(valid), len(rows))
            proposal, diagnostics = await compile_ticket_evidence(dict(job.get("ticketInfo") or {}), rows)
            await self._progress(job_id, "VALIDATING_WORKFLOW", "正在校验Workflow")
            validated = validate_workflow(proposal["workflowDefinition"], "DRAFT")
            proposal["workflowDefinition"] = validated["normalizedDefinition"]
            self.statuses[job_id] = "COMPLETED"
            await self.client.compiler_complete(job_id, {"compilerVersion": __version__, "catalogDigest": validated["catalogDigest"], "proposal": proposal, "diagnostics": {**diagnostics, "ignoredOperationCount": len(ignored)}})
        except Exception as exc:
            self.statuses[job_id] = "FAILED"
            await self.client.compiler_fail(job_id, {"compilerVersion": __version__, "code": "COMPILER_FAILED", "message": str(exc)[:1000]})


compiler_service = CompilerService()
