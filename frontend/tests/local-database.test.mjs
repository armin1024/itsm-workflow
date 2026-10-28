import assert from "node:assert/strict";
import fs from "node:fs";
import test from "node:test";
import ts from "typescript";
import { IDBFactory, IDBKeyRange, IDBObjectStore } from "fake-indexeddb";
globalThis.IDBKeyRange = IDBKeyRange;
const source = fs.readFileSync(new URL("../src/localDatabase.ts", import.meta.url), "utf8");
const { outputText } = ts.transpileModule(source, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ES2022 } });
const { createLocalDatabase, BrowserStorageError } = await import(`data:text/javascript;base64,${Buffer.from(outputText).toString("base64")}`);
const run = (id = "run1", extra = {}) => ({ id, kind: "run", name: id, status: "RUNNING", createdAt: "2026-10-26T00:00:00Z", updatedAt: "2026-10-26T00:00:00Z", workflowSnapshot: { nodes: [{ id: "sql1", type: "sql_read" }], edges: [] }, nodeStates: { sql1: "RUNNING" }, nodeOutputs: {}, nodeResults: {}, events: [], lastEventId: 0, ...extra });
const req = value => new Promise((resolve, reject) => { value.onsuccess = () => resolve(value.result); value.onerror = () => reject(value.error); });

test("extraction completeness survives local knowledge save, reload and export", async () => {
  const database = createLocalDatabase(new IDBFactory(), "partial-test");
  const extractionReview = { extractionOutcome: "PARTIAL_SUCCESS", publishable: false, requestId: "req-1", diagnostics: { failedOperationCount: 1, failedOperations: [{ operationId: "op-1", auditIndex: 0, code: "SQL_PARSE_FAILED", message: "SQL无法解析" }], blockingIssues: [] } };
  const item = { id: "partial-knowledge", kind: "knowledge", name: "部分成功草稿", summary: "说明", workflowDefinition: { nodes: [], edges: [] }, extractionReview, createdAt: "2026-10-27T00:00:00Z", updatedAt: "2026-10-27T00:00:00Z" };
  await database.put(item);
  database.close();
  assert.deepEqual((await database.get(item.id)).extractionReview, extractionReview);
  assert.deepEqual((await database.exportAll())[0].extractionReview, extractionReview);
  database.close();
});
async function legacy(factory, items, keepOpen = false) {
  const opening = factory.open("test", 1);
  opening.onupgradeneeded = () => opening.result.createObjectStore("items", { keyPath: "id" });
  const connection = await req(opening), tx = connection.transaction("items", "readwrite");
  for (const item of items) tx.objectStore("items").put(item);
  await new Promise((resolve, reject) => { tx.oncomplete = resolve; tx.onabort = () => reject(tx.error); });
  if (!keepOpen) connection.close();
  return connection;
}

test("v1 migration splits outputs and preserves all records without enforcing budget retroactively", async () => {
  const factory = new IDBFactory(), value = run("old", { status: "SUCCEEDED", lastEventId: 9, nodeOutputs: { sql1: { data: [{ name: "完整结果".repeat(1000) }] } } });
  await legacy(factory, [value, { id: "knowledge", kind: "knowledge", name: "知识", summary: "说明", workflowDefinition: { nodes: [], edges: [] }, createdAt: value.createdAt, updatedAt: value.updatedAt }]);
  const database = createLocalDatabase(factory, "test", 1);
  const connection = await database.open();
  assert.equal(connection.version, 2);
  assert.deepEqual([...connection.objectStoreNames], ["details", "outputs", "settings", "summaries"]);
  const metadata = await database.get("old", false);
  assert.deepEqual(metadata.nodeOutputs, {});
  assert.deepEqual(metadata.outputNodeIds, ["sql1"]);
  assert.deepEqual((await database.get("old")).nodeOutputs, value.nodeOutputs);
  assert.equal((await database.get("knowledge")).name, "知识");
  assert.ok((await database.status()).resultBytes > 1);
  database.close();
});

test("migration failure rolls the version and old store back and exposes read-only export", async () => {
  const factory = new IDBFactory(), value = run("old", { nodeOutputs: { sql1: { data: [1, 2, 3] } } });
  await legacy(factory, [value]);
  const original = IDBObjectStore.prototype.put;
  IDBObjectStore.prototype.put = function (...args) { if (this.name === "outputs") throw new DOMException("disk quota", "QuotaExceededError"); return original.apply(this, args); };
  const database = createLocalDatabase(factory, "test");
  try {
    const connection = await database.open();
    assert.equal(connection.version, 1);
    assert.deepEqual([...connection.objectStoreNames], ["items"]);
    assert.equal((await database.status()).readOnly, true);
    assert.match((await database.status()).warning, /回滚/);
    assert.deepEqual(await database.exportAll(), [value]);
    await assert.rejects(database.put(run("new")), error => error instanceof BrowserStorageError && error.code === "READ_ONLY");
  } finally { IDBObjectStore.prototype.put = original; database.close(); }
});

