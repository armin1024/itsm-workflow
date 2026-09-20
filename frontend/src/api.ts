import { withBase } from "./runtime";
import type { NodeCatalogItem } from "./types";

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
  return response.json();
}

export const api = {
  health: () => request<Record<string, unknown>>("/api/v1/health"),
  studioCatalog: () => request<{ catalogDigest: string; catalogVersion: string; runtimeVersion: string; nodes: NodeCatalogItem[]; testOnly: boolean }>("/api/v1/studio/catalog"),
  studioDebugNode: (body: Record<string, unknown>, apiKey?: string) => request<Record<string, unknown>>("/api/v1/studio/node-debug-runs", { method: "POST", headers: apiKey ? { "X-AOPS-Api-Key": apiKey } : {}, body: JSON.stringify(body) }),
  studioDebugReply: (id: string, response: Record<string, unknown>) => request<Record<string, unknown>>(`/api/v1/studio/node-debug-runs/${id}/interrupts/reply`, { method: "POST", body: JSON.stringify({ response }) }),
  compileEvidence: (ticketInfo: Record<string, unknown>, auditTimeline: Record<string, unknown>[]) => request<Record<string, unknown>>("/api/v1/studio/compiler/preview", { method: "POST", body: JSON.stringify({ ticketInfo, auditTimeline }) }),
  compileTicket: (ticketId: number, apiKey: string) => request<Record<string, unknown>>("/api/v1/studio/compiler/from-ticket", { method: "POST", headers: { "X-AOPS-Api-Key": apiKey }, body: JSON.stringify({ ticketId }) }),
  validateWorkflow: (workflowDefinition: Record<string, unknown>) => request<Record<string, unknown>>("/api/v1/studio/workflows/validate", { method: "POST", body: JSON.stringify({ workflowDefinition, validationMode: "TEST" }) }),
};
