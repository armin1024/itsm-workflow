import { useEffect, useState } from "react";
import { Button, InlineLoading, InlineNotification, Select, SelectItem, Tag, TextArea, TextInput } from "@carbon/react";
import { Link, NavLink, Navigate, Route, Routes } from "react-router-dom";
import { api } from "./api";
import WorkflowEditor, { newWorkflow, type EditableWorkflow } from "./WorkflowEditor";
import type { NodeCatalogItem } from "./types";

const pretty = (value: unknown) => JSON.stringify(value, null, 2);
const parseObject = (value: string, label: string) => {
  const parsed = JSON.parse(value);
  if (!parsed || Array.isArray(parsed) || typeof parsed !== "object") throw new Error(`${label}必须是JSON对象`);
  return parsed as Record<string, unknown>;
};
const parseArray = (value: string, label: string) => {
  const parsed = JSON.parse(value);
  if (!Array.isArray(parsed) || !parsed.every((item) => item && typeof item === "object" && !Array.isArray(item))) throw new Error(`${label}必须是JSON对象数组`);
  return parsed as Record<string, unknown>[];
};

function Status({ value }: { value: string }) {
  const kind = value === "SUCCEEDED" ? "green" : value === "FAILED" ? "red" : value.startsWith("WAITING") ? "warm-gray" : "cool-gray";
  return <Tag type={kind as "green"}>{value}</Tag>;
}

function Shell() {
  return <div className="runtime-shell">
    <header className="topbar">
      <Link className="wordmark" to="/studio"><span>IW</span><strong>Runtime Studio</strong></Link>
      <nav aria-label="主导航"><NavLink to="/studio">节点调试</NavLink><NavLink to="/workflow">流程编排</NavLink><NavLink to="/compiler">草稿提取</NavLink><a href="./docs" target="_blank" rel="noreferrer">API 文档</a></nav>
      <div className="runtime-badge">TEST_ONLY</div>
    </header>
    <Routes>
      <Route path="/studio" element={<NodeStudio />} />
      <Route path="/workflow" element={<WorkflowLab />} />
      <Route path="/compiler" element={<CompilerLab />} />
      <Route path="*" element={<Navigate to="/studio" replace />} />
    </Routes>
  </div>;
}

