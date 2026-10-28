import { withBase } from "./runtime";
import type { NodeCatalogItem } from "./types";
import { localId } from "./browserStore";
import type { RuntimeConfiguration } from "./RuntimeConfig";
import type { LogPage, LogStatus } from "./CallLogs";
import { apiErrorMessage, buildLogQuery } from "./logQuery";

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) { super(message); this.status = status; }
}

async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
  let response: Response;
  try { response = await fetch(withBase(path), {
    ...options,
    credentials: "include",
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
  }); } catch { throw new ApiError(0, "无法连接服务，请检查网络、代理或服务状态后重试"); }
  if (!response.ok) {
    let message = `请求失败 (${response.status})`;
    try {
      const body = await response.json();
      message = apiErrorMessage(body, response.status);
    } catch { /* non-JSON response */ }
    throw new ApiError(response.status, message);
  }
  if (response.status === 204) return undefined as T;
  return response.json();
}

export const api = {
  logs: (query = "", cursor?: string) => request<LogPage>(`/api/v1/studio/logs?${buildLogQuery(query, cursor)}`),
  rawLogs: (query = "", cursor?: string) => request<{ rawLines: Array<{ fileName: string; text: string; logId: string; timestamp: string }>; nextCursor: string | null; unavailable?: boolean }>(`/api/v1/studio/logs/raw?${buildLogQuery(query, cursor)}`),
  journal: (query = "") => request<{ unit: string; items: Array<{ timestamp: string; message: string; priority: string }>; available: boolean; permissionLimited: boolean; reason?: string; message: string; limit: number }>(`/api/v1/studio/logs/journal?${buildLogQuery(query)}`),
  logStatus: () => request<LogStatus>("/api/v1/studio/logs/status"),
  configuration: () => request<RuntimeConfiguration>("/api/v1/studio/config"),
  login: (token: string) => request<{ authenticated: boolean }>("/api/v1/studio/session", { method: "POST", body: JSON.stringify({ token }) }),
  session: () => request<{ authenticated: boolean }>("/api/v1/studio/session"),
  logout: () => request<void>("/api/v1/studio/session", { method: "DELETE" }),
  health: () => request<Record<string, unknown>>("/api/v1/health"),
  studioCatalog: () => request<{ catalogDigest: string; catalogVersion: string; runtimeVersion: string; nodes: NodeCatalogItem[]; testOnly: boolean }>("/api/v1/studio/catalog"),
  extract: (body: Record<string, unknown>, apiKey?: string, fromTicket = false) => request<Record<string, unknown>>(`/api/v1/studio/extractions${fromTicket ? "/from-ticket" : ""}`, { method: "POST", headers: apiKey ? { "X-AOPS-Api-Key": apiKey } : {}, body: JSON.stringify(body) }),
  studioEvents: (id: string, after: number) => request<{ events: Array<Record<string, unknown> & { eventId: number }>; active: boolean; historyGap?: boolean; syncStatus?: string; callbackError?: unknown }>(`/api/v1/studio/events/${encodeURIComponent(id)}?after=${after}`),
  dispatch: (body: Record<string, unknown>, apiKey?: string) => request<Record<string, unknown>>("/api/v1/studio/executions/dispatch", { method: "POST", headers: apiKey ? { "X-AOPS-Api-Key": apiKey } : {}, body: JSON.stringify(body) }),
  command: (runId: string, type: "PAUSE" | "CANCEL") => request<Record<string, unknown>>(`/api/v1/studio/executions/${encodeURIComponent(runId)}/commands`, { method: "POST", body: JSON.stringify({ commandId: localId("command"), type }) }),
  activity: (runId: string) => request<{ runId: string; active: boolean; dispatchId: string | null; executorInstanceId: string; phase: string }>(`/api/v1/studio/executions/${encodeURIComponent(runId)}`),
  planWorkflow: (body: Record<string, unknown>) => request<Record<string, unknown>>("/api/v1/studio/workflows/plan", { method: "POST", body: JSON.stringify(body) }),
  validateWorkflow: (workflowDefinition: Record<string, unknown>) => request<Record<string, unknown>>("/api/v1/studio/workflows/validate", { method: "POST", body: JSON.stringify({ workflowDefinition, validationMode: "TEST" }) }),
  openApi: () => request<Record<string, unknown>>("/openapi.json"),
};
