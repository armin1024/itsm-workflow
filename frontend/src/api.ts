import type { Knowledge, Run } from './types'
import { withBase } from './runtime'

export class ApiError extends Error {
  status: number
  constructor(status: number, message: string) { super(message); this.status = status }
}

async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
  const response = await fetch(withBase(path), {
    ...options,
    credentials: 'include',
    headers: {'Content-Type': 'application/json', ...(options.headers || {})},
  })
  if (!response.ok) {
    let message = `请求失败 (${response.status})`
    try { const body = await response.json(); message = body.detail || message } catch { /* response is not JSON */ }
    throw new ApiError(response.status, message)
  }
  if (response.status === 204) return undefined as T
  return response.json()
}

export const api = {
  login: (apiKey: string) => request<{uid: string; isAdmin: boolean; isOperator: boolean}>('/api/v1/auth/session', {method: 'POST', body: JSON.stringify({apiKey})}),
  me: () => request<{uid: string; isAdmin: boolean; isOperator: boolean}>('/api/v1/auth/me'),
  logout: () => request<void>('/api/v1/auth/session', {method: 'DELETE'}),
  knowledge: () => request<{items: Knowledge[]; total: number}>('/api/v1/knowledge'),
  knowledgeById: (id: string) => request<Knowledge>(`/api/v1/knowledge/${id}`),
  createKnowledge: (body: Record<string, unknown>) => request<Knowledge>('/api/v1/knowledge', {method: 'POST', body: JSON.stringify(body)}),
  updateKnowledge: (id: string, body: Record<string, unknown>) => request<Knowledge>(`/api/v1/knowledge/${id}`, {method: 'PATCH', body: JSON.stringify(body)}),
  deleteKnowledge: (id: string) => request<{knowledgeId: string; status: string; deletedAt: string; deletedBy: string}>(`/api/v1/knowledge/${id}`, {method: 'DELETE'}),
  extractKnowledge: (body: {ticketId: number; uids: string[]}) => request<{knowledge: Knowledge; extraction: {ticketId: number; ticketNo?: string; auditOperationCount: number; acceptedOperationCount: number; ignoredOperationCount: number; ignoredOperations: Array<{operationId?: unknown; reason: string}>}}>('/api/v1/knowledge/extract', {method: 'POST', body: JSON.stringify(body)}),
  publishKnowledge: (id: string) => request<Record<string, unknown>>(`/api/v1/knowledge/${id}/publish`, {method: 'POST', body: '{}'}),
  submitKnowledgeReview: (id: string) => request<Knowledge>(`/api/v1/knowledge/${id}/submit-review`, {method: 'POST', body: '{}'}),
  runs: () => request<{items: Run[]}>('/api/v1/runs'),
  run: (id: string) => request<Run>(`/api/v1/runs/${id}`),
  plan: (body: {knowledgeId: string; ticketId: number; parameters: Record<string, unknown>}) => request<Run>('/api/v1/runs/plan', {method: 'POST', body: JSON.stringify(body)}),
  approve: (id: string, planHash: string) => request<Run>(`/api/v1/runs/${id}/approve`, {method: 'POST', body: JSON.stringify({planHash})}),
  action: (id: string, action: 'pause' | 'resume' | 'cancel') => request<Run>(`/api/v1/runs/${id}/${action}`, {method: 'POST', body: '{}'}),
  resumeInterrupt: (runId: string, interruptId: string, payload: Record<string, unknown>) => request<Run>(`/api/v1/runs/${runId}/interrupts/${interruptId}/resume`, {method: 'POST', body: JSON.stringify({payload})}),
  updateCredential: (runId: string, apiKey: string) => request<Run>(`/api/v1/runs/${runId}/credential`, {method: 'POST', body: JSON.stringify({apiKey})}),
  artifact: (runId: string, nodeId: string) => request<{data: Record<string, unknown>}>(`/api/v1/runs/${runId}/nodes/${nodeId}/artifact`),
  retryNode: (runId: string, nodeId: string, decision: 'retry' | 'mark_failed') => request<Run>(`/api/v1/runs/${runId}/nodes/${nodeId}/retry`, {method: 'POST', body: JSON.stringify({decision})}),
}