test("blocked version upgrade has a visible warning and continues after closing legacy tab", async () => {
  const factory = new IDBFactory(), old = await legacy(factory, [run()], true), database = createLocalDatabase(factory, "test");
  const operation = database.open();
  await new Promise(resolve => setTimeout(resolve, 20));
  assert.match(database.warning().warning, /其他标签页/);
  old.close(); assert.equal((await operation).version, 2);
  assert.equal(database.warning().warning, ""); database.close();
});

test("list and active queries read summary indexes, never complete outputs", async () => {
  const database = createLocalDatabase(new IDBFactory(), "test");
  for (let i = 0; i < 45; i++) await database.put(run(`run${String(i).padStart(2, "0")}`, { status: i === 3 ? "RUNNING" : "SUCCEEDED", nodeOutputs: { sql1: { data: ["x".repeat(2000)] } } }));
  const original = IDBObjectStore.prototype.getAll;
  IDBObjectStore.prototype.getAll = function (...args) { assert.notEqual(this.name, "outputs"); assert.notEqual(this.name, "details"); return original.apply(this, args); };
  try {
    const page = await database.list({ kind: "run", page: 2, pageSize: 20 });
    assert.equal(page.total, 45); assert.equal(page.items.length, 20);
    assert.ok(page.items.every(item => !("nodeOutputs" in item) && !("workflowSnapshot" in item)));
    assert.equal((await database.active()).length, 1);
    assert.equal((await database.list({ kind: "run", keyword: "run03", status: "ACTIVE" })).total, 1);
    assert.equal((await database.list({ kind: "run", page: 100, pageSize: 20 })).page, 3);
  } finally { IDBObjectStore.prototype.getAll = original; database.close(); }
});

test("quota failure rolls back raw result, summary and lastEventId without notifying success", async () => {
  const database = createLocalDatabase(new IDBFactory(), "test", 100), changes = [];
  await database.put(run());
  const stop = database.subscribe(value => changes.push(...value));
  await assert.rejects(database.update("run1", current => ({ ...current, lastEventId: 6, status: "SUCCEEDED", nodeOutputs: { sql1: { data: ["x".repeat(500)] } } })), error => error.code === "QUOTA");
  assert.equal((await database.get("run1", false)).lastEventId, 0);
  assert.equal((await database.summary("run1")).status, "RUNNING");
  assert.equal(await database.output("run1", "sql1"), undefined);
  assert.equal((await database.status()).resultBytes, 0);
  assert.deepEqual(changes, []); stop(); database.close();
});

test("atomic event update persists one complete output and exposes it lazily", async () => {
  const database = createLocalDatabase(new IDBFactory(), "test"), changes = [];
  await database.put(run());
  const stop = database.subscribe(value => changes.push(...value));
  const output = { rowCount: 10000, data: Array.from({ length: 10000 }, (_, id) => ({ id })) };
  await database.update("run1", current => ({ ...current, status: "SUCCEEDED", lastEventId: 3, nodeOutputs: { sql1: output }, nodeResults: { sql1: { attemptId: "attempt1" } } }));
  assert.equal((await database.summary("run1")).status, "SUCCEEDED");
  assert.equal((await database.get("run1", false)).lastEventId, 3);
  assert.deepEqual(await database.output("run1", "sql1"), output);
  assert.equal((await database.exportAll())[0].nodeOutputs.sql1.data.length, 10000);
  assert.deepEqual(changes, [{ id: "run1", kind: "run" }]); stop(); database.close();
});

test("manual cleanup protects knowledge and recoverable runs and never deletes plan metadata", async () => {
  const database = createLocalDatabase(new IDBFactory(), "test");
  await database.put(run("finished", { status: "SUCCEEDED", nodeOutputs: { sql1: { data: [1, 2] } } }));
  for (const status of ["RUNNING", "PAUSED", "WAITING_INPUT", "FAILED", "UNKNOWN"]) {
    const id = status.toLowerCase(); await database.put(run(id, { status }));
    await assert.rejects(database.clearResults(id));
    assert.equal((await database.get(id)).status, status);
  }
  await database.put({ id: "knowledge", kind: "knowledge", name: "保留知识", workflowDefinition: { nodes: [], edges: [] } });
  await assert.rejects(database.clearResults("knowledge"));
  await database.clearResults("finished");
  assert.equal(await database.output("finished", "sql1"), undefined);
  assert.ok((await database.get("finished")).workflowSnapshot);
  assert.equal((await database.get("finished")).outputsCleared, true);
  assert.equal((await database.get("knowledge")).name, "保留知识");
  assert.equal((await database.status()).resultBytes, 0); database.close();
});
