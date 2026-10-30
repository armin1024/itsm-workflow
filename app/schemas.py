from __future__ import annotations

from typing import Any, Literal

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


class FailedSqlOperation(BaseModel):
    operationId: str | None
    auditIndex: int = Field(ge=0)
    stage: Literal["FILTERING_OPERATIONS", "PARAMETERIZING_SQL"]
    code: Literal["SQL_PARSE_FAILED", "SQL_NOT_READ_ONLY", "SQL_MULTIPLE_STATEMENTS", "SQL_TEMPLATE_TOO_LARGE", "SQL_PARAMETERIZATION_FAILED", "SQL_NODE_VALIDATION_FAILED"]
    message: str


class ExtractionBlockingIssue(BaseModel):
    code: Literal["DEPENDENCY_REQUIRES_REVIEW", "EXTRACTION_INCOMPLETE"]
    message: str
    nodeIds: list[str]
    operationIds: list[str]


class ExtractionDiagnostics(BaseModel):
    ticketNo: str | None = None
    auditOperationCount: int = Field(ge=0)
    acceptedOperationCount: int = Field(ge=0)
    ignoredOperationCount: int = Field(ge=0)
    ignoredOperations: list[dict[str, Any]]
    failedOperationCount: int = Field(ge=0)
    failedOperations: list[FailedSqlOperation]
    blockingIssues: list[ExtractionBlockingIssue]
    llmMode: Literal["structured"] = "structured"


class ExtractionFinished(BaseModel):
    """Success callback only; FAILED continues using the existing error envelope."""
    knowledgeId: str
    requestId: str
    type: Literal["FINISHED"]
    status: Literal["SUCCEEDED"]
    seq: int = Field(ge=1)
    occurredAt: str
    extractionOutcome: Literal["SUCCESS", "PARTIAL_SUCCESS"]
    publishable: bool
    proposal: dict[str, Any]
    validation: dict[str, Any]
    diagnostics: ExtractionDiagnostics


class CompilerTicketRequest(BaseModel):
    ticketId: int = Field(gt=0)
    targetCatalogDigest: str | None = None


class StudioLoginRequest(BaseModel):
    token: str = Field(min_length=1, max_length=1000)


class ExtractionSubmitRequest(BaseModel):
    requestId: str = Field(min_length=1, max_length=160, pattern=r"^[A-Za-z0-9_.:-]+$")
    ticketId: int = Field(gt=0)
    ticketInfo: dict[str, Any]
    auditTimeline: list[dict[str, Any]]


class StudioExtractionRequest(ExtractionSubmitRequest):
    knowledgeId: str = Field(min_length=1, max_length=160, pattern=r"^[A-Za-z0-9_.:-]+$")
    syncTec01: bool = False


class StudioTicketExtractionRequest(BaseModel):
    knowledgeId: str = Field(min_length=1, max_length=160, pattern=r"^[A-Za-z0-9_.:-]+$")
    requestId: str = Field(min_length=1, max_length=160, pattern=r"^[A-Za-z0-9_.:-]+$")
    ticketId: int = Field(gt=0)
    syncTec01: bool = False


class ExecutionDispatchRequest(BaseModel):
    dispatchId: str = Field(min_length=1, max_length=160, pattern=r"^[A-Za-z0-9_.:-]+$")
    runId: str = Field(min_length=1, max_length=160, pattern=r"^[A-Za-z0-9_.:-]+$")
    ticketId: int = Field(gt=0)
    workflowContentHash: str = Field(min_length=1, max_length=128)
    workflowSnapshot: dict[str, Any]
    runInputs: dict[str, Any] = Field(default_factory=dict)
    nodeStates: dict[str, str] = Field(default_factory=dict)
    nodeOutputs: dict[str, dict[str, Any]] = Field(default_factory=dict)
    nodeResults: dict[str, dict[str, Any]] = Field(default_factory=dict)
    currentNodeId: str | None = None
    selectedRoutes: dict[str, str] = Field(default_factory=dict)
    resumePayload: dict[str, Any] | None = None


class ExecutionCommandRequest(BaseModel):
    commandId: str = Field(min_length=1, max_length=160)
    type: str = Field(pattern="^(PAUSE|CANCEL)$")


class StudioExecutionRequest(ExecutionDispatchRequest):
    syncTec01: bool = False
    planMaterialHash: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
