import assert from "node:assert/strict";
import fs from "node:fs";
import test from "node:test";
import ts from "typescript";

const source = fs.readFileSync(new URL("../src/runGuards.ts", import.meta.url), "utf8");
const { outputText } = ts.transpileModule(source, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ES2022 } });
const { needsExecutionReview } = await import(`data:text/javascript;base64,${Buffer.from(outputText).toString("base64")}`);

test("a lost dispatch response requires review even when node states are READY", () => {
  assert.equal(needsExecutionReview("UNKNOWN", { a: "READY", b: "PENDING" }), true);
});

test("unconfirmed active or unknown node states require review", () => {
  assert.equal(needsExecutionReview("FAILED", { a: "UNKNOWN" }), true);
  assert.equal(needsExecutionReview("PAUSED", { a: "RUNNING" }), true);
});

test("a fresh plan and confirmed paused outputs do not imply unknown execution", () => {
  assert.equal(needsExecutionReview("PLANNED", { a: "READY" }), false);
  assert.equal(needsExecutionReview("PAUSED", { a: "SUCCEEDED", b: "READY" }), false);
});
