import { withBase } from "./runtime";
import type { NodeCatalogItem } from "./types";
import { localId } from "./browserStore";

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) { super(message); this.status = status; }
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
      const detail = body.detail;
      message = typeof detail === "string" ? detail : detail?.message || JSON.stringify(detail) || message;
    } catch { /* non-JSON response */ }
    throw new ApiError(response.status, message);
  }
  if (response.status === 204) return undefined as T;
  return response.json();
}

export const api = {
  login: (token: string) => request<{ authenticated: boolean }>("/api/v1/studio/session", { method: "POST", body: JSON.stringify({ token }) }),
  session: () => request<{ authenticated: boolean }>("/api/v1/studio/session"),
  logout: () => request<void>("/api/v1/studio/session", { method: "DELETE" }),
  health: () => request<Record<string, unknown>>("/api/v1/health"),
  studioCatalog: () => request<{ catalogDigest: string; catalogVersion: string; runtimeVersion: string; nodes: NodeCatalogItem[]; testOnly: boolean }>("/api/v1/studio/catalog"),
  studioNodes: () => request<{ items: NodeCatalogItem[]; total: number; catalogDigest: string }>("/api/v1/studio/nodes"),
  updateStudioNode: (type: string, schemaVersion: number, body: Record<string, unknown>) => request<Record<string, unknown>>(`/api/v1/studio/nodes/${encodeURIComponent(type)}/${schemaVersion}`, { method: "PATCH", body: JSON.stringify(body) }),
  studioDebugNode: (body: Record<string, unknown>, apiKey?: string) => request<Record<string, unknown>>("/api/v1/studio/node-debug-runs", { method: "POST", headers: apiKey ? { "X-AOPS-Api-Key": apiKey } : {}, body: JSON.stringify(body) }),
  studioDebugReply: (node: Record<string, unknown>, inputs: Record<string, unknown>, response: Record<string, unknown>) => request<Record<string, unknown>>("/api/v1/studio/node-debug-runs/reply", { method: "POST", body: JSON.stringify({ node, inputs, response }) }),
  extract: (body: Record<string, unknown>, apiKey?: string, fromTicket = false) => request<Record<string, unknown>>(`/api/v1/studio/extractions${fromTicket ? "/from-ticket" : ""}`, { method: "POST", headers: apiKey ? { "X-AOPS-Api-Key": apiKey } : {}, body: JSON.stringify(body) }),
  studioEvents: (id: string, after: number) => request<{ events: Array<Record<string, unknown> & { eventId: number }>; active: boolean; syncStatus?: string; callbackError?: unknown }>(`/api/v1/studio/events/${encodeURIComponent(id)}?after=${after}`),
  dispatch: (body: Record<string, unknown>, apiKey?: string) => request<Record<string, unknown>>("/api/v1/studio/executions/dispatch", { method: "POST", headers: apiKey ? { "X-AOPS-Api-Key": apiKey } : {}, body: JSON.stringify(body) }),
  command: (runId: string, type: "PAUSE" | "CANCEL") => request<Record<string, unknown>>(`/api/v1/studio/executions/${encodeURIComponent(runId)}/commands`, { method: "POST", body: JSON.stringify({ commandId: localId("command"), type }) }),
  planWorkflow: (body: Record<string, unknown>) => request<Record<string, unknown>>("/api/v1/studio/workflows/plan", { method: "POST", body: JSON.stringify(body) }),
  compileEvidence: (ticketInfo: Record<string, unknown>, auditTimeline: Record<string, unknown>[]) => request<Record<string, unknown>>("/api/v1/studio/compiler/preview", { method: "POST", body: JSON.stringify({ ticketInfo, auditTimeline }) }),
  compileTicket: (ticketId: number, apiKey: string) => request<Record<string, unknown>>("/api/v1/studio/compiler/from-ticket", { method: "POST", headers: { "X-AOPS-Api-Key": apiKey }, body: JSON.stringify({ ticketId }) }),
  validateWorkflow: (workflowDefinition: Record<string, unknown>) => request<Record<string, unknown>>("/api/v1/studio/workflows/validate", { method: "POST", body: JSON.stringify({ workflowDefinition, validationMode: "TEST" }) }),
  openApi: () => request<Record<string, unknown>>("/openapi.json"),
};
