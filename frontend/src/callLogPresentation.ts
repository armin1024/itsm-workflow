export type CallLog = {
  logId: string; timestamp: string; level: string; event: string; direction?: string; peer?: string;
  operation?: string; operationLabel?: string; operationDescription?: string; durationMs?: number;
  method?: string; url?: string; nodeId?: string; runId?: string; knowledgeId?: string; requestId?: string;
  errorMessage?: string; reasonCode?: string; errorField?: string; errorId?: string; stage?: string; exceptionType?: string; errorLocation?: string;
  httpStatus?: number; exitCode?: number; errorCode?: string; businessErrorCode?: string;
  businessStatus?: string; acknowledged?: boolean; spanId?: string; traceId?: string; callId?: string;
  attempt?: number; maxAttempts?: number; retryDelaySeconds?: number; [key: string]: unknown;
};

export type LogGroup = {
  key: string; records: CallLog[]; entry: CallLog; startedAt?: string; endedAt?: string;
  phase: "START_ONLY" | "COMPLETED" | "FAILED" | "EVENT"; durationMs?: number; estimatedDuration: boolean;
};

export const businessLabels: Record<string, string> = {
  SUCCEEDED: "成功", PARTIAL_SUCCESS: "部分成功，待审核", FAILED: "失败", WAITING_INPUT: "等待用户输入", PAUSED: "已暂停",
  TERMINATED: "已终止", CANCELLED: "已取消", UNKNOWN: "结果未知", RUNNING: "进行中",
};
const stageLabels: Record<string, string> = {
  FILTERING_OPERATIONS: "过滤有效SQL", PARAMETERIZING_SQL: "去重与参数化SQL",
  ANALYZING_EVIDENCE: "分析工单与操作证据", BUILDING_WORKFLOW: "组装流程草稿", VALIDATING_WORKFLOW: "校验流程定义",
};
const operationLabels: Record<string, string> = {
  compile_evidence: "知识提取失败诊断", NODE_STARTED: "回报节点开始执行", NODE_COMPLETED: "回报节点执行结果", RUN_RELEASED: "回报运行结束或释放",
  FINISHED: "回报知识提取终态", analyze_evidence: "调用LLM提炼经验草稿", sql_read: "执行AOPS SQL只读查询",
  ticket_info: "获取AOPS工单详情", audit_timeline: "获取AOPS工单操作记录", aops_command: "调用AOPS CLI",
  dispatch: "提交Workflow执行任务", extraction: "提交工单证据提取任务",
  callback_ack_wait: "等待tec01保存确认超时", pause_run: "请求暂停Workflow运行", cancel_run: "请求取消Workflow运行",
};

export function operationLabel(entry: CallLog): string {
  if (entry.operationLabel) return entry.operationLabel;
  if (entry.event === "COMPILER_PARTIAL_SUCCESS") return "知识提取部分成功：完整性待审核";
  if (entry.operation === "PROGRESS") return `回报提取进度：${stageLabels[String(entry.stage)] || "阶段进度"}`;
  if (entry.operation === "http_request") {
    const path = String(entry.url || "");
    if (path.endsWith("/catalog")) return "获取节点目录";
    if (path.includes("/schemas")) return "获取Workflow JSON约束";
    if (path.endsWith("/workflows/validate")) return "校验Workflow定义";
    if (path.endsWith("/workflows/plan")) return "生成执行计划预览";
    if (path.endsWith("/executions/dispatch")) return "接收Workflow执行下发";
    if (path.endsWith("/commands")) return "接收运行控制请求";
    if (path.includes("/extractions")) return "接收工单知识提取请求";
    if (path.endsWith("/session")) return "处理Studio管理会话";
    if (path.endsWith("/config")) return "查看运行配置";
    return "处理服务接口请求";
  }
  return operationLabels[entry.operation || ""] || "服务处理事件";
}

const starts = new Set(["REQUEST_STARTED", "CALL_STARTED"]);
const ends = new Set(["REQUEST_COMPLETED", "REQUEST_FAILED", "CALL_COMPLETED", "CALL_FAILED"]);

