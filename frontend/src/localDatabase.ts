/** Browser-only business storage. Lists never read result payloads. */
export const DATABASE_NAME = "itsm-runtime-studio";
export const RESULT_BUDGET_BYTES = 256 * 1024 * 1024;
export const POLLED_STATUSES = new Set(["QUEUED", "RUNNING", "PAUSE_REQUESTED", "CANCEL_REQUESTED", "DISPATCHING"]);
export type StoredIdentity = { id: string; kind: "knowledge" | "run" | "extraction" };
type RecordValue = StoredIdentity & Record<string, unknown>;
export type LocalSummary = StoredIdentity & {
  name: string; summary: string; status: string; createdAt: string; updatedAt: string;
  ticketId?: number; knowledgeId?: string; currentNodeId?: string; source?: string;
  syncTec01?: boolean; tec01RunId?: string; syncStatus?: string; lastEventId: number;
  nodeCount: number; completedNodes: number; supported: boolean; approved: boolean;
  matchPhrases: string[]; systemKeys: string[]; polling: number; resultBytes: number;
};
export type ListQuery = { kind: StoredIdentity["kind"]; page?: number; pageSize?: number; keyword?: string; status?: string; exact?: Record<string, string> };
export type ListPage = { items: LocalSummary[]; total: number; page: number; totalPages: number };
type Payload = { id: string; runId: string; nodeId: string; value: unknown; bytes: number; attemptId: string };
export type StorageChange = { id: string; kind: StoredIdentity["kind"] };
export class BrowserStorageError extends Error {
  constructor(message: string, readonly code: "QUOTA" | "READ_ONLY" | "WRITE_FAILED") { super(message); }
}
const request = <T>(value: IDBRequest<T>) => new Promise<T>((resolve, reject) => { value.onsuccess = () => resolve(value.result); value.onerror = () => reject(value.error); });
const completed = (tx: IDBTransaction) => new Promise<void>((resolve, reject) => { tx.oncomplete = () => resolve(); tx.onabort = tx.onerror = () => reject(tx.error || new Error("本地存储事务未完成")); });
const bytes = (value: unknown) => new TextEncoder().encode(JSON.stringify(value)).byteLength;
const supported = new Set(["sql_read", "condition", "hitl_form", "end"]);
const asRecord = (value: unknown): Record<string, unknown> => value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {};
function polling(item: RecordValue) { return item.kind === "run" ? POLLED_STATUSES.has(String(item.status)) : item.kind === "extraction" && (item.status === "EXTRACTING" || item.syncTec01 && item.syncStatus === "PENDING"); }
function summarize(item: RecordValue, resultBytes: number): LocalSummary {
  const flow = asRecord(item.kind === "run" ? item.workflowSnapshot : item.workflowDefinition), nodes = (flow.nodes || []) as Array<{ type: string }>;
  const plan = asRecord(item.plan), confirmation = asRecord(item.confirmation);
  return {
    id: item.id, kind: item.kind, name: String(item.name || ""), summary: String(item.summary || ""), status: String(item.status || "DRAFT"),
    createdAt: String(item.createdAt || ""), updatedAt: String(item.updatedAt || ""), ticketId: item.ticketId as number | undefined,
    knowledgeId: item.knowledgeId as string | undefined, currentNodeId: item.currentNodeId as string | undefined, source: item.source as string | undefined,
    syncTec01: Boolean(item.syncTec01), tec01RunId: item.tec01RunId as string | undefined, syncStatus: item.syncStatus as string | undefined,
    lastEventId: Number(item.lastEventId || 0), nodeCount: nodes.length, completedNodes: Object.values(asRecord(item.nodeStates)).filter(status => ["SUCCEEDED", "SKIPPED", "FAILED", "TERMINATED", "CANCELLED"].includes(String(status))).length,
    supported: nodes.every(node => supported.has(node.type)), approved: Boolean(plan.planMaterialHash && confirmation.planHash === plan.planMaterialHash),
    matchPhrases: (item.matchPhrases || []) as string[], systemKeys: (item.systemKeys || []) as string[], polling: polling(item) ? 1 : 0, resultBytes,
  };
}
function split(item: RecordValue) {
  const detail = { ...item }, outputs = asRecord(item.nodeOutputs);
  if (item.kind === "run") detail.nodeOutputs = {};
  return { detail, outputs };
}
function migration(store: IDBObjectStore, tx: IDBTransaction) {
  let total = 0;
  const cursor = store.openCursor();
  cursor.onsuccess = () => {
    const entry = cursor.result;
    if (!entry) { tx.objectStore("settings").put({ id: "resultBytes", value: total }); tx.db.deleteObjectStore("items"); return; }
    try {
      const value = entry.value as RecordValue, { detail, outputs } = split(value);
      let size = 0;
      for (const [nodeId, output] of Object.entries(outputs)) {
        const outputBytes = bytes(output); size += outputBytes;
        tx.objectStore("outputs").put({ id: JSON.stringify([value.id, nodeId]), runId: value.id, nodeId, value: output, bytes: outputBytes, attemptId: String(asRecord(asRecord(value.nodeResults)[nodeId]).attemptId || "") });
      }
      detail.outputNodeIds = Object.keys(outputs);
      tx.objectStore("details").put(detail);
      tx.objectStore("summaries").put(summarize(value, size));
      total += size;
      entry.delete(); // versionchange transaction rollback also restores v1.
      entry.continue();
    } catch { tx.abort(); }
  };
}
function matches(item: LocalSummary, query: ListQuery) {
  const exact = query.exact || {}, keyword = (query.keyword || "").toLowerCase();
  const haystack = `${item.id} ${item.name} ${item.summary} ${item.ticketId || ""} ${item.currentNodeId || ""} ${item.matchPhrases.join(" ")} ${item.systemKeys.join(" ")}`.toLowerCase();
  if (keyword && !haystack.includes(keyword)) return false;
  if (exact.ticketId && String(item.ticketId || "") !== exact.ticketId.trim()) return false;
  if (exact.knowledgeId && String(item.kind === "knowledge" ? item.id : item.knowledgeId || "") !== exact.knowledgeId.trim()) return false;
  if (exact.runId && item.id !== exact.runId.trim() || exact.nameExact && item.name !== exact.nameExact.trim() || exact.sourceType && item.source !== exact.sourceType.trim() || exact.currentNodeId && item.currentNodeId !== exact.currentNodeId.trim()) return false;
  if (!query.status) return true;
  if (item.kind === "knowledge") return query.status === "UNSUPPORTED" ? !item.supported : query.status === "DRAFT" ? item.supported : item.status === query.status;
  if (query.status === "ACTIVE") return POLLED_STATUSES.has(item.status);
  if (query.status === "WAITING") return ["WAITING_INPUT", "PAUSED", "PLANNED", "UNKNOWN"].includes(item.status);
  if (query.status === "FAILED") return ["FAILED", "TERMINATED", "CANCELLED"].includes(item.status);
  return item.status === query.status;
}

