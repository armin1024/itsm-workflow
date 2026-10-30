import assert from "node:assert/strict";
import fs from "node:fs";
import test from "node:test";
import ts from "typescript";
const source = fs.readFileSync(new URL("../src/logQuery.ts", import.meta.url), "utf8");
const { outputText } = ts.transpileModule(source, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ES2022 } });
const { buildLogQuery, composeLogFilters, validateLogFilters, effectiveLogQuery, apiErrorMessage, applyLocalDates, localDateTime } = await import(`data:text/javascript;base64,${Buffer.from(outputText).toString("base64")}`);

test("query removes empty dates, UI-only fields and stale URL cursor", () => {
  const values = buildLogQuery("fromTime=&toTime=&range=all&source=journal&cursor=old&peer=TEC01&keyword=%20测试%20");
  assert.equal(values.get("fromTime"), null);
  assert.equal(values.get("cursor"), null);
  assert.equal(values.get("source"), null);
  assert.equal(values.get("keyword"), "测试");
  assert.equal(values.get("peer"), "TEC01");
});

test("compiler diagnostic filters survive composition and API encoding", () => {
  const result = composeLogFilters({ peer: "COMPILER", direction: "INTERNAL", level: "ERROR", errorCode: "LLM_ANALYSIS_FAILED", reasonCode: "LLM_RESPONSE_SCHEMA_INVALID", errorField: "/steps/0/parameters", errorId: "err_fixture", stage: "ANALYZING_EVIDENCE" }, "应为数组");
  assert.deepEqual(result.errors, {});
  const query = buildLogQuery(result.params.toString());
  assert.equal(query.get("errorField"), "/steps/0/parameters");
  assert.equal(query.get("errorId"), "err_fixture");
  assert.equal(query.get("reasonCode"), "LLM_RESPONSE_SCHEMA_INVALID");
  assert.equal(query.get("stage"), "ANALYZING_EVIDENCE");
  assert.equal(query.get("keyword"), "应为数组");
  assert.ok(validateLogFilters(new URLSearchParams({errorField:"x".repeat(241)})).errorField);
});

test("local datetime picker values become timezone-aware ISO strings", () => {
  const result = applyLocalDates(new URLSearchParams(), "2026-10-23T09:00", "2026-10-23T10:00");
  assert.deepEqual(result.errors, {});
  assert.equal(result.params.get("fromTime"), new Date("2026-10-23T09:00").toISOString());
  assert.equal(localDateTime(result.params.get("fromTime")), "2026-10-23T09:00");
});

test("invalid dates, reversed intervals and malformed selections are blocked before fetch", () => {
  assert.ok(applyLocalDates(new URLSearchParams(), "invalid", "").errors.fromTime);
  assert.ok(applyLocalDates(new URLSearchParams(), "2026-10-24T09:00", "2026-10-23T09:00").errors.toTime);
  assert.ok(validateLogFilters(new URLSearchParams("level=全部")).level);
  assert.ok(validateLogFilters(new URLSearchParams("peer=unknown")).peer);
  assert.ok(composeLogFilters({range:"custom"}, "").errors.fromTime);
});

test("all conditions are committed together using technical event and operation values", () => {
  const result = composeLogFilters({range:"all",event:"CALL_FAILED",operation:"analyze_evidence",runId:"run1",level:"WARNING"}, "网关");
  assert.deepEqual(result.errors, {});
  assert.equal(result.params.get("event"), "CALL_FAILED");
  assert.equal(result.params.get("runId"), "run1");
  assert.equal(result.params.get("keyword"), "网关");
  assert.ok(composeLogFilters({range:"all"}, "x".repeat(201)).errors.keyword);
});

test("preset latest ranges resolve at refresh time, while returned strings can be frozen for paging", () => {
  const now = Date.parse("2026-10-23T10:00:00Z");
  const result = new URLSearchParams(effectiveLogQuery("range=24h&event=CALL_FAILED", now));
  assert.equal(result.get("fromTime"), "2026-10-22T10:00:00.000Z");
  assert.equal(result.get("toTime"), null);
  assert.equal(result.get("event"), "CALL_FAILED");
});

test("API errors preserve Chinese field details for both Studio and internal response envelopes", () => {
  const detail = {code:"REQUEST_VALIDATION_FAILED",message:"请求字段校验失败",details:{fields:[{field:"query.fromTime",message:"请选择有效时间"}]}};
  assert.equal(apiErrorMessage({detail},422), "开始时间：请选择有效时间");
  assert.equal(apiErrorMessage(detail,422), "开始时间：请选择有效时间");
  assert.equal(apiErrorMessage({detail:"登录已过期"},401), "登录已过期");
});
