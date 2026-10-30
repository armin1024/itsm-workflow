import assert from "node:assert/strict";
import fs from "node:fs";
import test from "node:test";
import ts from "typescript";
const source = fs.readFileSync(new URL("../src/runEvents.ts", import.meta.url), "utf8");
const { outputText } = ts.transpileModule(source, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ES2022 } });
const { applyRunEvent, pollDelay, mapWithConcurrency } = await import(`data:text/javascript;base64,${Buffer.from(outputText).toString("base64")}`);

test("visible and hidden polling cadence is bounded", () => { assert.equal(pollDelay(false), 2000); assert.equal(pollDelay(true), 5000); });
test("activity network work never exceeds four concurrent requests", async () => {
  let active = 0, maximum = 0, completed = 0;
  await mapWithConcurrency(Array.from({ length: 32 }, (_, id) => id), 4, async () => { active++; maximum = Math.max(maximum, active); await new Promise(resolve => setTimeout(resolve, 2)); active--; completed++; });
  assert.equal(maximum, 4); assert.equal(completed, 32);
});
test("a latest snapshot after an event ring gap restores facts without replaying historical actions", () => {
  const initial = { id: "r", kind: "run", events: [], nodeOutputs: {}, nodeStates: { a: "PENDING" }, selectedRoutes: {}, lastEventId: 0 };
  const result = applyRunEvent(initial, { eventId: 80, type: "RUN_RELEASED", snapshot: { status: "SUCCEEDED", nodeStates: { a: "SUCCEEDED" }, nodeOutputs: { a: { data: [{ value: 42 }] } }, selectedRoutes: {} } });
  assert.equal(result.lastEventId, 80); assert.equal(result.status, "SUCCEEDED");
  assert.equal(result.nodeOutputs.a.data[0].value, 42);
  assert.equal(result.events.length, 1); assert.equal(result.events[0].snapshot, undefined);
});
