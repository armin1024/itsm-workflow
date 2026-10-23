export const logLevels = { INFO: "信息", WARNING: "警告", ERROR: "错误", DEBUG: "调试" };
export const logPeers = { TEC01: "tec01", STUDIO: "本地页面", LLM: "LLM网关", AOPS_CLI: "AOPS CLI" };
export const logEvents = { REQUEST_STARTED: "收到请求", REQUEST_COMPLETED: "请求处理完成", REQUEST_FAILED: "请求处理失败", CALL_STARTED: "开始外部调用", CALL_COMPLETED: "外部调用完成", CALL_FAILED: "外部调用失败", RETRY_SCHEDULED: "安排重试", EXECUTION_DISPATCHED: "提交执行任务", EXTRACTION_SUBMITTED: "提交提取任务" };
export const logOperations = { http_request: "服务接口请求", dispatch: "提交Workflow执行", extraction: "提交知识提取", sql_read: "SQL只读查询", ticket_info: "获取工单详情", audit_timeline: "获取操作记录", aops_command: "AOPS CLI调用", analyze_evidence: "LLM提炼草稿", PROGRESS: "回报提取进度", FINISHED: "回报提取终态", NODE_STARTED: "回报节点开始", NODE_COMPLETED: "回报节点结果", RUN_RELEASED: "回报运行释放", callback_ack_wait: "保存确认总等待", pause_run: "请求暂停", cancel_run: "请求取消" };
export type LogFilterErrors = Record<string, string>;
const idFields = ["runId", "knowledgeId", "nodeId", "requestId", "spanId", "callId", "traceId"];
const labels: Record<string, string> = { keyword: "关键词", level: "日志级别", peer: "对端", event: "事件类型", operation: "操作", direction: "方向", fromTime: "开始时间", toTime: "结束时间", runId: "运行ID", knowledgeId: "知识ID", nodeId: "节点ID", requestId: "请求ID", spanId: "调用尝试ID", callId: "回报调用ID", traceId: "链路ID" };

export function validateLogFilters(params: URLSearchParams): LogFilterErrors {
  const errors: LogFilterErrors = {};
  if (params.get("range") && !["all", "custom", "1h", "24h", "7d"].includes(params.get("range")!)) errors.range = "时间范围选项无效，请重新选择";
  if ((params.get("keyword") || "").length > 200) errors.keyword = "关键词最多200个字符，请缩短查询内容";
  for (const key of idFields) if ((params.get(key) || "").length > 160) errors[key] = `${labels[key]}最多160个字符`;
  for (const [key, allowed] of Object.entries({ level: Object.keys(logLevels), peer: Object.keys(logPeers), direction: ["INBOUND", "OUTBOUND"] })) {
    const value = params.get(key);
    if (value && !allowed.includes(value)) errors[key] = `${labels[key]}选项无效，请重新选择或清空条件`;
  }
  for (const key of ["event", "operation"]) if ((params.get(key) || "").length > 80) errors[key] = `${labels[key]}最多80个字符`;
  for (const key of ["fromTime", "toTime"]) {
    const value = params.get(key);
    if (value && (!/T.*(?:Z|[+-]\d{2}:\d{2})$/i.test(value) || !Number.isFinite(Date.parse(value)))) errors[key] = `${labels[key]}格式不正确，请使用页面的时间选择器`;
  }
  if (!errors.fromTime && !errors.toTime && params.get("fromTime") && params.get("toTime") && Date.parse(params.get("fromTime")!) >= Date.parse(params.get("toTime")!)) errors.toTime = "结束时间必须晚于开始时间";
  return errors;
}

export function composeLogFilters(draft: Record<string, string>, keyword: string): { params: URLSearchParams; errors: LogFilterErrors } {
  let params = new URLSearchParams();
  if (keyword.trim()) params.set("keyword", keyword.trim());
  for (const key of ["level", "peer", "event", "operation", "direction", ...idFields]) if (draft[key]?.trim()) params.set(key, draft[key].trim());
  const range = draft.range || "all";
  if (range !== "all") params.set("range", range);
  if (range === "custom") {
    if (!draft.fromTime && !draft.toTime) return { params, errors: { fromTime: "请选择开始或结束时间，或改为不限时间" } };
    return applyLocalDates(params, draft.fromTime || "", draft.toTime || "");
  }
  return { params, errors: validateLogFilters(params) };
}

export function localDateTime(iso: string): string {
  const date = new Date(iso);
  if (!iso || !Number.isFinite(date.getTime())) return "";
  const pad = (value: number) => String(value).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

export function applyLocalDates(params: URLSearchParams, from: string, to: string): { params: URLSearchParams; errors: LogFilterErrors } {
  const next = new URLSearchParams(params);
  const errors: LogFilterErrors = {};
  for (const [key, value] of [["fromTime", from], ["toTime", to]]) {
    if (!value) { next.delete(key); continue; }
    const date = new Date(value);
    if (!Number.isFinite(date.getTime())) errors[key] = `${labels[key]}不完整，请重新选择`;
    else next.set(key, date.toISOString());
  }
  return { params: next, errors: { ...validateLogFilters(next), ...errors } };
}

export function buildLogQuery(query: string, cursor?: string): URLSearchParams {
  const input = new URLSearchParams(query), output = new URLSearchParams();
  for (const key of ["keyword", "level", "peer", "direction", "event", "operation", "fromTime", "toTime", ...idFields]) {
    const value = input.get(key)?.trim();
    if (value) output.set(key, value);
  }
  output.set("limit", "100");
  if (cursor) output.set("cursor", cursor);
  return output;
}

export function effectiveLogQuery(query: string, now = Date.now()): string {
  const params = new URLSearchParams(query);
  const hours: Record<string, number> = { "1h": 1, "24h": 24, "7d": 168 };
  const range = params.get("range") || "";
  if (hours[range]) {
    params.set("fromTime", new Date(now - hours[range] * 3600000).toISOString());
    params.delete("toTime");
  }
  return buildLogQuery(params.toString()).toString();
}

export function apiErrorMessage(body: unknown, status: number): string {
  const input = body && typeof body === "object" ? body as Record<string, unknown> : {};
  const detail = input.detail ?? input;
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) return detail.map(item => typeof item?.msg === "string" ? `${labels[String(item.loc?.at(-1))] || String(item.loc?.at(-1) || "参数")}：${item.msg}` : "参数格式不正确").join("；");
  const data = detail && typeof detail === "object" ? detail as Record<string, unknown> : {};
  const fields = (data.details as { fields?: Array<{ field?: string; message?: string }> } | undefined)?.fields;
  if (fields?.length) return fields.map(field => `${labels[field.field?.split(".").at(-1) || ""] || field.field || "参数"}：${field.message || "格式不正确"}`).join("；");
  if (typeof data.message === "string") return data.message;
  return status === 401 ? "登录已过期，请刷新页面重新登录" : `请求失败（HTTP ${status}），请稍后重试`;
}
