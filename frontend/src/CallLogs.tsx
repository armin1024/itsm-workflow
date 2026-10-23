import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Button, Checkbox, InlineLoading, InlineNotification, Select, SelectItem, TextInput } from "@carbon/react";
import { useSearchParams } from "react-router-dom";
import { Link } from "react-router-dom";
import { composeLogFilters, effectiveLogQuery, localDateTime, logLevels, logEvents, logOperations, validateLogFilters } from "./logQuery";
import { api } from "./api";
import { pretty } from "./ui";
import { groupLogs, operationLabel, statusLabel, transportLabel, durationLabel, isFailure, businessLabels } from "./callLogPresentation";
import type { CallLog, LogGroup } from "./callLogPresentation";

export type { CallLog } from "./callLogPresentation";
export type LogPage = { items: CallLog[]; nextCursor: string | null; scannedBytes: number; unavailable?: boolean };
export type LogStatus = { directory: string; level: string; retentionDays: number; maxTotalBytes: number; maxFileBytes: number; cleanupIntervalSeconds: number; usedBytes: number; fileCount: number; lastCleanupAt: string | null; lastDeletedFiles: number; writeErrors: number; cleanupErrors: number; droppedRecords: number; queuedRecords: number; lastError: string | null; available: boolean };
const mb = (value = 0) => `${(value / 1024 / 1024).toFixed(1)} MiB`;
const clock = (value?: string | null) => value ? new Date(value).toLocaleString("zh-CN", { hour12: false }) : "—";
const events: Record<string, string> = { REQUEST_STARTED: "收到请求", REQUEST_COMPLETED: "请求完成", REQUEST_FAILED: "请求失败", CALL_STARTED: "开始调用", CALL_COMPLETED: "调用完成", CALL_FAILED: "调用失败", RETRY_SCHEDULED: "等待重试", EXECUTION_DISPATCHED: "下发执行", EXTRACTION_SUBMITTED: "提交提取" };
const peers: Record<string, string> = { TEC01: "tec01", STUDIO: "本地页面", LLM: "LLM网关", AOPS_CLI: "AOPS CLI" };
const advanced = [{ key: "runId", label: "运行ID" }, { key: "knowledgeId", label: "知识ID" }, { key: "nodeId", label: "节点ID" }, { key: "requestId", label: "请求ID" }, { key: "spanId", label: "调用尝试ID" }, { key: "callId", label: "回报调用ID" }, { key: "traceId", label: "调用链路ID" }];