function NodeStudio() {
  const [catalog, setCatalog] = useState<NodeCatalogItem[]>([]);
  const [selected, setSelected] = useState<NodeCatalogItem>();
  const [digest, setDigest] = useState("");
  const [mode, setMode] = useState("SIMULATION");
  const [nodeJson, setNodeJson] = useState("{}");
  const [inputsJson, setInputsJson] = useState("{}");
  const [fixtureJson, setFixtureJson] = useState("{}");
  const [ticketId, setTicketId] = useState("");
  const [apiKey, setApiKey] = useState("");
  const [manualValue, setManualValue] = useState("");
  const [result, setResult] = useState<Record<string, unknown>>();
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  const choose = (item: NodeCatalogItem) => {
    const ui = item.uiSchema || {};
    setSelected(item);
    setNodeJson(pretty({ id: "debug-node", type: item.type, schemaVersion: item.schemaVersion, handlerVersion: item.handlerVersion, title: item.name, config: ui.debugConfig || {}, inputs: [], approvalPolicy: item.approvalPolicy, timeoutSeconds: 60 }));
    setInputsJson(pretty(ui.debugInputs || {}));
    setFixtureJson(pretty({ fixtureOutput: ui.debugFixture || {}, rule: ui.debugRule }));
    setResult(undefined); setError(""); setApiKey("");
  };
  useEffect(() => { api.studioCatalog().then((value) => { setCatalog(value.nodes); setDigest(value.catalogDigest); const initial = value.nodes.find((item) => item.type === "sql_read") || value.nodes[0]; if (initial) choose(initial); }).catch((value) => setError(value.message)); }, []);
  const run = async () => {
    setBusy(true); setError("");
    try {
      const body = { node: parseObject(nodeJson, "节点定义"), inputs: parseObject(inputsJson, "节点输入"), mode, simulation: parseObject(fixtureJson, "模拟配置"), ticketId: ticketId ? Number(ticketId) : undefined };
      setResult(await api.studioDebugNode(body, mode === "TEST" ? apiKey : undefined));
      setApiKey("");
    } catch (value) { setError((value as Error).message); } finally { setBusy(false); }
  };
  const reply = async (response: Record<string, unknown>) => {
    if (!result?.debugRunId) return;
    setBusy(true); setError("");
    try { setResult(await api.studioDebugReply(String(result.debugRunId), response)); } catch (value) { setError((value as Error).message); } finally { setBusy(false); }
  };
  const interrupt = result?.status === "WAITING_INPUT" ? result.interrupt as Record<string, unknown> : undefined;
  const candidates = (interrupt?.candidates as Record<string, unknown>[] | undefined) || [];
  const needsAops = mode === "TEST" && selected?.type === "sql_read";
  return <main className="page studio-page">
    <header className="page-title"><div><p className="overline">NODE REGISTRY / LOCAL STUDIO</p><h1>节点调试</h1><p>同一份节点定义支持模拟、预检和真实测试。AOPS API Key只在本次请求与CLI子进程内存中使用。</p></div><code>{digest ? digest.slice(0, 12) : "loading"}</code></header>
    {error && <InlineNotification kind="error" title="调试失败" subtitle={error} hideCloseButton />}
    <div className="studio-layout">
      <aside className="studio-catalog"><header><b>Node Catalog</b><span>{catalog.length} 类型</span></header>{catalog.map((item) => <button type="button" className={selected?.type === item.type ? "active" : ""} key={`${item.type}-${item.schemaVersion}`} onClick={() => choose(item)}><span>{item.category}</span><b>{item.name}</b><small>{item.type} · schema {item.schemaVersion} · handler {item.handlerVersion}</small></button>)}</aside>
      <section className="studio-editor">{selected ? <>
        <header><div><p className="overline">SINGLE NODE DEBUG</p><h2>{selected.name}</h2><p>{selected.description}</p></div><Status value="READY" /></header>
        <form className="debug-controls" onSubmit={(event) => event.preventDefault()}><Select id="debug-mode" labelText="执行模式" value={mode} onChange={(event) => setMode(event.target.value)}>{selected.supportedModes.filter((item) => item !== "PRODUCTION").map((item) => <SelectItem key={item} value={item} text={item} />)}</Select>{needsAops && <TextInput id="ticket-id" labelText="当前工单 ID" value={ticketId} onChange={(event) => setTicketId(event.target.value)} inputMode="numeric" />}{needsAops && <TextInput id="aops-key" labelText="AOPS_API_KEY（不保存）" type="password" value={apiKey} onChange={(event) => setApiKey(event.target.value)} autoComplete="off" />}</form>
        {needsAops && <InlineNotification kind="warning" title="真实只读测试" subtitle="将调用系统aops-cli，并产生AOPS审计记录；请确认数据库路径、SQL与工单ID。" hideCloseButton />}
        <div className="studio-json-grid"><TextArea id="studio-node" labelText="节点定义 JSON" rows={14} value={nodeJson} onChange={(event) => setNodeJson(event.target.value)} /><TextArea id="studio-inputs" labelText="节点输入 JSON" rows={14} value={inputsJson} onChange={(event) => setInputsJson(event.target.value)} /><TextArea id="studio-fixture" labelText="Simulation Adapter JSON" rows={14} value={fixtureJson} onChange={(event) => setFixtureJson(event.target.value)} /></div>
        <div className="studio-actions"><span>调试记录将在{24}小时后清理；凭据永不写入记录</span><Button onClick={run} disabled={busy || (needsAops && (!apiKey || !ticketId))}>{busy ? "正在运行" : "运行单节点"}</Button></div>
      </> : <InlineLoading description="加载Node Catalog" />}</section>
      <aside className="studio-result"><header><b>调试结果</b>{result && <Status value={String(result.status)} />}</header>{result ? <><dl><div><dt>Debug Run</dt><dd>{String(result.debugRunId)}</dd></div><div><dt>模式</dt><dd>{String(result.mode)}</dd></div><div><dt>Handler</dt><dd>{String(result.handlerVersion)}</dd></div></dl>{interrupt && <section className="studio-interrupt"><b>{String(interrupt.title || "请选择")}</b>{candidates.map((item) => <button type="button" key={String(item.id)} onClick={() => reply({ action: "SELECT", candidateId: item.id })}><strong>{String(item.label || item.id)}</strong><small>{String(item.value)}</small></button>)}<TextInput id="studio-manual" labelText="直接输入值" value={manualValue} onChange={(event) => setManualValue(event.target.value)} /><div><Button size="sm" onClick={() => reply({ action: "MANUAL_VALUE", value: manualValue })} disabled={!manualValue}>使用该值</Button><Button size="sm" kind="danger--ghost" onClick={() => reply({ action: "CANCEL" })}>取消</Button></div></section>}<h3>输出</h3><pre>{pretty(result.output)}</pre>{result.diagnostic && <><h3>诊断</h3><pre>{pretty(result.diagnostic)}</pre></>}</> : <div className="inspector-empty"><span>01</span><h3>等待调试</h3><p>选择节点和模式。SIMULATION不访问外部系统，TEST使用真实适配器。</p></div>}</aside>
    </div>
  </main>;
}