export function groupLogs(items: CallLog[], merge = true): LogGroup[] {
  const grouped = new Map<string, CallLog[]>();
  for (const entry of items) {
    const family = entry.event.startsWith("REQUEST_") ? "request" : entry.event.startsWith("CALL_") || entry.event === "RETRY_SCHEDULED" ? "call" : "event";
    // Never pair by requestId or nodeId: both legitimately span multiple attempts.
    const identity = entry.spanId || (family === "request" ? entry.traceId : undefined);
    const key = merge && identity && family !== "event" ? `${family}:${identity}` : `event:${entry.logId}`;
    const values = grouped.get(key) || [];
    if (!values.some(value => value.logId === entry.logId)) values.push(entry);
    grouped.set(key, values);
  }
  return Array.from(grouped, ([key, values]) => {
    const records = [...values].sort((a, b) => a.timestamp.localeCompare(b.timestamp));
    const first = records.find(entry => starts.has(entry.event));
    const final = [...records].reverse().find(entry => ends.has(entry.event));
    const entry = final || records[records.length - 1];
    const failed = !!final && (final.event.endsWith("FAILED") || !!final.errorCode || (final.httpStatus !== undefined && final.httpStatus >= 400) || (final.exitCode !== undefined && final.exitCode !== 0));
    let durationMs = typeof final?.durationMs === "number" && Number.isFinite(final.durationMs) && final.durationMs >= 0 ? final.durationMs : undefined;
    let estimatedDuration = false;
    if (durationMs === undefined && first && final) {
      const difference = Date.parse(final.timestamp) - Date.parse(first.timestamp);
      if (Number.isFinite(difference) && difference >= 0) { durationMs = difference; estimatedDuration = true; }
    }
    const phase: LogGroup["phase"] = final ? failed ? "FAILED" : "COMPLETED" : first ? "START_ONLY" : "EVENT";
    return { key, records, entry, startedAt: first?.timestamp, endedAt: final?.timestamp, phase, durationMs, estimatedDuration };
  });
}

export function statusLabel(group: LogGroup): string {
  if (group.entry.event === "COMPILER_FAILED") return "提取失败";
  if (group.entry.event === "COMPILER_REPORT_FAILED") return "终态同步失败";
  if (group.phase === "START_ONLY") return "已开始 · 未见结束记录";
  if (group.phase === "FAILED") return "调用失败";
  if (group.phase === "COMPLETED") return group.entry.acknowledged ? "回报已确认" : "调用完成";
  return group.entry.event === "RETRY_SCHEDULED" ? "安排重试" : "处理事件";
}

export function transportLabel(group: LogGroup): string {
  const entry = group.entry;
  if (entry.errorCode === "TEC01_ACK_WAIT_EXCEEDED") return "保存确认总等待到期（非新HTTP响应）";
  if (group.phase === "EVENT") return "本事件不记录响应码";
  if (entry.method === "CLI" || entry.peer === "AOPS_CLI") {
    if (typeof entry.exitCode === "number") return `CLI退出码 ${entry.exitCode}（非HTTP）`;
    return group.phase === "START_ONLY" ? "开始记录不含CLI退出码" : "未取得CLI退出码";
  }
  if (typeof entry.httpStatus === "number") return `HTTP ${entry.httpStatus}`;
  if (group.phase === "START_ONLY") return "开始记录不含响应码";
  if (entry.errorCode?.includes("TIMEOUT") || entry.errorCode?.includes("WAIT_EXCEEDED")) return "未收到HTTP响应 · 超时";
  if (entry.errorCode?.includes("UNAVAILABLE")) return "未收到HTTP响应 · 连接失败";
  return "结束记录未包含HTTP状态码";
}

export function durationLabel(group: LogGroup): string {
  if (group.durationMs !== undefined) {
    const text = group.durationMs >= 1000 ? `${(group.durationMs / 1000).toFixed(2)} 秒` : `${Number(group.durationMs.toFixed(2))} ms`;
    return `${group.estimatedDuration ? "约 " : ""}${text}`;
  }
  return group.phase === "START_ONLY" ? "等待结束记录" : group.phase === "EVENT" ? "非计时事件" : "结束记录未含耗时";
}

export function isFailure(group: LogGroup): boolean {
  return group.phase === "FAILED" || group.entry.businessStatus === "FAILED";
}
