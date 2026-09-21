from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol


class ExecutionMode(StrEnum):
    PRODUCTION = "PRODUCTION"
    TEST = "TEST"
    SIMULATION = "SIMULATION"
    DRY_RUN = "DRY_RUN"


class IdempotencyClass(StrEnum):
    READ_SAFE = "READ_SAFE"
    PURE = "PURE"
    REPLAY_WITH_STORED_RESULT = "REPLAY_WITH_STORED_RESULT"
    EXTERNAL_IDEMPOTENT = "EXTERNAL_IDEMPOTENT"
    NON_IDEMPOTENT = "NON_IDEMPOTENT"


class ResumeSemantics(StrEnum):
    SAFE_RETRY = "SAFE_RETRY"
    CHECK_RESULT_THEN_RETRY = "CHECK_RESULT_THEN_RETRY"
    WAIT_FOR_INPUT = "WAIT_FOR_INPUT"
    UNKNOWN_REQUIRES_OPERATOR = "UNKNOWN_REQUIRES_OPERATOR"


@dataclass(frozen=True)
class ExecutionContext:
    mode: ExecutionMode
    run_id: str
    node_id: str
    attempt_id: str
    ticket_id: int
    runtime_version: str
    lease_token: str | None = None
    run_revision: int | None = None


@dataclass(frozen=True)
class NodeResult:
    status: str
    output: dict[str, Any]
    safe_summary: str
    artifacts: tuple[dict[str, Any], ...] = ()
    external_request_id: str | None = None
    metrics: dict[str, float | int] | None = None


class StatePort(Protocol):
    async def get_run(self, run_id: str) -> dict[str, Any]: ...


class ArtifactPort(Protocol):
    async def get(self, artifact_id: str) -> dict[str, Any]: ...
    async def put(self, role: str, value: Any, sensitivity: str) -> str: ...


class ModelPort(Protocol):
    async def invoke_structured(self, *, model_profile: str, prompt_template: str, inputs: dict[str, Any], response_schema: dict[str, Any], idempotency_key: str) -> dict[str, Any]: ...


class HumanInteractionPort(Protocol):
    async def request(self, *, kind: str, request: dict[str, Any]) -> dict[str, Any]: ...


class DbReadPort(Protocol):
    async def read(self, *, database_ref: str, sql: str, ticket_id: int, timeout_seconds: int) -> dict[str, Any]: ...


class NodeHandler(Protocol):
    async def execute(self, definition: dict[str, Any], inputs: dict[str, Any], context: ExecutionContext, ports: dict[str, Any]) -> NodeResult: ...
