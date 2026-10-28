from __future__ import annotations

import time
from collections import OrderedDict, deque
from typing import Any
from app.resources import ResourceError, cpu_run, data_budget, object_size
from app.config import settings


class StudioEvents:
    """Short-lived transport buffers. The browser owns saved knowledge and runs."""

    def __init__(self) -> None:
        self.streams: OrderedDict[str, dict[str, Any]] = OrderedDict()

    @staticmethod
    def _owner(stream_id: str) -> str:
        return "studio-stream:" + stream_id

    def reserve(self, stream_id: str, amount: int):
        owner = self._owner(stream_id)
        previous = data_budget.owners.get(owner)
        data_budget.reserve(owner, amount)
        return previous

    def restore_reservation(self, stream_id: str, previous):
        if previous is None:
            data_budget.release(self._owner(stream_id))
        else:
            data_budget.reserve(self._owner(stream_id), previous)

    def cleanup(self):
        now = time.monotonic()
        for key, stream in list(self.streams.items()):
            if not stream["active"] and now - stream["updated"] > 900:
                self.streams.pop(key, None)
                data_budget.release(self._owner(key))
        while len(self.streams) > 128:
            evict = next((key for key, value in self.streams.items() if not value["active"]), None)
            if evict is None:
                break
            self.streams.pop(evict, None)
            data_budget.release(self._owner(evict))

    def clear(self):
        for key in self.streams:
            data_budget.release(self._owner(key))
        self.streams.clear()

    def open(self, stream_id: str, *, reset: bool = False) -> None:
        self.cleanup()
        now = time.monotonic()
        if stream_id in self.streams and not reset:
            self.streams[stream_id]["active"] = True
            self.streams[stream_id]["updated"] = now
            return
        self.streams[stream_id] = {"events": deque(maxlen=64), "sequence": 0, "active": True, "updated": now,
                                   "capacity": data_budget.owners.get(self._owner(stream_id))}
        self.cleanup()

    async def publish(self, stream_id: str, event: dict[str, Any]) -> None:
        if stream_id not in self.streams:
            self.open(stream_id)
        stream = self.streams[stream_id]
        # Only the latest event carries a complete snapshot, avoiding many
        # retained copies of large SQL results while the browser is polling.
        prior = [{key: value for key, value in previous.items() if key not in {"snapshot", "result", "interaction"}} for previous in stream["events"]]
        latest = {**event, "eventId": stream["sequence"] + 1}
        if "snapshot" in latest:
            # Local clients apply the complete snapshot. Do not encode or
            # account the same result/context twice in one local response.
            latest.pop("result", None)
            latest.pop("interaction", None)
        candidate = deque([*prior, latest], maxlen=64)
        capacity = stream.get("capacity") or settings.executor_run_memory_mb * 1024 * 1024
        size = await cpu_run(object_size, list(candidate), limit=capacity)
        if size > capacity:
            # Keep the last successfully cached snapshot, but make the lost
            # new result explicit and terminal. Never retain the oversized
            # candidate or leave the browser polling a permanently active job.
            failed = {"type": "RUN_RELEASED", "status": "UNKNOWN", "reason": "调试结果超出临时缓存额度，最新结果未保存，请人工核实", "eventId": stream["sequence"] + 1}
            stream["events"].append(failed)
            stream["sequence"] += 1
            stream["active"] = False
            stream["updated"] = time.monotonic()
            raise ResourceError("STUDIO_RESULT_LIMIT", "调试结果超出临时缓存额度；外部请求可能已发生，请核对记录，不要自动重试")
        if event.get("type") in {"RUN_RELEASED", "FINISHED"}:
            stream["active"] = False
            data_budget.reserve(self._owner(stream_id), size)
        elif self._owner(stream_id) not in data_budget.owners:
            data_budget.reserve(self._owner(stream_id), capacity)
        stream["sequence"] += 1
        stream["events"] = candidate
        stream["updated"] = time.monotonic()

    def get(self, stream_id: str, after: int) -> dict[str, Any]:
        self.cleanup()
        stream = self.streams.get(stream_id)
        if stream is None:
            raise KeyError(stream_id)
        events = [dict(item) for item in stream["events"] if item["eventId"] > after]
        first = stream["events"][0]["eventId"] if stream["events"] else stream["sequence"] + 1
        return {"events": events, "active": stream["active"], "lastEventId": stream["sequence"], "historyGap": after < first - 1}


studio_events = StudioEvents()
