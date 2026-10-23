import assert from "node:assert/strict";
import fs from "node:fs";
import test from "node:test";
import ts from "typescript";

const source = fs.readFileSync(new URL("../src/callLogPresentation.ts", import.meta.url), "utf8");
const { outputText } = ts.transpileModule(source, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ES2022 } });
const { groupLogs, statusLabel, transportLabel, durationLabel, operationLabel, isFailure, businessLabels } = await import(`data:text/javascript;base64,${Buffer.from(outputText).toString("base64")}`);
const row = (id, event, fields = {}) => ({ logId: id, event, timestamp: `2026-10-23T00:00:0${id}+00:00`, level: "INFO", ...fields });

test("tec01 TERMINATED and legacy browser cancellation have readable labels", () => {
  assert.equal(businessLabels.TERMINATED, "已终止");
  assert.equal(businessLabels.CANCELLED, "已取消");
});

test("interleaved attempts pair only by span, never shared request or call ids", () => {
  const items = [row("4", "CALL_COMPLETED", {spanId:"b",callId:"same",requestId:"same",httpStatus:200,durationMs:10}), row("3", "CALL_STARTED", {spanId:"b",callId:"same",requestId:"same"}), row("2", "CALL_FAILED", {spanId:"a",callId:"same",requestId:"same",httpStatus:503,durationMs:30}), row("1", "CALL_STARTED", {spanId:"a",callId:"same",requestId:"same"})];
  const groups = groupLogs(items);
  assert.equal(groups.length, 2);
  assert.deepEqual(groups.map(group => group.records.length), [2, 2]);
  assert.equal(groups[0].durationMs, 10);
  assert.equal(groups[1].phase, "FAILED");
});

test("old inbound records correlate by trace but do not absorb submission events", () => {
  const groups = groupLogs([row("3", "REQUEST_COMPLETED", {traceId:"t",httpStatus:200}), row("2", "EXTRACTION_SUBMITTED", {traceId:"t"}), row("1", "REQUEST_STARTED", {traceId:"t"})]);
  assert.equal(groups.length, 2);
  assert.equal(groups[0].records.length, 2);
  assert.equal(groups[1].phase, "EVENT");
});

test("start-only and end-only pages are explicit, not fabricated running or timeout states", () => {
  const start = groupLogs([row("1", "CALL_STARTED", {spanId:"s"})])[0];
  assert.equal(statusLabel(start), "已开始 · 未见结束记录");
  assert.equal(durationLabel(start), "等待结束记录");
  assert.equal(transportLabel(start), "开始记录不含响应码");
  const end = groupLogs([row("2", "CALL_COMPLETED", {spanId:"s",httpStatus:200,durationMs:0})])[0];
  assert.equal(end.startedAt, undefined);
  assert.equal(durationLabel(end), "0 ms");
});

test("timeout has no invented HTTP code, and CLI zero is not HTTP 200", () => {
  const timeout = groupLogs([row("2", "CALL_FAILED", {errorCode:"TEC01_TIMEOUT",durationMs:30000})])[0];
  assert.equal(transportLabel(timeout), "未收到HTTP响应 · 超时");
  assert.equal(durationLabel(timeout), "30.00 秒");
  const cli = groupLogs([row("2", "CALL_COMPLETED", {peer:"AOPS_CLI",exitCode:0})])[0];
  assert.equal(transportLabel(cli), "CLI退出码 0（非HTTP）");
});

test("HTTP 200 with invalid confirmation is failed, while acknowledged business failure is distinct", () => {
  const bad = groupLogs([row("2", "CALL_FAILED", {httpStatus:200,errorCode:"TEC01_ACK_INVALID"})])[0];
  assert.equal(statusLabel(bad), "调用失败");
  const business = groupLogs([row("2", "CALL_COMPLETED", {httpStatus:200,acknowledged:true,businessStatus:"FAILED",businessErrorCode:"LLM_ANALYSIS_FAILED"})])[0];
  assert.equal(statusLabel(business), "回报已确认");
  assert.equal(business.phase, "COMPLETED");
  assert.equal(isFailure(business), true);
});

test("retry arrangement joins only its finished attempt, and durations are not derived from backoff", () => {
  const group = groupLogs([row("3", "RETRY_SCHEDULED", {spanId:"s",retryDelaySeconds:1}), row("2", "CALL_FAILED", {spanId:"s",durationMs:12}), row("1", "CALL_STARTED", {spanId:"s"})])[0];
  assert.equal(group.records.length, 3);
  assert.equal(group.entry.event, "CALL_FAILED");
  assert.equal(group.durationMs, 12);
});

test("missing measured duration can only use labeled timestamp estimate when both ends exist", () => {
  const group = groupLogs([row("2", "CALL_COMPLETED", {spanId:"s"}), row("1", "CALL_STARTED", {spanId:"s"})])[0];
  assert.equal(group.estimatedDuration, true);
  assert.equal(durationLabel(group), "约 1.00 秒");
  const groups = groupLogs([row("2", "CALL_COMPLETED", {requestId:"same"}), row("1", "CALL_STARTED", {requestId:"same"})]);
  assert.equal(groups.length, 2);
});

test("raw mode stays unmerged and duplicate log ids do not duplicate timeline events", () => {
  const start = row("1", "CALL_STARTED", {spanId:"s"});
  const end = row("2", "CALL_COMPLETED", {spanId:"s"});
  assert.equal(groupLogs([end,start],false).length, 2);
  assert.equal(groupLogs([end,start,start])[0].records.length, 2);
});

test("operation descriptions are human-readable and cover old files", () => {
  assert.equal(operationLabel(row("1","CALL_STARTED",{operation:"sql_read"})), "执行AOPS SQL只读查询");
  assert.equal(operationLabel(row("1","REQUEST_STARTED",{operation:"http_request",url:"/internal/v1/runtime/workflows/validate"})), "校验Workflow定义");
  assert.equal(operationLabel(row("1","CALL_STARTED",{operation:"PROGRESS",stage:"PARAMETERIZING_SQL"})), "回报提取进度：去重与参数化SQL");
});
