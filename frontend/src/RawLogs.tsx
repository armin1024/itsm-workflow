import { useCallback, useEffect, useRef, useState } from "react";
import { Button, InlineLoading, InlineNotification, Select, SelectItem, TextInput } from "@carbon/react";
import { Link, useSearchParams } from "react-router-dom";
import { api } from "./api";
import { composeLogFilters, effectiveLogQuery, localDateTime, validateLogFilters } from "./logQuery";

type RawPage = Awaited<ReturnType<typeof api.rawLogs>>;
type JournalPage = Awaited<ReturnType<typeof api.journal>>;

export default function RawLogs() {
  const [params, setParams] = useSearchParams();
  const source = params.get("source") === "journal" ? "journal" : "app";
  const [selectedSource, setSelectedSource] = useState(source);
  const [keyword, setKeyword] = useState("");
  const [dates, setDates] = useState<Record<string, string>>({ range: "all" });
  const [page, setPage] = useState<RawPage>(), [journal, setJournal] = useState<JournalPage>();
  const [busy, setBusy] = useState(false), [error, setError] = useState("");
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [cursor, setCursor] = useState<string>();
  const sequence = useRef(0), frozen = useRef("");
  const query = params.toString();
  const load = useCallback(async (position?: string) => {
    const id = ++sequence.current;
    const invalid = validateLogFilters(new URLSearchParams(query));
    setErrors(invalid);
    if (Object.keys(invalid).length) return;
    if (!position) frozen.current = effectiveLogQuery(query);
    setBusy(true); setError("");
    try {
      if (source === "journal") {
        const result = await api.journal(frozen.current);
        if (sequence.current === id) { setJournal(result); setPage(undefined); setCursor(undefined); }
      } else {
        const result = await api.rawLogs(frozen.current, position);
        if (sequence.current === id) { setPage(result); setJournal(undefined); setCursor(position); }
      }
    } catch (value) { if (sequence.current === id) setError((value as Error).message); }
    finally { if (sequence.current === id) setBusy(false); }
  }, [query, source]);
  useEffect(() => {
    const values = new URLSearchParams(query);
    setSelectedSource(source); setKeyword(values.get("keyword") || "");
    setDates({ range: values.get("range") || (values.get("fromTime") || values.get("toTime") ? "custom" : "all"), fromTime: localDateTime(values.get("fromTime") || ""), toTime: localDateTime(values.get("toTime") || "") });
    setCursor(undefined); void load();
    return () => { sequence.current++; };
  }, [query, source, load]);
  const apply = () => {
    const result = composeLogFilters(dates, keyword);
    setErrors(result.errors);
    if (Object.keys(result.errors).length) return;
    result.params.set("source", selectedSource);
    if (result.params.toString() === query) void load(); else setParams(result.params);
  };
  const lines = source === "app" ? page?.rawLines.map(line => ({ time: line.timestamp, origin: line.fileName, text: line.text, key: line.logId })) || [] : journal?.items.map((line,index) => ({ time: line.timestamp, origin: journal.unit, text: line.message, key: `${line.timestamp}-${index}` })) || [];
  const time = (value: string) => new Date(value).toLocaleString("zh-CN", { hour12: false });
  return <main className="page raw-logs"><Link to="/logs" className="back-link">← 返回调用日志</Link><header className="page-title"><div><p className="overline">后台日志原文</p><h1>原始日志</h1><p>直接查看应用文件行或本服务journalctl输出，经安全脱敏，不开放请求正文、生产凭据或其他服务日志。</p></div><Button kind="tertiary" disabled={busy} onClick={() => load(cursor)}>刷新已应用条件</Button></header>
    <form onSubmit={event => { event.preventDefault(); apply(); }} className="raw-log-query"><Select id="raw-source" labelText="日志来源" value={selectedSource} onChange={event => setSelectedSource(event.target.value as "app" | "journal")}><SelectItem value="app" text="应用日志文件原文（JSONL）" /><SelectItem value="journal" text="服务控制台（journalctl）" /></Select><TextInput id="raw-keyword" labelText="关键词" maxLength={200} invalid={!!errors.keyword} invalidText={errors.keyword} value={keyword} onChange={event => setKeyword(event.target.value)} placeholder="错误码、启动信息或操作名称" /><Select id="raw-range" labelText="时间范围" value={dates.range || "all"} onChange={event => setDates({ ...dates, range: event.target.value })}><SelectItem value="all" text="不限时间" /><SelectItem value="1h" text="最近1小时" /><SelectItem value="24h" text="最近24小时" /><SelectItem value="7d" text="最近7天" /><SelectItem value="custom" text="自定义时间" /></Select><Button type="submit" disabled={busy}>查询</Button></form>
    {dates.range === "custom" && <div className="log-date-controls"><TextInput id="raw-from" labelText="开始时间（含）" type="datetime-local" invalid={!!errors.fromTime} invalidText={errors.fromTime} value={dates.fromTime || ""} onChange={event => setDates({ ...dates, fromTime: event.target.value })} /><TextInput id="raw-to" labelText="结束时间（不含）" type="datetime-local" invalid={!!errors.toTime} invalidText={errors.toTime} value={dates.toTime || ""} onChange={event => setDates({ ...dates, toTime: event.target.value })} /></div>}
    {Object.keys(errors).length > 0 && <InlineNotification kind="warning" title="请调整筛选条件" subtitle={Object.values(errors).join("；")} hideCloseButton />}
    {error && <InlineNotification kind="error" title="暂时无法读取后台日志" subtitle={error} hideCloseButton />}
    {source === "journal" && journal && (!journal.available || journal.permissionLimited) && <InlineNotification kind="warning" title={journal.reason === "NOT_LINUX" ? "当前为本地调试环境" : "控制台日志不可用或权限不完整"} subtitle={journal.message} hideCloseButton />}
    <p className="log-pairing-note">当前已应用来源：{source === "app" ? "应用日志文件" : "服务控制台"}。修改来源、关键词或时间后点击“查询”生效；刷新只读取已应用条件。</p>
    <p className="log-pairing-note">{source === "app" ? "读取程序保存的JSONL事件行，不进行调用合并。每批最多100条，单次扫描最多8MiB；被清理或从未保存的内容无法恢复。" : `固定读取itsm-workflow-api.service，包含启动和异常信息；每次只取所选范围最新${journal?.limit || 100}条，关键词仅在这批中筛选。请缩小时间范围查看更早记录。`}</p>
    {source === "journal" && <details className="journal-help"><summary>journalctl权限与服务启动失败排查</summary><p>页面不会执行sudo或修改系统用户组。管理员可先确认systemd-journal组存在，再按主机安全策略选择是否授权：</p><pre>{"getent group systemd-journal\nsudo usermod -aG systemd-journal itsmworkflow\nsudo systemctl restart itsm-workflow-api"}</pre><p>该组通常具备更广的系统journal读取权限；页面仍固定只读本服务，不允许选择其他unit。若组不存在，请由管理员核实发行版权限方案，不自动创建或开放任意sudo。</p><p>如果API本身无法启动，页面不能通过已停止的API读取日志，请直接在主机执行：</p><pre>sudo journalctl -u itsm-workflow-api.service -n 100 --no-pager -o cat</pre><p>journal由系统保留策略管理，不受应用日志512MiB限额约束。</p></details>}
    {busy && <InlineLoading description="读取后台日志原文" />}
    <section className="raw-log-lines" aria-label="后台日志原文">{lines.map(line => <article key={line.key}><header><time>{time(line.time)}</time><code>{line.origin}</code></header><pre>{line.text}</pre></article>)}</section>
    {!busy && !lines.length && <div className="log-empty"><h2>{source === "journal" && journal && !journal.available ? "当前无法获取控制台日志" : "当前读取范围没有匹配记录"}</h2><p>可清空关键词、调整时间范围或查看上方权限说明；没有日志不代表未执行过操作。</p></div>}
    {source === "app" && <footer className="log-pagination"><Button kind="tertiary" disabled={busy || !cursor} onClick={() => load()}>返回最新</Button><span>原始文件行 · 不统计全量总数</span><Button kind="tertiary" disabled={busy || !page?.nextCursor} onClick={() => load(page?.nextCursor || undefined)}>读取更早原文</Button></footer>}
  </main>;
}
