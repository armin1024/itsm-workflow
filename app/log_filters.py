from datetime import UTC, datetime
from typing import Annotated

from fastapi import HTTPException, Query
from pydantic import BeforeValidator


def empty_date(value):
    return None if isinstance(value, str) and not value.strip() else value


OptionalDate = Annotated[datetime | None, BeforeValidator(empty_date)]


def time_bounds(from_time, to_time):
    invalid = [{"field": "query." + key, "message": "时间必须包含时区，请使用页面时间选择器或带时区ISO格式"} for key, value in (("fromTime", from_time), ("toTime", to_time)) if value is not None and value.tzinfo is None]
    if not invalid and from_time and to_time and from_time >= to_time:
        invalid.append({"field": "query.toTime", "message": "结束时间必须晚于开始时间"})
    if invalid:
        raise HTTPException(422, {"code": "REQUEST_VALIDATION_FAILED", "message": "日志筛选条件不正确", "retryable": False, "details": {"fields": invalid}})


class LogFilters:
    def __init__(self, limit: int = Query(default=100, ge=1, le=200), cursor: str | None = Query(default=None, max_length=2048), keyword: str = Query(default="", max_length=200), level: str = Query(default="", pattern="^(DEBUG|INFO|WARNING|ERROR)?$"), peer: str = Query(default="", pattern="^(TEC01|STUDIO|LLM|AOPS_CLI)?$"), direction: str = Query(default="", pattern="^(INBOUND|OUTBOUND)?$"), event: str = Query(default="", max_length=80), operation: str = Query(default="", max_length=80), runId: str = Query(default="", max_length=160), knowledgeId: str = Query(default="", max_length=160), nodeId: str = Query(default="", max_length=160), requestId: str = Query(default="", max_length=160), spanId: str = Query(default="", max_length=160), callId: str = Query(default="", max_length=160), traceId: str = Query(default="", max_length=160), fromTime: OptionalDate = None, toTime: OptionalDate = None):
        time_bounds(fromTime, toTime)
        self.options = {"limit": limit, "cursor": cursor, "keyword": keyword.strip(), "level": level, "peer": peer, "direction": direction, "event": event.strip(), "operation": operation.strip(), "runId": runId.strip(), "knowledgeId": knowledgeId.strip(), "nodeId": nodeId.strip(), "requestId": requestId.strip(), "spanId": spanId.strip(), "callId": callId.strip(), "traceId": traceId.strip(), "fromTime": fromTime.astimezone(UTC).isoformat() if fromTime else "", "toTime": toTime.astimezone(UTC).isoformat() if toTime else ""}