export default function CallLogs() {
  const [params, setParams] = useSearchParams();
  const [draft, setDraft] = useState(params.get("keyword") || "");
  const [page, setPage] = useState<LogPage>();
  const [status, setStatus] = useState<LogStatus>();
  const [busy, setBusy] = useState(false), [error, setError] = useState("");
  const [cursor, setCursor] = useState<string>();
  const [auto, setAuto] = useState(false), [selected, setSelected] = useState<LogGroup>();
  const [merge, setMerge] = useState(true);
  const [related, setRelated] = useState<CallLog[]>([]), [relatedNext, setRelatedNext] = useState<string | null>(null);
  const [relatedBusy, setRelatedBusy] = useState(false), [relatedError, setRelatedError] = useState("");
  const selectedVersion = useRef(0);
  const [exact, setExact] = useState<Record<string, string>>({});
  const [filterErrors, setFilterErrors] = useState<Record<string, string>>({});
  const frozenQuery = useRef("");
  const sequence = useRef(0);
  const query = params.toString();
  const load = useCallback(async (position?: string) => {
    const id = ++sequence.current;
    const invalid = validateLogFilters(new URLSearchParams(query));
    setFilterErrors(invalid);
    if (Object.keys(invalid).length) { setBusy(false); setError(""); return; }
    if (!position) frozenQuery.current = effectiveLogQuery(query);
    setBusy(true); setError("");
    try {
      const [result, summary] = await Promise.all([api.logs(frozenQuery.current, position), api.logStatus()]);
      if (id === sequence.current) { setPage(result); setStatus(summary); setCursor(position); }
    } catch (value) {
      if (id === sequence.current) setError((value as Error).message);
    } finally { if (id === sequence.current) setBusy(false); }
  }, [query]);
  useEffect(() => { setSelected(undefined); setCursor(undefined); const current = new URLSearchParams(query); setDraft(current.get("keyword") || ""); setExact({ ...Object.fromEntries([...advanced.map(field => field.key), "direction", "level", "peer", "event", "operation"].map(key => [key, current.get(key) || ""])), range: current.get("range") || (current.get("fromTime") || current.get("toTime") ? "custom" : "all"), fromTime: localDateTime(current.get("fromTime") || ""), toTime: localDateTime(current.get("toTime") || "") }); void load(); return () => { sequence.current++; }; }, [load, query]);
  useEffect(() => {
    if (!auto || cursor) return;
    const interval = window.setInterval(() => { if (!document.hidden && !busy) void load(); }, 5000);
    return () => clearInterval(interval);
  }, [auto, cursor, busy, load]);
  const applyFilters = () => {
    const result = composeLogFilters(exact, draft);
    setFilterErrors(result.errors);
    if (Object.keys(result.errors).length) return;
    if (result.params.toString() === query) void load();
    else setParams(result.params);
  };
  const clearFilters = () => { setDraft(""); setExact({ range: "all" }); setFilterErrors({}); setParams({}); if (!query) void load(); };
  const groups = useMemo(() => groupLogs(page?.items || [], merge), [page, merge]);
  const currentSelection = selected ? groups.find(group => group.key === selected.key) || selected : undefined;
  const relatedSelection = currentSelection && related.length ? groupLogs([...related, ...currentSelection.records]).find(group => group.records.some(row => row.logId === currentSelection.entry.logId)) : undefined;
  const detail = relatedSelection || currentSelection;
  const select = (group?: LogGroup) => { selectedVersion.current++; setSelected(group); setRelated([]); setRelatedNext(null); setRelatedError(""); setRelatedBusy(false); };
  const loadRelated = async (next?: string) => {
    if (!detail) return;
    const version = selectedVersion.current;
    const entry = detail.entry;
    const key = entry.callId ? "callId" : entry.spanId ? "spanId" : "traceId";
    const identity = entry[key];
    if (typeof identity !== "string") return;
    setRelatedBusy(true); setRelatedError("");
    try {
      const result = await api.logs(new URLSearchParams({ [key]: identity }).toString(), next);
      if (version === selectedVersion.current) {
        setRelated(previous => Array.from(new Map([...(next ? previous : []), ...result.items].map(row => [row.logId, row])).values()));
        setRelatedNext(result.nextCursor);
      }
    } catch (value) { if (version === selectedVersion.current) setRelatedError((value as Error).message); }
    finally { if (version === selectedVersion.current) setRelatedBusy(false); }
  };
  return <main className="page call-logs">
    <header className="page-title"><div><p className="overline">CALL OBSERVABILITY</p><h1>调用日志</h1><p>只读追踪服务交互与确认结果。日志不包含SQL、参数、请求正文或生产凭据，不作为运行状态依据。</p></div><div className="header-actions"><Button as={Link} to="/logs/raw" kind="secondary">后台原始日志</Button><Button kind="tertiary" disabled={busy} onClick={() => load(cursor)}>刷新已应用条件</Button></div></header>
    {status && <section className="log-storage-strip" aria-label="日志存储状态"><div><small>应用日志占用</small><strong>{mb(status.usedBytes)} / {mb(status.maxTotalBytes)}</strong></div><div><small>保留上限</small><strong>{status.retentionDays}天 · 每{status.cleanupIntervalSeconds}秒清理</strong></div><div><small>最近清理</small><strong>{clock(status.lastCleanupAt)}</strong></div><div><small>丢弃 / 写入失败 / 清理失败</small><strong>{status.droppedRecords} / {status.writeErrors} / {status.cleanupErrors}</strong></div></section>}
    {status && (!status.available || status.writeErrors > 0 || status.cleanupErrors > 0 || status.droppedRecords > 0) && <InlineNotification kind="warning" title="日志存储存在异常" subtitle={`${status.lastError || "目录不可用"}。请检查目录权限、磁盘和服务端终端；日志故障不代表业务执行失败。`} hideCloseButton />}
    {error && <InlineNotification kind="error" title="无法读取日志" subtitle={error} hideCloseButton />}
    {error && <Button kind="ghost" onClick={() => load()}>从最新日志重新查询</Button>}
    {Object.keys(filterErrors).length > 0 && <InlineNotification kind="warning" title="请调整筛选条件" subtitle={Object.values(filterErrors).join("；")} hideCloseButton />}
    {(Object.keys(filterErrors).length > 0 || error) && page && <p className="log-pairing-note">当前保留上一次成功查询的结果；本次有误的条件尚未应用。</p>}
    <form className="log-query" onSubmit={(event) => { event.preventDefault(); applyFilters(); }}>
      <TextInput id="log-keyword" labelText="关键词" placeholder="中文操作、接口、ID或错误码" maxLength={200} invalid={!!filterErrors.keyword} invalidText={filterErrors.keyword} value={draft} onChange={(event) => setDraft(event.target.value)} />
      <Select id="log-level" labelText="日志级别" invalid={!!filterErrors.level} invalidText={filterErrors.level} value={exact.level || ""} onChange={(event) => setExact({ ...exact, level: event.target.value })}><SelectItem value="" text="全部级别" />{Object.entries(logLevels).map(([value, label]) => <SelectItem key={value} value={value} text={`${label}（${value}）`} />)}</Select>
      <Select id="log-peer" labelText="调用对端" invalid={!!filterErrors.peer} invalidText={filterErrors.peer} value={exact.peer || ""} onChange={(event) => setExact({ ...exact, peer: event.target.value })}><SelectItem value="" text="全部对端" />{Object.entries(peers).map(([value, text]) => <SelectItem key={value} value={value} text={text} />)}</Select>
      <Button type="submit" disabled={busy}>查询</Button><Button kind="ghost" onClick={clearFilters}>清空条件</Button>
    </form>
    <div className="log-event-controls"><Select id="log-range" labelText="时间范围" invalid={!!filterErrors.range} invalidText={filterErrors.range} value={exact.range || "all"} onChange={event => setExact({ ...exact, range: event.target.value })}><SelectItem value="all" text="不限时间" /><SelectItem value="1h" text="最近1小时" /><SelectItem value="24h" text="最近24小时" /><SelectItem value="7d" text="最近7天" /><SelectItem value="custom" text="自定义时间" /></Select><Select id="log-event" labelText="事件类型" value={exact.event || ""} onChange={event => setExact({ ...exact, event: event.target.value })}><SelectItem value="" text="全部事件" />{Object.entries(logEvents).map(([value,text]) => <SelectItem key={value} value={value} text={text} />)}</Select><Select id="log-operation" labelText="具体操作" value={exact.operation || ""} onChange={event => setExact({ ...exact, operation: event.target.value })}><SelectItem value="" text="全部操作" />{Object.entries(logOperations).map(([value,text]) => <SelectItem key={value} value={value} text={text} />)}</Select></div>
    {exact.range === "custom" && <div className="log-date-controls"><TextInput id="log-from-time" labelText="开始时间（含）" type="datetime-local" invalid={!!filterErrors.fromTime} invalidText={filterErrors.fromTime} value={exact.fromTime || ""} onChange={event => setExact({ ...exact, fromTime: event.target.value })} /><TextInput id="log-to-time" labelText="结束时间（不含）" type="datetime-local" invalid={!!filterErrors.toTime} invalidText={filterErrors.toTime} value={exact.toTime || ""} onChange={event => setExact({ ...exact, toTime: event.target.value })} /></div>}
    <p className="log-pairing-note">修改条件后点击“查询”生效；时间按浏览器本地时区自动转换，无需填写时区字符串。查看更早日志时冻结本次时间范围。</p>
    <details className="log-advanced"><summary>精确筛选 · 运行、知识与链路ID</summary><div>{advanced.map((field) => <TextInput key={field.key} id={`log-${field.key}`} labelText={field.label} helperText="可选，按完整ID精确匹配" maxLength={160} invalid={!!filterErrors[field.key]} invalidText={filterErrors[field.key]} value={exact[field.key] || ""} onChange={(event) => setExact({ ...exact, [field.key]: event.target.value })} />)}<Select id="log-direction" labelText="调用方向" value={exact.direction || ""} onChange={(event) => setExact({ ...exact, direction: event.target.value })}><SelectItem value="" text="全部方向" /><SelectItem value="INBOUND" text="入站请求" /><SelectItem value="OUTBOUND" text="出站调用" /></Select><Button kind="secondary" onClick={applyFilters}>查询已填写条件</Button></div></details>
    <div className="log-list-toolbar"><span>{cursor ? "历史日志 · 自动更新已暂停" : "最新日志"} · 本批{groups.length}项 / {page?.items.length || 0}条事件</span><Select id="log-view" labelText="展示方式" value={merge ? "merged" : "raw"} onChange={event => { setMerge(event.target.value === "merged"); select(); }}><SelectItem value="merged" text="按调用合并开始与结束" /><SelectItem value="raw" text="原始事件" /></Select><Checkbox id="log-auto" labelText="每5秒自动更新最新日志" checked={auto} onChange={(_, { checked }) => setAuto(checked)} /></div>
    <p className="log-pairing-note">同一次尝试按调用标识关联，重试另列。只见开始记录不等于仍在运行，结束记录可能在其他批次、被筛选或已清理；可在详情中查询关联事件。</p>
    {busy && <InlineLoading description="分块读取日志" />}
    <section className="log-table" aria-label="调用日志列表"><div className="log-row log-table-head"><span>开始 / 结束时间</span><span>方向 / 对端</span><span>这一步在做什么</span><span>调用结果 / 响应</span><span>本次耗时</span></div>{groups.map(group => { const entry = group.entry; const retry = group.records.find(row => row.event === "RETRY_SCHEDULED"); return <button className={`log-row ${isFailure(group) ? "log-row-failed" : ""}`} key={group.key} onClick={() => select(group)}><span><time>{clock(group.startedAt || entry.timestamp)}</time><small>{group.endedAt ? `结束 ${clock(group.endedAt)}` : group.phase === "EVENT" ? "事件时间" : "当前未见结束"}</small><small className={`log-level log-level-${entry.level.toLowerCase()}`}>{entry.level}{group.endedAt && !group.startedAt ? " · 开始记录未加载" : ""}</small></span><span>{entry.direction === "OUTBOUND" ? "出站 →" : "入站 ←"}<small>{peers[entry.peer || ""] || entry.peer || "服务"}</small></span><span><strong>{operationLabel(entry)}</strong><small title={String(entry.url || "")}>{entry.method || entry.operation || entry.event}{entry.url ? ` ${entry.url}` : ""}</small><small>{[entry.nodeId && `节点 ${entry.nodeId}`, entry.knowledgeId && `知识 ${entry.knowledgeId}`, entry.runId && `运行 ${entry.runId}`].filter(Boolean).join(" · ") || String(entry.requestId || "无业务关联ID")}</small>{entry.attempt !== undefined && <small>第{entry.attempt}/{entry.maxAttempts || "?"}次尝试</small>}</span><span><strong className={`log-call-state log-call-state-${group.phase.toLowerCase()}`}>{statusLabel(group)}</strong><small>{transportLabel(group)}</small>{entry.businessStatus && <small>业务结果：{businessLabels[entry.businessStatus] || entry.businessStatus}</small>}{(entry.businessErrorCode || entry.errorCode) && <small>{entry.businessErrorCode || entry.errorCode}</small>}{retry && <small>本次失败后计划{retry.retryDelaySeconds}秒再试</small>}</span><span>{durationLabel(group)}{group.estimatedDuration && <small>按日志时间差推算</small>}{!group.endedAt && group.phase === "START_ONLY" && <small>不是最终耗时</small>}</span></button>; })}</section>
    {page && !page.items.length && !busy && <div className="log-empty"><h2>{page.unavailable ? "日志存储暂不可用" : page.nextCursor ? "本次扫描范围内没有匹配日志" : "没有匹配日志"}</h2><p>{page.nextCursor ? "可继续读取更早日志，或调整筛选条件。" : "修改查询条件，或执行一次提取/流程测试后刷新。"}</p></div>}
    <footer className="log-pagination"><Button kind="tertiary" disabled={busy || !cursor} onClick={() => load()}>返回最新</Button><span>单次扫描上限8MiB · 不统计全量总数</span><Button kind="tertiary" disabled={busy || !page?.nextCursor} onClick={() => load(page?.nextCursor || undefined)}>读取更早日志</Button></footer>
    {status && <details className="log-storage-details"><summary>存储与清理策略</summary><p>目录：<code>{status.directory}</code></p><p>单文件上限 {mb(status.maxFileBytes)}，跨UTC日期自动轮转。超期或总量超限时自动删除最旧归档，不支持手工删除或重放。队列待写 {status.queuedRecords} 条，最近清理删除 {status.lastDeletedFiles} 个文件。</p><p>这里只限制应用日志文件；journalctl副本由主机journald策略管理。刷新、关闭页面不会删除服务端日志。</p></details>}
    {detail && <aside className="log-detail" role="dialog" aria-modal="false" aria-label="日志详情"><header><div><p className="overline">CALL LIFECYCLE</p><h2>{operationLabel(detail.entry)}</h2></div><Button kind="ghost" onClick={() => select()}>关闭</Button></header><p>{detail.entry.operationDescription || "查看本次调用的开始、结束及重试事件。"}</p><dl className="log-call-facts"><div><dt>调用阶段</dt><dd>{statusLabel(detail)}</dd></div><div><dt>响应 / 退出码</dt><dd>{transportLabel(detail)}</dd></div><div><dt>开始时间</dt><dd>{clock(detail.startedAt)}</dd></div><div><dt>结束时间</dt><dd>{clock(detail.endedAt)}</dd></div><div><dt>本次耗时</dt><dd>{durationLabel(detail)}</dd></div>{detail.entry.businessStatus && <div><dt>节点或提取业务结果</dt><dd>{businessLabels[detail.entry.businessStatus] || detail.entry.businessStatus}</dd></div>}</dl><p>调用完成只说明本次通信或CLI结束；回报已确认不代表整个Workflow或提取成功。没有HTTP响应或CLI退出码时不会补造数值。</p>{(detail.entry.callId || detail.entry.spanId || detail.entry.traceId) && <Button kind="tertiary" disabled={relatedBusy} onClick={() => loadRelated()}>{relatedBusy ? "读取关联事件" : "查看关联事件与全部尝试"}</Button>}{relatedError && <InlineNotification kind="error" title="无法查询关联事件" subtitle={relatedError} hideCloseButton />}<p className="log-pairing-note">关联查询不受列表级别和时间筛选限制；已清理或从未记录的事件无法补齐。</p><ol className="log-call-timeline">{[...(related.length ? related : detail.records)].sort((a,b) => a.timestamp.localeCompare(b.timestamp)).map(row => <li key={row.logId}><time>{clock(row.timestamp)}</time><strong>{events[row.event] || row.event}{row.attempt ? ` · 第${row.attempt}次尝试` : ""}</strong><span>{operationLabel(row)}{row.httpStatus !== undefined ? ` · HTTP ${row.httpStatus}` : ""}{row.exitCode !== undefined ? ` · CLI退出码 ${row.exitCode}` : ""}{row.errorCode ? ` · ${row.errorCode}` : ""}{row.businessErrorCode ? ` · ${row.businessErrorCode}` : ""}{row.retryDelaySeconds !== undefined ? ` · ${row.retryDelaySeconds}秒后重试` : ""}</span><details><summary>安全技术字段</summary><pre>{pretty(row)}</pre></details></li>)}</ol>{relatedNext && <Button kind="tertiary" disabled={relatedBusy} onClick={() => loadRelated(relatedNext)}>继续读取关联事件</Button>}</aside>}
  </main>;
}
