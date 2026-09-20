from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from app.workflow import WorkflowDefinition


class SessionRequest(BaseModel):
    apiKey: str = Field(min_length=1)


class KnowledgeCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    summary: str = Field(min_length=1, max_length=2000)
    matchPhrases: list[str] = Field(min_length=1, max_length=20)
    negativePhrases: list[str] = Field(default_factory=list, max_length=20)
    systemKeys: list[str] = Field(default_factory=list, max_length=20)
    uids: list[str] = Field(default_factory=list, max_length=100)
    workflowDefinition: WorkflowDefinition


class KnowledgeUpdate(KnowledgeCreate):
    pass


class KnowledgeExtractRequest(BaseModel):
    ticketId: int = Field(gt=0)
    uids: list[str] = Field(default_factory=list, max_length=100)


class PublishRequest(BaseModel):
    pass


class ReviewRejectRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=2000)


class MatchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=4000)
    limit: int = Field(default=3, ge=1, le=10)
    targetSystems: list[str] = Field(default_factory=list, max_length=20)


class PlanRequest(BaseModel):
    knowledgeId: str
    ticketId: int = Field(gt=0)
    parameters: dict[str, Any] = Field(default_factory=dict)


class ApproveRequest(BaseModel):
    planHash: str = Field(min_length=64, max_length=64)


class ResumeRequest(BaseModel):
    payload: dict[str, Any] = Field(default_factory=dict)


class RetryRequest(BaseModel):
    decision: str = Field(pattern="^(retry|mark_failed)$")


class CredentialRequest(BaseModel):
    apiKey: str = Field(min_length=1)


class LegacyImportRequest(BaseModel):
    package: dict[str, Any]


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
    actorUid: str = Field(min_length=1, max_length=120)


class CompilerPreviewRequest(BaseModel):
    ticketInfo: dict[str, Any]
    auditTimeline: list[dict[str, Any]]
    targetCatalogDigest: str | None = None


class StudioWorkspaceCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    draft: dict[str, Any] = Field(default_factory=dict)


class StudioNodeDebugRequest(BaseModel):
    workspaceId: str | None = None
    node: dict[str, Any]
    inputs: dict[str, Any] = Field(default_factory=dict)
    mode: str = Field(default="SIMULATION", pattern="^(TEST|SIMULATION|DRY_RUN)$")
    simulation: dict[str, Any] = Field(default_factory=dict)


class StudioInterruptReply(BaseModel):
    response: dict[str, Any]
