import { useEffect, useState } from "react";
import type { EditableWorkflow } from "./WorkflowEditor";

export type LocalKnowledge = {
  id: string; kind: "knowledge"; name: string; summary: string;
  matchPhrases: string[]; negativePhrases: string[]; systemKeys: string[];
  workflowDefinition: EditableWorkflow; source: "MANUAL" | "EXTRACTION";
  ticketId?: number; createdAt: string; updatedAt: string;
};
export type LocalRun = {
  id: string; kind: "run"; knowledgeId: string; name: string; ticketId: number;
  workflowSnapshot: EditableWorkflow; workflowContentHash: string;
  runInputs: Record<string, unknown>; status: string; nodeStates: Record<string, string>;
  nodeOutputs: Record<string, unknown>; selectedRoutes: Record<string, string>;
  currentNodeId: string; interaction?: Record<string, unknown> | null;
  diagnostic?: unknown; dispatchId?: string; lastEventId: number;
  nodeResults?: Record<string, Record<string, unknown>>;
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
  syncStatus?: string; callbackError?: unknown;
};
type LocalItem = LocalKnowledge | LocalRun | LocalExtraction;
const changeEvent = "itsm-studio-browser-data";
let database: Promise<IDBDatabase> | undefined;
function db() {
  if (!database) database = new Promise((resolve, reject) => {
    const open = indexedDB.open("itsm-runtime-studio", 1);
    open.onupgradeneeded = () => open.result.createObjectStore("items", { keyPath: "id" });
    open.onsuccess = () => resolve(open.result);
    open.onerror = () => reject(new Error("浏览器本地存储不可用，请检查隐私模式或存储权限"));
  });
  return database;
}
async function transaction<T>(mode: IDBTransactionMode, operation: (store: IDBObjectStore) => IDBRequest<T>): Promise<T> {
  const database = await db();
  return new Promise((resolve, reject) => {
    const tx = database.transaction("items", mode), request = operation(tx.objectStore("items"));
    tx.oncomplete = () => { if (mode === "readwrite") window.dispatchEvent(new Event(changeEvent)); resolve(request.result); };
    tx.onerror = () => reject(new Error(tx.error?.name === "QuotaExceededError" ? "浏览器存储已满，请导出并删除旧的调试结果" : "无法保存浏览器本地数据"));
    tx.onabort = () => reject(new Error("浏览器存储事务未完成"));
  });
}
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
  all: () => transaction<LocalItem[]>("readonly", (store) => store.getAll()),
  get: <T extends LocalItem>(id: string) => transaction<T | undefined>("readonly", (store) => store.get(id)),
  put: async (value: LocalItem) => {
    // SQL result columns are data; inspect definitions and configured inputs
    // so execution credentials cannot accidentally enter browser storage.
    if (value.kind === "run") rejectStoredCredentials({ workflowSnapshot: value.workflowSnapshot, runInputs: value.runInputs, plan: value.plan });
    else if (value.kind === "extraction") rejectStoredCredentials(value.proposal);
    else rejectStoredCredentials(value);
    await transaction("readwrite", (store) => store.put(value));
  },
  remove: async (id: string) => { await transaction("readwrite", (store) => store.delete(id)); },
};
export function useBrowserItems<T extends LocalItem>(kind: T["kind"]) {
  const [items, setItems] = useState<T[]>([]), [error, setError] = useState("");
  useEffect(() => {
    let live = true;
    const load = () => browserStore.all().then((values) => { if (live) setItems(values.filter((item) => item.kind === kind) as T[]); }).catch((error) => { if (live) setError(error.message); });
    void load(); window.addEventListener(changeEvent, load);
    return () => { live = false; window.removeEventListener(changeEvent, load); };
  }, [kind]);
  return { items: items.sort((a, b) => ("updatedAt" in b ? b.updatedAt : "").localeCompare("updatedAt" in a ? a.updatedAt : "")), error };
}
export function downloadJson(name: string, value: unknown) {
  const href = URL.createObjectURL(new Blob([JSON.stringify(value, null, 2)], { type: "application/json;charset=utf-8" }));
  const anchor = document.createElement("a"); anchor.href = href; anchor.download = name; anchor.click(); URL.revokeObjectURL(href);
}
