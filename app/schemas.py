from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class RuntimeValidateRequest(BaseModel):
    workflowDefinition: dict[str, Any]
    targetCatalogDigest: str | None = None
    validationMode: str = Field(default="DRAFT", pattern="^(DRAFT|PUBLISH|EXECUTE|TEST)$")


class RuntimePlanRequest(BaseModel):
    workflowVersionId: str
    workflowContentHash: str
    workflowSnapshot: dict[str, Any]
    ticketId: int = Field(gt=0)
    parameters: dict[str, Any] = Field(default_factory=dict)
    actorUid: str = Field(default="studio", min_length=1, max_length=120)


class CompilerPreviewRequest(BaseModel):
    ticketInfo: dict[str, Any]
    auditTimeline: list[dict[str, Any]]
    targetCatalogDigest: str | None = None


class CompilerTicketRequest(BaseModel):
    ticketId: int = Field(gt=0)
    targetCatalogDigest: str | None = None


class StudioWorkspaceCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    draft: dict[str, Any] = Field(default_factory=dict)


class StudioNodeDebugRequest(BaseModel):
    workspaceId: str | None = None
    ticketId: int | None = Field(default=None, gt=0)
    node: dict[str, Any]
    inputs: dict[str, Any] = Field(default_factory=dict)
    mode: str = Field(default="TEST", pattern="^(TEST|SIMULATION|DRY_RUN)$")
    simulation: dict[str, Any] = Field(default_factory=dict)


class StudioInterruptReply(BaseModel):
    response: dict[str, Any]


class StudioLoginRequest(BaseModel):
    token: str = Field(min_length=1, max_length=1000)


class StudioNodeUpdate(BaseModel):
    enabled: bool = True
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(min_length=1, max_length=1000)
    debugConfig: dict[str, Any] = Field(default_factory=dict)
    debugInputs: dict[str, Any] = Field(default_factory=dict)
    debugFixture: dict[str, Any] = Field(default_factory=dict)


class StudioWorkflowDebugRequest(BaseModel):
    workflowDefinition: dict[str, Any]
    ticketId: int | None = Field(default=None, gt=0)
    inputs: dict[str, Any] = Field(default_factory=dict)
    mode: str = Field(default="TEST", pattern="^(TEST|SIMULATION|DRY_RUN)$")
    simulation: dict[str, Any] = Field(default_factory=dict)


class CompilerJobRequest(BaseModel):
    jobId: str = Field(min_length=1, max_length=160)
    ticketId: int = Field(gt=0)
    ticketInfo: dict[str, Any]
    auditTimeline: list[dict[str, Any]]


class ExecutionDispatchRequest(BaseModel):
    dispatchId: str = Field(min_length=1, max_length=160)
    runId: str = Field(min_length=1, max_length=160)
    ticketId: int = Field(gt=0)
    workflowContentHash: str = Field(min_length=1, max_length=128)
    workflowSnapshot: dict[str, Any]
    runInputs: dict[str, Any] = Field(default_factory=dict)
    nodeStates: dict[str, str] = Field(default_factory=dict)
    nodeOutputs: dict[str, dict[str, Any]] = Field(default_factory=dict)
    currentNodeId: str | None = None
    selectedRoutes: dict[str, str] = Field(default_factory=dict)
    resumePayload: dict[str, Any] | None = None


class ExecutionCommandRequest(BaseModel):
    commandId: str = Field(min_length=1, max_length=160)
    type: str = Field(pattern="^(PAUSE|CANCEL)$")