function WorkflowLab() {
  const [definition, setDefinition] = useState<EditableWorkflow>(newWorkflow());
  const [result, setResult] = useState<Record<string, unknown>>();
  const [error, setError] = useState("");
  const validate = async () => { setError(""); try { setResult(await api.validateWorkflow(definition as unknown as Record<string, unknown>)); } catch (value) { setError((value as Error).message); } };
  return <main className="page workflow-lab"><header className="page-title"><div><p className="overline">DAG AUTHORING / TEST</p><h1>流程编排</h1><p>拖拽节点、连接条件和结果依赖，然后使用正式Registry执行静态校验。这里不发布生产知识。</p></div><Button onClick={validate}>校验流程</Button></header>{error && <InlineNotification kind="error" title="流程校验失败" subtitle={error} hideCloseButton />}<WorkflowEditor definition={definition} onChange={setDefinition} />{result && <section className="validation-result"><h2>校验通过</h2><p>Catalog <code>{String(result.catalogDigest).slice(0, 16)}</code></p><pre>{pretty(result)}</pre></section>}</main>;
}

function CompilerLab() {
  const [source, setSource] = useState<"ticket" | "json">("ticket");
  const [ticketId, setTicketId] = useState("");
  const [apiKey, setApiKey] = useState("");
  const [ticketJson, setTicketJson] = useState(pretty({ id: 100173, incident_id: "INC-2026-0001", event_title: "客户信息查询", incident_description: "查询客户信息", system_list: [{ id: "crm", name: "客户系统" }] }));
  const [timelineJson, setTimelineJson] = useState("[]");
  const [result, setResult] = useState<Record<string, unknown>>();
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const compile = async () => { setBusy(true); setError(""); try { const value = source === "ticket" ? await api.compileTicket(Number(ticketId), apiKey) : await api.compileEvidence(parseObject(ticketJson, "工单详情"), parseArray(timelineJson, "操作记录")); setResult(value); setApiKey(""); } catch (value) { setError((value as Error).message); } finally { setBusy(false); } };
  return <main className="page compiler-page"><header className="page-title"><div><p className="overline">WORKFLOW COMPILER / PREVIEW</p><h1>草稿提取</h1><p>从工单证据过滤成功的SQL读操作，经LLM生成中文经验定义和线性DAG。结果只作预览，不保存生产知识。</p></div></header>{error && <InlineNotification kind="error" title="提取失败" subtitle={error} hideCloseButton />}<div className="compiler-tabs"><Button kind={source === "ticket" ? "primary" : "ghost"} onClick={() => setSource("ticket")}>按工单ID提取</Button><Button kind={source === "json" ? "primary" : "ghost"} onClick={() => setSource("json")}>直接传JSON</Button></div><div className="compiler-grid"><form className="compiler-input" onSubmit={(event) => { event.preventDefault(); void compile(); }}>{source === "ticket" ? <><TextInput id="compiler-ticket" labelText="工单ID" value={ticketId} onChange={(event) => setTicketId(event.target.value)} /><TextInput id="compiler-key" labelText="AOPS_API_KEY（不保存）" type="password" value={apiKey} onChange={(event) => setApiKey(event.target.value)} autoComplete="off" /><InlineNotification kind="info" title="调用范围" subtitle="服务将执行event-center info与audit_timeline，仅把响应data交给Compiler。" hideCloseButton /></> : <><TextArea id="ticket-json" labelText="ticketInfo（info.data对象）" rows={14} value={ticketJson} onChange={(event) => setTicketJson(event.target.value)} /><TextArea id="timeline-json" labelText="auditTimeline（audit_timeline.data数组）" rows={14} value={timelineJson} onChange={(event) => setTimelineJson(event.target.value)} /></>}<Button type="submit" disabled={busy || (source === "ticket" && (!ticketId || !apiKey))}>{busy ? "正在提取" : "生成草稿预览"}</Button></form><section className="compiler-output"><header><h2>DraftProposal</h2>{result && <Status value="READY" />}</header>{result ? <pre>{pretty(result)}</pre> : <div className="inspector-empty"><span>02</span><h3>尚未生成</h3><p>结果包含名称、摘要、匹配短语、过滤诊断和可编辑Workflow DAG。</p></div>}</section></div></main>;
}

export default function App() { return <Shell />; }
