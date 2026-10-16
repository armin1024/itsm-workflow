from __future__ import annotations

import time
from collections import OrderedDict, deque
from typing import Any


class StudioEvents:
    """Short-lived transport buffers. The browser owns saved knowledge and runs."""

    def __init__(self) -> None:
        self.streams: OrderedDict[str, dict[str, Any]] = OrderedDict()

    def open(self, stream_id: str, *, reset: bool = False) -> None:
        now = time.monotonic()
        for key, stream in list(self.streams.items()):
            if not stream["active"] and now - stream["updated"] > 900:
                self.streams.pop(key, None)
        if stream_id in self.streams and not reset:
            self.streams[stream_id]["active"] = True
            self.streams[stream_id]["updated"] = now
            return
        self.streams[stream_id] = {"events": deque(maxlen=64), "sequence": 0, "active": True, "updated": now}
        while len(self.streams) > 128:
            evict = next((key for key, value in self.streams.items() if not value["active"]), None)
            if evict is None:
                break
            del self.streams[evict]

    async def publish(self, stream_id: str, event: dict[str, Any]) -> None:
        if stream_id not in self.streams:
            self.open(stream_id)
        stream = self.streams[stream_id]
        # Only the latest event carries a complete snapshot, avoiding many
        # retained copies of large SQL results while the browser is polling.
        for previous in stream["events"]:
            previous.pop("snapshot", None)
            previous.pop("result", None)
        stream["sequence"] += 1
        stream["events"].append({**event, "eventId": stream["sequence"]})
        stream["updated"] = time.monotonic()
        if event.get("type") in {"RUN_RELEASED", "FINISHED"}:
            stream["active"] = False

    def get(self, stream_id: str, after: int) -> dict[str, Any]:
        stream = self.streams.get(stream_id)
        if stream is None:
            raise KeyError(stream_id)
        events = [item for item in stream["events"] if item["eventId"] > after]
        return {"events": events, "active": stream["active"], "lastEventId": stream["sequence"]}


studio_events = StudioEvents()
