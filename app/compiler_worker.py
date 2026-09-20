from __future__ import annotations

import asyncio
import hashlib
import json
import os
import socket
from typing import Any

from app import __version__
from app.config import settings
from app.extraction import compile_ticket_evidence
from app.runtime import NODE_REGISTRY
from app.runtime.planner import validate_workflow
from app.tec01_client import Tec01Client, Tec01Error


def _safe_error(exc: Exception) -> tuple[str, str, bool]:
    message = str(exc)[:500]
    if "NO_VALID_OPERATIONS" in message:
        return "NO_VALID_OPERATIONS", message, False
    if "LLM_ANALYSIS" in message:
        return "LLM_ANALYSIS_FAILED", message, True
    if isinstance(exc, Tec01Error):
        return exc.code, message, exc.retryable
    return "COMPILER_FAILED", message, False


class CompilerWorker:
    def __init__(self, client: Tec01Client | None = None, compiler_id: str | None = None):
        self.client = client or Tec01Client()
        self.compiler_id = compiler_id or f"{socket.gethostname()}:{os.getpid()}"

    async def process(self, job: dict[str, Any]) -> None:
        extraction_id, lease = str(job["extractionId"]), str(job["leaseToken"])
        evidence_hash = str(job.get("evidenceHash") or "")
        try:
            actual = hashlib.sha256(json.dumps({"ticketInfo": job.get("ticketInfo"), "auditTimeline": job.get("auditTimeline")}, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            if evidence_hash and evidence_hash != actual:
                raise ValueError("EVIDENCE_HASH_MISMATCH")
            proposal, diagnostics = await compile_ticket_evidence(job.get("ticketInfo"), job.get("auditTimeline"))
            validated = validate_workflow(proposal["workflowDefinition"], "DRAFT")
            target_digest = str(job.get("targetCatalogDigest") or "")
            if target_digest and target_digest != validated["catalogDigest"]:
                raise ValueError("CATALOG_DIGEST_CONFLICT")
            proposal["workflowDefinition"] = validated["normalizedDefinition"]
            payload = {"leaseToken": lease, "compilerVersion": __version__, "evidenceHash": actual, "catalogDigest": validated["catalogDigest"], "proposal": proposal, "diagnostics": diagnostics}
            await self.client.complete_extraction(extraction_id, payload, f"extraction:{extraction_id}:complete")
        except Exception as exc:
            code, message, retryable = _safe_error(exc)
            await self.client.fail_extraction(extraction_id, {"leaseToken": lease, "compilerVersion": __version__, "code": code, "message": message, "retryable": retryable}, f"extraction:{extraction_id}:fail:{code}")

    async def run_forever(self) -> None:
        catalog = NODE_REGISTRY.catalog()
        while True:
            try:
                job = await self.client.claim_compiler_job(self.compiler_id, __version__, [catalog["catalogDigest"]], settings.tec01_claim_wait_seconds)
                if job:
                    await self.process(job)
            except Tec01Error:
                await asyncio.sleep(3)


async def main() -> None:
    if not settings.tec01_enabled:
        raise RuntimeError("TEC01_ENABLED未启用")
    await CompilerWorker().run_forever()


if __name__ == "__main__":
    asyncio.run(main())