export function createLocalDatabase(factory: IDBFactory = indexedDB, name = DATABASE_NAME, budgetBytes = RESULT_BUDGET_BYTES) {
  let opening: Promise<IDBDatabase> | undefined, connection: IDBDatabase | undefined;
  let readOnly = false, warning = "";
  const listeners = new Set<(changes: StorageChange[]) => void>();
  const pending = new Map<string, StorageChange>(); let notifying = false;
  const notify = (change: StorageChange) => {
    pending.set(change.id, change);
    if (notifying) return;
    notifying = true; queueMicrotask(() => { notifying = false; const changes = [...pending.values()]; pending.clear(); listeners.forEach(listener => listener(changes)); });
  };
  const openLegacy = () => new Promise<IDBDatabase>((resolve, reject) => {
    const fallback = factory.open(name);
    fallback.onsuccess = () => { readOnly = true; connection = fallback.result; resolve(fallback.result); };
    fallback.onerror = () => reject(new BrowserStorageError("浏览器本地数据库无法打开；不要清理站点数据，请先保留原始数据。", "READ_ONLY"));
  });
  const open = () => {
    if (!opening) opening = new Promise<IDBDatabase>((resolve, reject) => {
      const operation = factory.open(name, 2);
      operation.onblocked = () => { warning = "本地数据库升级被其他标签页阻止，请关闭旧版Studio标签页；原数据不会删除。"; notify({ id: "@storage", kind: "run" }); };
      operation.onupgradeneeded = () => {
        const database = operation.result, tx = operation.transaction!;
        const summaries = database.createObjectStore("summaries", { keyPath: "id" });
        summaries.createIndex("kindUpdated", ["kind", "updatedAt"]);
        summaries.createIndex("kindStatusUpdated", ["kind", "status", "updatedAt"]);
        summaries.createIndex("polling", "polling");
        database.createObjectStore("details", { keyPath: "id" });
        database.createObjectStore("outputs", { keyPath: "id" }).createIndex("runId", "runId");
        database.createObjectStore("settings", { keyPath: "id" });
        if (database.objectStoreNames.contains("items")) migration(tx.objectStore("items"), tx);
        else tx.objectStore("settings").put({ id: "resultBytes", value: 0 });
      };
      operation.onsuccess = () => {
        connection = operation.result; warning = "";
        connection.onversionchange = () => { connection?.close(); connection = undefined; opening = undefined; warning = "另一个标签页正在升级本地数据库，请刷新页面后继续。"; notify({ id: "@storage", kind: "run" }); };
        resolve(operation.result);
      };
      operation.onerror = () => {
        warning = "本地数据库升级失败，已回滚并保留旧数据；当前只读，请先导出备份并释放空间后刷新重试。";
        openLegacy().then(resolve, reject);
      };
    });
    return opening;
  };
  const write = async <T>(operation: (tx: IDBTransaction) => Promise<T>): Promise<T> => {
    const database = await open();
    if (readOnly) throw new BrowserStorageError(warning, "READ_ONLY");
    const tx = database.transaction(["summaries", "details", "outputs", "settings"], "readwrite"), done = completed(tx);
    try { const value = await operation(tx); await done; return value; }
    catch (error) {
      try { tx.abort(); } catch { /* transaction may already have rolled back */ }
      await done.catch(() => {});
      if (error instanceof BrowserStorageError) throw error;
      const quota = (error as Error)?.name === "QuotaExceededError" || tx.error?.name === "QuotaExceededError";
      throw new BrowserStorageError(quota ? "浏览器存储已满，本次结果和游标未保存。请手动导出或清理已完成结果，再点击恢复同步；不会重新执行SQL。" : "浏览器本地写入失败，本次事务已回滚。请检查存储权限后手动恢复同步。", quota ? "QUOTA" : "WRITE_FAILED");
    }
  };
  const readDetail = async <T extends StoredIdentity>(id: string, withOutputs = true): Promise<T | undefined> => {
    const database = await open();
    if (readOnly && database.version < 2) return request(database.transaction("items").objectStore("items").get(id));
    const tx = database.transaction(["details", "outputs"]), value = await request(tx.objectStore("details").get(id)) as RecordValue | undefined;
    if (!value) return;
    if (withOutputs && value.kind === "run") {
      const outputs = await request(tx.objectStore("outputs").index("runId").getAll(id)) as Payload[];
      value.nodeOutputs = Object.fromEntries(outputs.map(output => [output.nodeId, output.value]));
    }
    return value as unknown as T;
  };
  const save = async (tx: IDBTransaction, value: RecordValue) => {
      const details = tx.objectStore("details"), outputsStore = tx.objectStore("outputs"), summaries = tx.objectStore("summaries");
      const [previous, oldSummary, setting] = await Promise.all([request(details.get(value.id)), request(summaries.get(value.id)), request(tx.objectStore("settings").get("resultBytes"))]);
      const { detail, outputs } = split(value);
      const outputIds = new Set((previous?.outputNodeIds || []) as string[]);
      let resultBytes = Number(oldSummary?.resultBytes || 0), delta = 0;
      for (const [nodeId, output] of Object.entries(outputs)) {
        const id = JSON.stringify([value.id, nodeId]), old = await request(outputsStore.get(id)) as Payload | undefined;
        const attemptId = String(asRecord(asRecord(value.nodeResults)[nodeId]).attemptId || "");
        if (old && old.attemptId === attemptId && outputIds.has(nodeId)) continue;
        const size = bytes(output), difference = size - (old?.bytes || 0);
        delta += difference; resultBytes += difference; outputIds.add(nodeId);
        outputsStore.put({ id, runId: value.id, nodeId, value: output, bytes: size, attemptId });
      }
      const nextBytes = Number(setting?.value || 0) + delta;
      if (delta > 0 && nextBytes > budgetBytes) throw new BrowserStorageError("浏览器完整调试结果超过256 MiB软预算，本次结果和游标未保存。请手动导出或清理已完成结果，再恢复同步；原始结果不会截断。", "QUOTA");
      detail.outputNodeIds = [...outputIds];
      details.put(detail); summaries.put(summarize(value, resultBytes));
      tx.objectStore("settings").put({ id: "resultBytes", value: nextBytes });
  };
  const put = async <T extends StoredIdentity>(item: T) => {
    await write(tx => save(tx, item as unknown as RecordValue));
    notify({ id: item.id, kind: item.kind });
  };
  const scanSummaries = async (kind?: StoredIdentity["kind"]): Promise<LocalSummary[]> => {
    const database = await open();
    if (readOnly && database.version < 2) {
      const records = await request(database.transaction("items").objectStore("items").getAll()) as RecordValue[];
      return records.filter(item => !kind || item.kind === kind).map(item => summarize(item, 0));
    }
    const tx = database.transaction("summaries"), index = tx.objectStore("summaries").index("kindUpdated");
    return new Promise((resolve, reject) => {
      const items: LocalSummary[] = [], cursor = index.openCursor(kind ? IDBKeyRange.bound([kind, ""], [kind, "\uffff"]) : undefined, "prev");
      cursor.onsuccess = () => { const entry = cursor.result; if (entry) { items.push(entry.value); entry.continue(); } else resolve(items); };
      cursor.onerror = () => reject(cursor.error);
    });
  };
  return {
    open, put, get: readDetail,
    update: async <T extends StoredIdentity>(id: string, updater: (item: T) => T | undefined): Promise<T | undefined> => {
      let updated: T | undefined;
      await write(async tx => {
        const current = await request(tx.objectStore("details").get(id));
        if (!current) return;
        updated = updater(current as T);
        if (updated) await save(tx, updated as unknown as RecordValue);
      });
      if (updated) notify({ id, kind: updated.kind });
      return updated;
    },
    warning: () => ({ readOnly, warning }),
    subscribe: (listener: (changes: StorageChange[]) => void) => { listeners.add(listener); return () => { listeners.delete(listener); }; },
    close: () => { connection?.close(); connection = undefined; opening = undefined; },
    summary: async (id: string) => { const database = await open(); return readOnly ? (await scanSummaries()).find(item => item.id === id) : request(database.transaction("summaries").objectStore("summaries").get(id)) as Promise<LocalSummary | undefined>; },
    summaries: scanSummaries,
    list: async (query: ListQuery): Promise<ListPage> => {
      const database = await open(), size = query.pageSize || 20, wanted = Math.max(1, query.page || 1);
      if (readOnly && database.version < 2) {
        const items = (await scanSummaries(query.kind)).filter(item => matches(item, query));
        const totalPages = Math.ceil(items.length / size), page = Math.min(wanted, totalPages || 1);
        return { items: items.slice((page - 1) * size, page * size), total: items.length, page, totalPages };
      }
      const store = database.transaction("summaries").objectStore("summaries");
      const singleStatus = query.kind === "run" && query.status && !["ACTIVE", "WAITING", "FAILED"].includes(query.status);
      const index = store.index(singleStatus ? "kindStatusUpdated" : "kindUpdated");
      const range = singleStatus ? IDBKeyRange.bound([query.kind, query.status!, ""], [query.kind, query.status!, "\uffff"]) : IDBKeyRange.bound([query.kind, ""], [query.kind, "\uffff"]);
      return new Promise((resolve, reject) => {
        let total = 0, lastPage: LocalSummary[] = []; const pageItems: LocalSummary[] = [], cursor = index.openCursor(range, "prev");
        cursor.onsuccess = () => {
          const entry = cursor.result;
          if (!entry) {
            const totalPages = Math.ceil(total / size), page = Math.min(wanted, totalPages || 1);
            resolve({ items: page === wanted ? pageItems : lastPage, total, page, totalPages }); return;
          }
          const item = entry.value as LocalSummary;
          if (matches(item, query)) {
            if (total % size === 0) lastPage = [];
            lastPage.push(item);
            if (total >= (wanted - 1) * size && total < wanted * size) pageItems.push(item);
            total++;
          }
          entry.continue();
        };
        cursor.onerror = () => reject(cursor.error);
      });
    },
    active: async () => {
      const database = await open();
      return readOnly ? [] : request(database.transaction("summaries").objectStore("summaries").index("polling").getAll(1)) as Promise<LocalSummary[]>;
    },
    output: async (runId: string, nodeId: string) => {
      const database = await open();
      if (readOnly && database.version < 2) return asRecord((await readDetail<RecordValue>(runId))?.nodeOutputs)[nodeId];
      return ((await request(database.transaction("outputs").objectStore("outputs").get(JSON.stringify([runId, nodeId])))) as Payload | undefined)?.value;
    },
    status: async () => {
      const database = await open();
      const value = database.version < 2 ? 0 : (await request(database.transaction("settings").objectStore("settings").get("resultBytes")))?.value || 0;
      return { readOnly, warning, resultBytes: Number(value), budgetBytes, version: database.version };
    },
    exportAll: async () => {
      const database = await open();
      if (database.version < 2) return request(database.transaction("items").objectStore("items").getAll());
      const ids = await request(database.transaction("details").objectStore("details").getAllKeys());
      const result = [];
      for (const id of ids) result.push(await readDetail(String(id)));
      return result;
    },
    remove: async (id: string) => {
      let kind: StoredIdentity["kind"] = "run";
      await write(async tx => {
        const item = await request(tx.objectStore("summaries").get(id)) as LocalSummary | undefined;
        if (!item) return;
        kind = item.kind;
        const keys = await request(tx.objectStore("outputs").index("runId").getAllKeys(id));
        keys.forEach(key => tx.objectStore("outputs").delete(key));
        tx.objectStore("details").delete(id); tx.objectStore("summaries").delete(id);
        const setting = await request(tx.objectStore("settings").get("resultBytes"));
        tx.objectStore("settings").put({ id: "resultBytes", value: Math.max(0, Number(setting?.value || 0) - item.resultBytes) });
      });
      notify({ id, kind });
    },
    clearResults: async (id: string) => {
      await write(async tx => {
        const [summary, detail] = await Promise.all([request(tx.objectStore("summaries").get(id)), request(tx.objectStore("details").get(id))]);
        if (!summary || summary.kind !== "run" || !["SUCCEEDED", "TERMINATED", "CANCELLED"].includes(summary.status)) throw new BrowserStorageError("仅可手动清理已完成或已终止运行的结果；知识、计划和可恢复运行不会删除。", "WRITE_FAILED");
        const keys = await request(tx.objectStore("outputs").index("runId").getAllKeys(id));
        keys.forEach(key => tx.objectStore("outputs").delete(key));
        tx.objectStore("summaries").put({ ...summary, resultBytes: 0 });
        tx.objectStore("details").put({ ...detail, outputNodeIds: [], outputsCleared: true });
        const setting = await request(tx.objectStore("settings").get("resultBytes"));
        tx.objectStore("settings").put({ id: "resultBytes", value: Math.max(0, Number(setting?.value || 0) - summary.resultBytes) });
      });
      notify({ id, kind: "run" });
    },
  };
}
