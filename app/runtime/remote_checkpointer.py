from __future__ import annotations

import base64
from collections.abc import AsyncIterator, Sequence
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver, Checkpoint, CheckpointMetadata, CheckpointTuple

from app import __version__
from app.tec01_client import Tec01Client


def _config_values(config: RunnableConfig) -> dict[str, Any]:
    return dict(config.get("configurable") or {})


def _encode_typed(serde, value: Any) -> dict[str, str]:
    value_type, data = serde.dumps_typed(value)
    return {"type": value_type, "base64": base64.b64encode(data).decode("ascii")}


def _decode_typed(serde, value: dict[str, str]) -> Any:
    return serde.loads_typed((value["type"], base64.b64decode(value["base64"])))


class RemoteTec01Checkpointer(BaseCheckpointSaver):
    """LangGraph saver that stages opaque checkpoints in tec01.

    A business attempt commit must activate the staged checkpoint. Reads only
    return tec01 COMMITTED checkpoints.
    """

    def __init__(self, client: Tec01Client, **kwargs):
        super().__init__(**kwargs)
        self.client = client

    async def aget_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        values = _config_values(config)
        thread_id = str(values["thread_id"])
        item = await self.client.get_checkpoint(thread_id, values.get("checkpoint_id"), str(values.get("checkpoint_ns") or ""))
        if not item:
            return None
        checkpoint = _decode_typed(self.serde, item["checkpoint"])
        metadata = _decode_typed(self.serde, item["metadata"])
        result_config = {"configurable": {"thread_id": thread_id, "checkpoint_ns": item.get("checkpointNamespace", ""), "checkpoint_id": item["checkpointId"]}}
        parent_id = item.get("parentCheckpointId")
        parent = {"configurable": {"thread_id": thread_id, "checkpoint_ns": item.get("checkpointNamespace", ""), "checkpoint_id": parent_id}} if parent_id else None
        writes = [(value["taskId"], value["channel"], _decode_typed(self.serde, value["value"])) for value in item.get("pendingWrites", [])]
        return CheckpointTuple(result_config, checkpoint, metadata, parent, writes)

    async def alist(self, config: RunnableConfig | None, *, filter: dict[str, Any] | None = None, before: RunnableConfig | None = None, limit: int | None = None) -> AsyncIterator[CheckpointTuple]:
        if config is None:
            return
        values = _config_values(config)
        before_id = _config_values(before).get("checkpoint_id") if before else None
        response = await self.client.list_checkpoints(str(values["thread_id"]), str(values.get("checkpoint_ns") or ""), before_id, limit)
        for item in response.get("items", []):
            item_config = {"configurable": {"thread_id": values["thread_id"], "checkpoint_ns": item.get("checkpointNamespace", ""), "checkpoint_id": item["checkpointId"]}}
            found = await self.aget_tuple(item_config)
            if found:
                yield found

    async def aput(self, config: RunnableConfig, checkpoint: Checkpoint, metadata: CheckpointMetadata, new_versions: dict[str, Any]) -> RunnableConfig:
        values = _config_values(config)
        thread_id = str(values["thread_id"])
        checkpoint_id = str(checkpoint["id"])
        lease = str(values.get("lease_token") or "")
        attempt = str(values.get("attempt_id") or "")
        if not lease or not attempt:
            raise ValueError("Remote Checkpointer需要lease_token和attempt_id")
        payload = {"leaseToken": lease, "attemptId": attempt, "runtimeVersion": __version__, "checkpointFormatVersion": 1, "checkpointNamespace": str(values.get("checkpoint_ns") or ""), "parentCheckpointId": values.get("checkpoint_id"), "checkpoint": _encode_typed(self.serde, checkpoint), "metadata": _encode_typed(self.serde, metadata), "newVersions": new_versions}
        await self.client.stage_checkpoint(thread_id, checkpoint_id, payload, f"checkpoint:{thread_id}:{checkpoint_id}")
        return {"configurable": {**values, "checkpoint_id": checkpoint_id}}

    async def aput_writes(self, config: RunnableConfig, writes: Sequence[tuple[str, Any]], task_id: str, task_path: str = "") -> None:
        values = _config_values(config)
        thread_id, checkpoint_id = str(values["thread_id"]), str(values.get("checkpoint_id") or "")
        if not checkpoint_id:
            raise ValueError("checkpoint_id缺失")
        payload = {"leaseToken": str(values.get("lease_token") or ""), "attemptId": str(values.get("attempt_id") or ""), "taskId": task_id, "taskPath": task_path, "writes": [{"index": index, "channel": channel, "value": _encode_typed(self.serde, value)} for index, (channel, value) in enumerate(writes)]}
        await self.client.stage_checkpoint_writes(thread_id, checkpoint_id, payload, f"checkpoint-writes:{thread_id}:{checkpoint_id}:{task_id}")

    async def adelete_thread(self, thread_id: str) -> None:
        await self.client.delete_checkpoint_thread(thread_id)
