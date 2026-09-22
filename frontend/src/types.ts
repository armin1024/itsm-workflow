export type NodeStatus =
  | "PENDING"
  | "READY"
  | "RUNNING"
  | "WAITING"
  | "SUCCEEDED"
  | "FAILED"
  | "SKIPPED"
  | "CANCELLED"
  | "UNKNOWN";

export type WorkflowNode = {
  id: string;
  type: "sql_read" | "condition" | "hitl_select" | "hitl_form" | "human_input" | "approval" | "end";
  title: string;
  config: Record<string, unknown>;
  inputs: Array<{
    name: string;
    type: string;
    description?: string;
    required?: boolean;
    source: {
      kind: string;
      key?: string;
      nodeId?: string;
      jsonPointer?: string;
      value?: unknown;
    };
  }>;
  approvalPolicy: string;
  timeoutSeconds: number;
  uiPosition?: { x: number; y: number };
};

export type WorkflowEdge = {
  id: string;
  source: string;
  target: string;
  label?: string;
  default?: boolean;
  condition?: Record<string, unknown>;
};

export type Run = {
  id?: string;
  runId: string;
  knowledgeId: string;
  knowledgeName: string;
  workflowVersionId: string;
  ticketId: number;
  initiatedBy: string;
  status: string;
  revision: number;
  lastEventId: number;
  observedAt: string;
  source: string;
  stale: boolean;
  terminal: boolean;
  runPath: string;
  planHash: string;
  workflow: {
    entryNodeId: string;
    nodes: WorkflowNode[];
    edges: WorkflowEdge[];
  };
  runInputs?: Record<string, unknown>;
  nodeStatuses: Record<string, NodeStatus>;
  currentNodeId?: string;
  waitingReason?: string;
  progress: {
    progressCurrent?: number;
    progressTotal?: number;
    current?: number;
    total?: number;
  };
  attempts: Array<Record<string, unknown>>;
  events?: Array<WorkflowEvent>;
  createdAt: string;
  startedAt?: string;
  finishedAt?: string;
};

export type RunSummary = Pick<
  Run,
  | "runId"
  | "knowledgeId"
  | "knowledgeName"
  | "ticketId"
  | "initiatedBy"
  | "status"
  | "revision"
  | "currentNodeId"
  | "waitingReason"
  | "progress"
  | "createdAt"
  | "startedAt"
  | "finishedAt"
>;

export type WorkflowEvent = {
  eventId: string;
  sequence: number;
  runId: string;
  nodeId?: string;
  type: string;
  status?: string;
  safeSummary: string;
  progressCurrent?: number;
  progressTotal?: number;
  payload?: Record<string, unknown>;
  timestamp: string;
};

export type Knowledge = {
  knowledgeId: string;
  status: string;
  name: string;
  summary: string;
  matchPhrases: string[];
  negativePhrases: string[];
  systemKeys: string[];
  creatorUid: string;
  sourceType: string;
  sourceTicketId?: number;
  sourceTicketNo?: string;
  uids: string[];
  createdAt: string;
  updatedAt: string;
  submittedAt?: string;
  submittedBy?: string;
  reviewedAt?: string;
  reviewedBy?: string;
  reviewNote?: string;
  deletedAt?: string;
  deletedBy?: string;
  lastPublishedAt?: string;
  lastPublishedBy?: string;
  lifecycle?: Array<{
    eventId: string;
    eventType: string;
    workflowVersionId?: string;
    actorUid?: string;
    summary: string;
    source: string;
    createdAt: string;
  }>;
  visibility: string;
  workflowDefinition: {
    schemaVersion: 1;
    entryNodeId: string;
    nodes: WorkflowNode[];
    edges: WorkflowEdge[];
  };
};

export type KnowledgeSummary = Pick<
  Knowledge,
  | "knowledgeId"
  | "status"
  | "name"
  | "summary"
  | "sourceType"
  | "sourceTicketId"
  | "sourceTicketNo"
  | "creatorUid"
  | "visibility"
  | "createdAt"
  | "updatedAt"
  | "submittedAt"
> & {
  nodeCount: number;
  publishedAt?: string;
};

export type Paginated<T> = {
  items: T[];
  page: number;
  pageSize: number;
  total: number;
  totalPages: number;
  appliedFilters?: Record<string, unknown>;
};

export type WorkflowVersionSummary = {
  workflowVersionId: string;
  knowledgeId: string;
  knowledgeName: string;
  versionNumber: number;
  contentHash: string;
  publishedBy: string;
  publishedAt: string;
  current: boolean;
};
