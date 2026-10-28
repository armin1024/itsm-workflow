import { useEffect, useState } from "react";
import type { EditableWorkflow } from "./WorkflowEditor";
import { createLocalDatabase, type LocalSummary, type ListQuery } from "./localDatabase";
export { BrowserStorageError, RESULT_BUDGET_BYTES, type LocalSummary } from "./localDatabase";

export type LocalKnowledge = {
  id: string; kind: "knowledge"; name: string; summary: string;
  matchPhrases: string[]; negativePhrases: string[]; systemKeys: string[];
  workflowDefinition: EditableWorkflow; source: "MANUAL" | "EXTRACTION";
  ticketId?: number; createdAt: string; updatedAt: string;
  extractionReview?: { extractionOutcome?: string; publishable?: boolean; diagnostics?: unknown; requestId?: string };
};
export type LocalRun = {
  id: string; kind: "run"; knowledgeId: string; name: string; ticketId: number;
  workflowSnapshot: EditableWorkflow; workflowContentHash: string;
  runInputs: Record<string, unknown>; status: string; nodeStates: Record<string, string>;
  nodeOutputs: Record<string, unknown>; selectedRoutes: Record<string, string>;
  currentNodeId: string; interaction?: Record<string, unknown> | null;
  diagnostic?: unknown; dispatchId?: string; lastEventId: number;
  nodeResults?: Record<string, Record<string, unknown>>;
  outputNodeIds?: string[]; outputsCleared?: boolean; eventHistoryIncomplete?: boolean;
  syncTec01: boolean; tec01RunId?: string; events: Record<string, unknown>[];
  plan?: Record<string, unknown>; removeAfterCancel?: boolean;
  confirmation?: { planHash: string; confirmedAt: string };
  syncStatus?: string; callbackError?: unknown;
  createdAt: string; updatedAt: string;
};
export type LocalExtraction = {
  id: string; kind: "extraction"; status: string; ticketId?: number;
  syncTec01: boolean; lastEventId: number; events: Record<string, unknown>[];
  proposal?: Record<string, unknown>; validation?: Record<string, unknown>;
  diagnostics?: unknown; error?: unknown; createdAt: string; updatedAt: string;
  extractionOutcome?: string; publishable?: boolean;
  syncStatus?: string; callbackError?: unknown;
};
export type LocalItem = LocalKnowledge | LocalRun | LocalExtraction;
let database: ReturnType<typeof createLocalDatabase> | undefined;
const db = () => database ||= createLocalDatabase();
export const localId = (prefix: string) => `${prefix}_${Array.from(crypto.getRandomValues(new Uint8Array(16)), (value) => value.toString(16).padStart(2, "0")).join("")}`;
export const now = () => new Date().toISOString();
const credentialKeys = new Set(["authorization", "aopsapikey", "aopsbottoken", "apikey", "accesstoken", "refreshtoken", "studioadmintoken", "runtimeservicetoken", "token", "password", "passwd", "secret"]);
export function rejectStoredCredentials(value: unknown, path = "$"): void {
  if (Array.isArray(value)) value.forEach((item, index) => rejectStoredCredentials(item, `${path}[${index}]`));
  else if (value && typeof value === "object") for (const [key, item] of Object.entries(value)) {
    if (credentialKeys.has(key.toLowerCase().replaceAll(/[_-]/g, ""))) throw new Error(`浏览器禁止保存凭据字段：${path}.${key}。执行凭据请仅填写 AOPS_API_KEY 专用输入框。`);
    rejectStoredCredentials(item, `${path}.${key}`);
  }
}
export const browserStore = {
  summaries: (kind?: LocalItem["kind"]) => db().summaries(kind),
  list: (query: ListQuery) => db().list(query),
  active: () => db().active(),
  get: <T extends LocalItem>(id: string) => db().get<T>(id),
  metadata: <T extends LocalItem>(id: string) => db().get<T>(id, false),
  output: (runId: string, nodeId: string) => db().output(runId, nodeId),
  status: () => db().status(),
  warning: () => db().warning(),
  exportAll: () => db().exportAll(),
  clearResults: (runId: string) => db().clearResults(runId),
  subscribe: (listener: (changes: Array<{ id: string; kind: LocalItem["kind"] }>) => void) => db().subscribe(listener),
  update: <T extends LocalItem>(id: string, updater: (item: T) => T | undefined) => db().update(id, updater),
  put: async (value: LocalItem) => {
    // SQL result columns are data; inspect definitions and configured inputs
    // so execution credentials cannot accidentally enter browser storage.
    if (value.kind === "run") rejectStoredCredentials({ workflowSnapshot: value.workflowSnapshot, runInputs: value.runInputs, plan: value.plan });
    else if (value.kind === "extraction") rejectStoredCredentials(value.proposal);
    else rejectStoredCredentials(value);
    await db().put(value);
  },
  remove: (id: string) => db().remove(id),
};
export function useLocalDetail<T extends LocalItem>(id: string | null) {
  const [item, setItem] = useState<T>(), [error, setError] = useState("");
  useEffect(() => {
    let live = true, generation = 0;
    const load = () => { const current = ++generation; if (!id) { setItem(undefined); return; } void browserStore.metadata<T>(id).then(value => { if (live && current === generation) { setItem(value); setError(""); } }).catch(value => { if (live && current === generation) setError(value.message); }); };
    setItem(undefined); load(); const stop = browserStore.subscribe(changes => { if (changes.some(change => change.id === id)) load(); });
    return () => { live = false; stop(); };
  }, [id]);
  return { item, error };
}
export function useLocalPage(query: ListQuery) {
  const [page, setPage] = useState({ items: [] as LocalSummary[], total: 0, page: 1, totalPages: 0 }), [error, setError] = useState(""), [loading, setLoading] = useState(true), [refresh, setRefresh] = useState(0);
  const key = JSON.stringify(query);
  useEffect(() => {
    let live = true, generation = 0;
    const load = async () => { const current = ++generation; setLoading(true); try { const value = await browserStore.list(query); if (live && current === generation) { setPage(value); setError(""); } } catch (value) { if (live && current === generation) setError((value as Error).message); } finally { if (live && current === generation) setLoading(false); } };
    void load(); const stop = browserStore.subscribe(changes => { if (changes.some(change => change.kind === query.kind)) void load(); });
    return () => { live = false; stop(); };
  }, [key, refresh]);
  return { ...page, error, loading, reload: () => setRefresh(value => value + 1) };
}
export function downloadJson(name: string, value: unknown) {
  const href = URL.createObjectURL(new Blob([JSON.stringify(value, null, 2)], { type: "application/json;charset=utf-8" }));
  const anchor = document.createElement("a"); anchor.href = href; anchor.download = name; anchor.click(); URL.revokeObjectURL(href);
}
