from __future__ import annotations

import asyncio
import copy
import time
from collections import OrderedDict
from typing import Any

from app.runtime import NODE_REGISTRY
from app.runtime.executor import execute_single_node
from app.runtime.planner import canonical_hash
from app.config import settings
from app.workflow import WorkflowNode


class SingleNodeExecutor:
    """A resolved-input primitive with bounded process-local request deduplication."""

    def __init__(self):
        self.requests: OrderedDict[str, dict[str, Any]] = OrderedDict()

    async def execute(self, payload: dict[str, Any], api_key: str | None) -> dict[str, Any]:
        now = time.monotonic()
        for request_id, item in list(self.requests.items()):
            if item["task"].done() and (now - item["time"] > 900 or len(self.requests) > 32):
                del self.requests[request_id]
        node = WorkflowNode.model_validate(payload["node"]).model_dump(mode="json")
        manifest = NODE_REGISTRY.validate_node(node)
        if node.get("handlerVersion") and node["handlerVersion"] != manifest.handler_version:
            raise ValueError("NODE_HANDLER_VERSION_UNAVAILABLE")
        if node["type"] == "sql_read" and (not api_key or not payload.get("ticketId")):
            raise ValueError("SQL读单步执行必须提供ticketId和X-AOPS-Api-Key")
        request_id = str(payload["requestId"])
        fingerprint = canonical_hash(payload)
        # Keep only a digest of the credential so a different user's key cannot
        # retrieve another caller's cached output with the same request ID.
        identity_hash = canonical_hash(api_key or "")
        previous = self.requests.get(request_id)
        if previous:
            if previous["fingerprint"] != fingerprint or previous["identity"] != identity_hash:
                raise RuntimeError("SINGLE_NODE_REQUEST_CONFLICT")
            return copy.deepcopy(await asyncio.shield(previous["task"]))
        if sum(not item["task"].done() for item in self.requests.values()) >= settings.executor_max_active_runs:
            raise RuntimeError("EXECUTOR_NO_CAPACITY")

        async def run():
            result = await execute_single_node(node=node, inputs=payload.get("inputs") or {}, mode="PRODUCTION", api_key=api_key, ticket_id=payload.get("ticketId"), resume_payload=payload.get("resumePayload"))
            return {"requestId": request_id, "nodeId": node["id"], "status": result["status"], "output": result.get("output"), "diagnostic": result.get("diagnostic"), "interaction": result.get("interrupt"), "handlerVersion": result["handlerVersion"]}

        task = asyncio.create_task(run(), name="single-node:" + request_id)
        self.requests[request_id] = {"fingerprint": fingerprint, "identity": identity_hash, "task": task, "time": now}
        return copy.deepcopy(await asyncio.shield(task))

    async def shutdown(self):
        tasks = [item["task"] for item in self.requests.values() if not item["task"].done()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.requests.clear()


single_executor = SingleNodeExecutor()
