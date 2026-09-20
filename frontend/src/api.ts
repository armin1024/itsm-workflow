import type {
  Knowledge,
  KnowledgeSummary,
  Paginated,
  Run,
  RunSummary,
  WorkflowVersionSummary,
  NodeCatalogItem,
} from "./types";
import { withBase } from "./runtime";

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
  const response = await fetch(withBase(path), {
    ...options,
    credentials: "include",
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
  });
  if (!response.ok) {
    let message = `请求失败 (${response.status})`;
    try {
      const body = await response.json();
      message = body.detail || message;
    } catch {
      /* response is not JSON */
    }
    throw new ApiError(response.status, message);
  }
  if (response.status === 204) return undefined as T;
  return response.json();
}

function query(
  path: string,
  params: Record<string, string | number | undefined>,
): string {
  const search = new URLSearchParams();
  Object.entries(params).forEach(([key, value]) => {
    if (value !== undefined && value !== "") search.set(key, String(value));
  });
  const suffix = search.toString();
  return suffix ? `${path}?${suffix}` : path;
}

export const api = {
  login: (apiKey: string) =>
    request<{ uid: string; isAdmin: boolean; isOperator: boolean; features?: { studio?: boolean } }>(
      "/api/v1/auth/session",
      { method: "POST", body: JSON.stringify({ apiKey }) },
    ),
  me: () =>
    request<{ uid: string; isAdmin: boolean; isOperator: boolean; features?: { studio?: boolean } }>(
      "/api/v1/auth/me",
    ),
  logout: () => request<void>("/api/v1/auth/session", { method: "DELETE" }),
  knowledge: (
    params: {
      page?: number;
      pageSize?: number;
      keyword?: string;
      status?: string;
    } & Record<string, string | number | undefined> = {},
  ) => request<Paginated<KnowledgeSummary>>(query("/api/v1/knowledge", params)),
  knowledgeById: (id: string) => request<Knowledge>(`/api/v1/knowledge/${id}`),
  versions: (params: Record<string, string | number | undefined> = {}) =>
    request<Paginated<WorkflowVersionSummary>>(query("/api/v1/workflow-versions", params)),
  createKnowledge: (body: Record<string, unknown>) =>
    request<Knowledge>("/api/v1/knowledge", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  updateKnowledge: (id: string, body: Record<string, unknown>) =>
    request<Knowledge>(`/api/v1/knowledge/${id}`, {
      method: "PATCH",
      body: JSON.stringify(body),
    }),
  deleteKnowledge: (id: string) =>
    request<{
      knowledgeId: string;
      status: string;
      deletedAt: string;
      deletedBy: string;
    }>(`/api/v1/knowledge/${id}`, { method: "DELETE" }),
  extractKnowledge: (body: { ticketId: number; uids: string[] }) =>
    request<{
      knowledge: Knowledge;
      extraction: {
        ticketId: number;
        ticketNo?: string;
        auditOperationCount: number;
        acceptedOperationCount: number;
        ignoredOperationCount: number;
        ignoredOperations: Array<{ operationId?: unknown; reason: string }>;
      };
    }>("/api/v1/knowledge/extract", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  publishKnowledge: (id: string) =>
    request<Record<string, unknown>>(`/api/v1/knowledge/${id}/publish`, {
      method: "POST",
      body: "{}",
    }),
  submitKnowledgeReview: (id: string) =>
    request<Knowledge>(`/api/v1/knowledge/${id}/submit-review`, {
      method: "POST",
      body: "{}",
    }),
  runs: (
    params: {
      page?: number;
      pageSize?: number;
      keyword?: string;
      statusGroup?: string;
    } & Record<string, string | number | undefined> = {},
  ) => request<Paginated<RunSummary>>(query("/api/v1/runs", params)),
  run: (id: string) => request<Run>(`/api/v1/runs/${id}`),
  plan: (body: {
    knowledgeId: string;
    ticketId: number;
    parameters: Record<string, unknown>;
  }) =>
    request<Run>("/api/v1/runs/plan", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  approve: (id: string, planHash: string) =>
    request<Run>(`/api/v1/runs/${id}/approve`, {
      method: "POST",
      body: JSON.stringify({ planHash }),
    }),
  action: (id: string, action: "pause" | "resume" | "cancel") =>
    request<Run>(`/api/v1/runs/${id}/${action}`, {
      method: "POST",
      body: "{}",
    }),
  resumeInterrupt: (
    runId: string,
    interruptId: string,
    payload: Record<string, unknown>,
  ) =>
    request<Run>(`/api/v1/runs/${runId}/interrupts/${interruptId}/resume`, {
      method: "POST",
      body: JSON.stringify({ payload }),
    }),
  updateCredential: (runId: string, apiKey: string) =>
    request<Run>(`/api/v1/runs/${runId}/credential`, {
      method: "POST",
      body: JSON.stringify({ apiKey }),
    }),
  artifact: (runId: string, nodeId: string) =>
    request<{ data: Record<string, unknown> }>(
      `/api/v1/runs/${runId}/nodes/${nodeId}/artifact`,
    ),
  retryNode: (
    runId: string,
    nodeId: string,
    decision: "retry" | "mark_failed",
  ) =>
    request<Run>(`/api/v1/runs/${runId}/nodes/${nodeId}/retry`, {
      method: "POST",
      body: JSON.stringify({ decision }),
    }),
  diagnostic: (runId: string, attemptId: string) =>
    request<{
      attemptId: string;
      errorCode?: string;
      errorMessage?: string;
      exitCode?: number;
      diagnosticTruncated: boolean;
      data: { stdout: string; stderr: string; truncated: boolean };
    }>(`/api/v1/runs/${runId}/attempts/${attemptId}/diagnostic`),
  exportPreview: (knowledgeIds: string[]) =>
    request<Record<string, unknown>>("/api/v1/transfers/export/preview", { method: "POST", body: JSON.stringify({ knowledgeIds }) }),
  exportPackage: (body: Record<string, unknown>) =>
    request<Record<string, unknown>>("/api/v1/transfers/export", { method: "POST", body: JSON.stringify(body) }),
  importPreview: (body: Record<string, unknown>) =>
    request<Record<string, unknown>>("/api/v1/transfers/import/preview", { method: "POST", body: JSON.stringify(body) }),
  importPackage: (body: Record<string, unknown>) =>
    request<Record<string, unknown>>("/api/v1/transfers/import", { method: "POST", body: JSON.stringify(body) }),
  replacePreview: (body: Record<string, unknown>) =>
    request<Record<string, unknown>>("/api/v1/database-paths/replace/preview", { method: "POST", body: JSON.stringify(body) }),
  replacePaths: (body: Record<string, unknown>) =>
    request<Record<string, unknown>>("/api/v1/database-paths/replace", { method: "POST", body: JSON.stringify(body) }),
  studioCatalog: () => request<{ catalogDigest: string; catalogVersion: string; runtimeVersion: string; nodes: NodeCatalogItem[]; testOnly: boolean }>("/api/v1/studio/catalog"),
  studioWorkspaces: () => request<{ items: Array<Record<string, unknown>>; total: number }>("/api/v1/studio/workspaces"),
  createStudioWorkspace: (name: string, draft: Record<string, unknown> = {}) => request<Record<string, unknown>>("/api/v1/studio/workspaces", { method: "POST", body: JSON.stringify({ name, draft }) }),
  studioDebugNode: (body: Record<string, unknown>) => request<Record<string, unknown>>("/api/v1/studio/node-debug-runs", { method: "POST", body: JSON.stringify(body) }),
  studioDebugRun: (id: string) => request<Record<string, unknown>>(`/api/v1/studio/node-debug-runs/${id}`),
  studioDebugReply: (id: string, response: Record<string, unknown>) => request<Record<string, unknown>>(`/api/v1/studio/node-debug-runs/${id}/interrupts/reply`, { method: "POST", body: JSON.stringify({ response }) }),
};
