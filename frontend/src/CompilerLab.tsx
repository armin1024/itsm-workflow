import { useEffect, useState } from "react";
import { Button, Checkbox, InlineNotification, TextArea, TextInput } from "@carbon/react";
import { useNavigate } from "react-router-dom";
import { api } from "./api";
import { browserStore, localCatalog, localId, now, useBrowserItems, type LocalExtraction, type LocalKnowledge } from "./browserStore";
import WorkflowEditor, { type EditableWorkflow } from "./WorkflowEditor";
import { parseArray, parseObject, pretty, Status } from "./ui";

export default function CompilerLab() {
  const [source, setSource] = useState<"ticket" | "json">("ticket"), [ticketId, setTicketId] = useState(""), [apiKey, setApiKey] = useState("");
  const [ticketJson, setTicketJson] = useState(pretty({ id: 100173, incident_id: "INC-2026-0001", event_title: "客户信息查询", incident_description: "查询客户信息", system_list: [{ id: "crm", name: "客户系统" }] })), [timelineJson, setTimelineJson] = useState("[]");
  const [syncTec01, setSyncTec01] = useState(false), [tec01Id, setTec01Id] = useState(""), [selectedId, setSelectedId] = useState("");
  const [definition, setDefinition] = useState<EditableWorkflow>(), [name, setName] = useState(""), [summary, setSummary] = useState(""), [allowed, setAllowed] = useState<Set<string>>();
  const [error, setError] = useState(""), [busy, setBusy] = useState(false), [loadedId, setLoadedId] = useState("");
  const { items: extractions, error: storageError } = useBrowserItems<LocalExtraction>("extraction"), navigate = useNavigate();
  const task = extractions.find((item) => item.id === selectedId), proposal = task?.proposal;
  useEffect(() => { if (!selectedId && extractions[0]) setSelectedId(extractions[0].id); }, [selectedId, extractions]);
  useEffect(() => { api.studioCatalog().then(async (value) => setAllowed(new Set((await localCatalog(value.nodes)).filter((item) => item.studioEnabled !== false).map((item) => item.type)))).catch((error) => setError(error.message)); }, []);
  useEffect(() => {
    if (task?.status === "SUCCEEDED" && task.proposal && loadedId !== task.id) {
      setDefinition(task.proposal.workflowDefinition as EditableWorkflow); setName(String(task.proposal.name)); setSummary(String(task.proposal.summary)); setLoadedId(task.id);
    }
  }, [task, loadedId]);
  const compile = async () => {
    setBusy(true); setError("");
    try {
      if (syncTec01 && !tec01Id.trim()) throw new Error("同步前请填写 tec01 已创建的经验ID");
      const info = source === "json" ? parseObject(ticketJson, "工单详情") : undefined, timeline = source === "json" ? parseArray(timelineJson, "操作记录") : undefined;
      const id = source === "ticket" ? Number(ticketId) : Number(info?.id);
      if (!Number.isInteger(id) || id <= 0) throw new Error("工单ID必须为正整数");
      const knowledgeId = syncTec01 ? tec01Id.trim() : localId("local_ext");
      if (extractions.some((item) => item.id === knowledgeId && item.status === "EXTRACTING")) throw new Error("该经验ID已有正在处理的提取任务");
      const item: LocalExtraction = { id: knowledgeId, kind: "extraction", status: "EXTRACTING", ticketId: id, syncTec01, syncStatus: syncTec01 ? "PENDING" : "DISABLED", lastEventId: 0, events: [], createdAt: now(), updatedAt: now() };
      await api.extract({ knowledgeId, requestId: localId("extract_request"), ticketId: id, ...(info ? { ticketInfo: info, auditTimeline: timeline } : {}), syncTec01 }, source === "ticket" ? apiKey : undefined, source === "ticket");
      await browserStore.put(item); setSelectedId(knowledgeId); setDefinition(undefined); setLoadedId(""); setApiKey("");
    } catch (error) { setError((error as Error).message); }
    finally { setBusy(false); }
  };
  const save = async () => {
    if (!task || !proposal || !definition) return;
    setBusy(true); setError("");
    try {
      if (!name.trim()) throw new Error("请填写经验名称");
      const item: LocalKnowledge = { id: localId("local_knw"), kind: "knowledge", name: name.trim(), summary, matchPhrases: (proposal.matchPhrases || []) as string[], negativePhrases: (proposal.negativePhrases || []) as string[], systemKeys: (proposal.systemKeys || []) as string[], workflowDefinition: definition, ticketId: task.ticketId, source: "EXTRACTION", createdAt: now(), updatedAt: now() };
      await browserStore.put(item); navigate(`/knowledge?knowledge=${item.id}`);
    } catch (error) { setError((error as Error).message); }
    finally { setBusy(false); }
  };
  const progress = task?.events.filter((item) => item.type === "PROGRESS") || [], latest = progress.at(-1);
  return <main className="page compiler-page"><header className="page-title"><div><p className="overline">WORKFLOW COMPILER / BROWSER LIBRARY</p><h1>知识抽取</h1><p>查看逐阶段提取进度，编辑生成的流程，并保存到浏览器的本地知识库后测试执行。</p></div>{task && <Status value={task.status} />}</header>{(error || storageError) && <InlineNotification kind="error" title="操作未完成" subtitle={error || storageError} hideCloseButton />}<div className="compiler-tabs"><Button kind={source === "ticket" ? "primary" : "ghost"} onClick={() => setSource("ticket")}>按工单ID提取</Button><Button kind={source === "json" ? "primary" : "ghost"} onClick={() => setSource("json")}>直接传JSON</Button></div><div className="compiler-grid compact"><form className="compiler-input" onSubmit={(event) => { event.preventDefault(); void compile(); }}>{source === "ticket" ? <><TextInput id="compiler-ticket" labelText="工单ID *" value={ticketId} onChange={(event) => setTicketId(event.target.value)} /><TextInput id="compiler-key" labelText="AOPS_API_KEY（请求完成即清空，不保存）" type="password" value={apiKey} onChange={(event) => setApiKey(event.target.value)} autoComplete="off" /></> : <><TextArea id="ticket-json" labelText="ticketInfo（info.data 对象）" rows={8} value={ticketJson} onChange={(event) => setTicketJson(event.target.value)} /><TextArea id="timeline-json" labelText="auditTimeline（audit_timeline.data 数组）" rows={8} value={timelineJson} onChange={(event) => setTimelineJson(event.target.value)} /></>}<Checkbox id="extraction-sync" labelText="将提取进度与结果同步到 tec01" checked={syncTec01} onChange={(_, { checked }) => setSyncTec01(checked)} />{syncTec01 && <TextInput id="extraction-tec01-id" labelText="tec01 已创建的经验ID *" helperText="tec01须先创建对应提取记录，回调将使用此ID。" value={tec01Id} onChange={(event) => setTec01Id(event.target.value)} />}<Button type="submit" disabled={busy || (source === "ticket" && (!ticketId || !apiKey))}>{busy ? "正在提交" : "提取工作流草稿"}</Button></form><section className="compiler-output"><header><h2>提取进度</h2>{task && <code>{task.id}</code>}</header>{task ? <>{task.syncTec01 && <div className="extraction-sync-state"><Status value={task.syncStatus || "PENDING"} /><span>{task.syncStatus === "FAILED" ? "草稿保留在本地，但 tec01 尚未确认回调。" : task.syncStatus === "SYNCED" ? "最近回调已同步 tec01。" : "等待 tec01 确认同步。"}</span>{task.callbackError != null && <pre>{pretty(task.callbackError)}</pre>}</div>}<div className="extraction-stage"><span className={task.status === "EXTRACTING" ? "live-indicator" : ""} /><b>{task.status === "EXTRACTING" ? String(latest?.message || "正在准备提取任务") : task.status === "SUCCEEDED" ? "草稿已生成，可编辑并保存" : "提取未完成"}</b></div><ol className="extraction-progress">{progress.map((event, index) => <li key={String(event.eventId || index)}><time>{new Date(String(event.occurredAt || task.createdAt)).toLocaleTimeString("zh-CN")}</time><div><b>{String(event.message || event.stage)}</b>{(event.progress as Record<string, unknown>)?.total != null && <small>{String((event.progress as Record<string, unknown>).current ?? "—")} / {String((event.progress as Record<string, unknown>).total)}</small>}</div></li>)}</ol>{task.error != null && <pre>{pretty(task.error)}</pre>}{task.diagnostics != null && <details><summary>操作过滤与提取诊断</summary><pre>{pretty(task.diagnostics)}</pre></details>}</> : <div className="inspector-empty"><span>01</span><h3>等待提取任务</h3><p>提取过程中显示过滤、参数化、LLM分析及流程校验进度。</p></div>}{extractions.length > 0 && <details className="extraction-history"><summary>本地提取记录（{extractions.length}）</summary>{extractions.map((item) => <div key={item.id}><button onClick={() => { setSelectedId(item.id); setLoadedId(""); setDefinition(undefined); }}>{item.ticketId} · {new Date(item.createdAt).toLocaleString("zh-CN")} · {item.status}</button>{item.status !== "EXTRACTING" && <button aria-label="删除提取记录" onClick={() => { void browserStore.remove(item.id); if (item.id === selectedId) { setSelectedId(""); setDefinition(undefined); } }}>移除</button>}</div>)}</details>}</section></div>{definition && <section className="draft-canvas"><header><div><p className="overline">EXTRACTED DRAFT</p><h2>编辑并保留经验</h2></div><Button disabled={busy} onClick={save}>保存到本地知识库</Button></header><div className="two-fields"><TextInput id="extracted-name" labelText="经验名称" value={name} onChange={(event) => setName(event.target.value)} /><TextArea id="extracted-summary" labelText="经验摘要" rows={2} value={summary} onChange={(event) => setSummary(event.target.value)} /></div><WorkflowEditor key={loadedId} definition={definition} onChange={setDefinition} allowedTypes={allowed} /></section>}</main>;
}
