import { useEffect, useState } from "react";
import { Button, InlineNotification, Select, SelectItem, TextArea, TextInput } from "@carbon/react";
import { api } from "./api";
import WorkflowEditor, { type EditableWorkflow } from "./WorkflowEditor";
import type { NodeCatalogItem } from "./types";
import { parseArray, parseObject, pretty, Status } from "./ui";

export default function CompilerLab() {
  const [source, setSource] = useState<"ticket" | "json">("ticket");
  const [ticketId, setTicketId] = useState("");
  const [apiKey, setApiKey] = useState("");
  const [ticketJson, setTicketJson] = useState(pretty({ id: 100173, incident_id: "INC-2026-0001", event_title: "客户信息查询", incident_description: "查询客户信息", system_list: [{ id: "crm", name: "客户系统" }] }));
  const [timelineJson, setTimelineJson] = useState("[]");
  const [compiled, setCompiled] = useState<Record<string, unknown>>();
  const [definition, setDefinition] = useState<EditableWorkflow>();
  const [catalog, setCatalog] = useState<NodeCatalogItem[]>([]);
  const [runInputs, setRunInputs] = useState("{}");
  const [selectedNodeId, setSelectedNodeId] = useState("");
  const [debugResult, setDebugResult] = useState<Record<string, unknown>>();
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => { api.studioCatalog().then((value) => setCatalog(value.nodes)); }, []);
  const hasSql = definition?.nodes.some((node) => node.type === "sql_read") || false;
  const selectedNode = definition?.nodes.find((node) => node.id === selectedNodeId);
  const selectedIsSql = selectedNode?.type === "sql_read";
  const nodeResults = (debugResult?.nodeResults as Record<string, unknown>[] | undefined) || [];
  const proposal = compiled?.proposal as Record<string, unknown> | undefined;

  const prepareDebug = (workflow: EditableWorkflow) => {
    setDefinition(workflow);
    setSelectedNodeId(workflow.nodes[0]?.id || "");
    setDebugResult(undefined);
    const inputs: Record<string, unknown> = {};
    workflow.nodes.forEach((node) => node.inputs.forEach((item) => {
      if (item.source.kind === "RUN_INPUT" && item.source.key && !(item.source.key in inputs)) inputs[item.source.key] = item.type === "number" || item.type === "integer" ? 0 : "";
    }));
    setRunInputs(pretty(inputs));
  };

  const compile = async () => {
    setBusy(true); setError("");
    try {
      const value = source === "ticket" ? await api.compileTicket(Number(ticketId), apiKey) : await api.compileEvidence(parseObject(ticketJson, "工单详情"), parseArray(timelineJson, "操作记录"));
      setCompiled(value);
      prepareDebug((value.proposal as Record<string, unknown>).workflowDefinition as unknown as EditableWorkflow);
      setApiKey("");
    } catch (value) { setError((value as Error).message); }
    finally { setBusy(false); }
  };

  const debugOne = async () => {
    if (!selectedNode) return;
    setBusy(true); setError("");
    try {
      const raw = parseObject(runInputs, "调试输入");
      const values: Record<string, unknown> = {};
      selectedNode.inputs.forEach((item) => {
        if (item.name in raw) values[item.name] = raw[item.name];
        else if (item.source.kind === "RUN_INPUT" && item.source.key && item.source.key in raw) values[item.name] = raw[item.source.key];
        else if (item.source.kind === "LITERAL") values[item.name] = item.source.value;
      });
      setDebugResult(await api.studioDebugNode({ node: selectedNode, inputs: values, mode: "TEST", simulation: {}, ticketId: ticketId ? Number(ticketId) : undefined }, apiKey || undefined));
      setApiKey("");
    } catch (value) { setError((value as Error).message); }
    finally { setBusy(false); }
  };

  const debugAll = async () => {
    if (!definition) return;
    setBusy(true); setError("");
    try {
      setDebugResult(await api.studioDebugWorkflow({ workflowDefinition: definition, inputs: parseObject(runInputs, "运行输入"), mode: "TEST", simulation: {}, ticketId: ticketId ? Number(ticketId) : undefined }, apiKey || undefined));
      setApiKey("");
    } catch (value) { setError((value as Error).message); }
    finally { setBusy(false); }
  };

  const displayedResults = debugResult?.debugRunId && !debugResult.nodeResults ? [debugResult] : nodeResults;
  const allowedTypes = new Set(catalog.filter((node) => node.studioEnabled !== false).map((node) => node.type));
  return <main className="page compiler-page">
    <header className="page-title"><div><p className="overline">WORKFLOW COMPILER / REAL DEBUG</p><h1>草稿提取</h1><p>提取完成后直接渲染DAG；调试固定执行真实Handler，SQL读调用aops-cli。</p></div>{compiled && <Status value="DRAFT_READY" />}</header>
    {error && <InlineNotification kind="error" title="操作失败" subtitle={error} hideCloseButton />}
    <div className="compiler-tabs"><Button kind={source === "ticket" ? "primary" : "ghost"} onClick={() => setSource("ticket")}>按工单ID提取</Button><Button kind={source === "json" ? "primary" : "ghost"} onClick={() => setSource("json")}>直接传JSON</Button></div>
    <div className="compiler-grid compact"><form className="compiler-input" onSubmit={(event) => { event.preventDefault(); void compile(); }}>{source === "ticket" ? <><TextInput id="compiler-ticket" labelText="工单ID" value={ticketId} onChange={(event) => setTicketId(event.target.value)} /><TextInput id="compiler-key" labelText="AOPS_API_KEY（提取完成即清空）" type="password" value={apiKey} onChange={(event) => setApiKey(event.target.value)} autoComplete="off" /></> : <><TextArea id="ticket-json" labelText="ticketInfo（info.data）" rows={8} value={ticketJson} onChange={(event) => setTicketJson(event.target.value)} /><TextArea id="timeline-json" labelText="auditTimeline（audit_timeline.data）" rows={8} value={timelineJson} onChange={(event) => setTimelineJson(event.target.value)} /></>}<Button type="submit" disabled={busy || (source === "ticket" && (!ticketId || !apiKey))}>{busy ? "正在处理" : "生成可视化草稿"}</Button></form><section className="compiler-output"><header><h2>提取摘要</h2></header>{proposal ? <dl className="proposal-summary"><div><dt>名称</dt><dd>{String(proposal.name)}</dd></div><div><dt>摘要</dt><dd>{String(proposal.summary)}</dd></div><div><dt>匹配短语</dt><dd>{((proposal.matchPhrases || []) as string[]).join("、")}</dd></div></dl> : <div className="inspector-empty"><span>02</span><h3>尚未生成</h3><p>生成后在下方直接显示可编辑工作流画布。</p></div>}</section></div>
    {definition && <><section className="draft-canvas"><header><div><p className="overline">COMPILED DAG</p><h2>可视化草稿</h2></div><code>{definition.nodes.length} nodes · {definition.edges.length} edges</code></header><WorkflowEditor definition={definition} onChange={setDefinition} allowedTypes={allowedTypes} /></section><section className="flow-debug-panel"><header><div><p className="overline">REAL STEP / FLOW DEBUG</p><h2>真实调试控制台</h2></div>{debugResult && <Status value={String(debugResult.status)} />}</header><InlineNotification kind="warning" title="调试会执行真实操作" subtitle="包含SQL读节点时将执行aops-cli db read并产生AOPS审计记录，必须提供当前工单ID和AOPS_API_KEY。" hideCloseButton /><form className="flow-debug-controls flow-debug-controls-real" onSubmit={(event) => event.preventDefault()}><Select id="selected-node" labelText="单步节点" value={selectedNodeId} onChange={(event) => setSelectedNodeId(event.target.value)}>{definition.nodes.map((node) => <SelectItem key={node.id} value={node.id} text={`${node.title} (${node.id})`} />)}</Select>{hasSql && <TextInput id="flow-ticket" labelText="当前工单ID" value={ticketId} onChange={(event) => setTicketId(event.target.value)} />}{hasSql && <TextInput id="flow-key" labelText="AOPS_API_KEY（不保存）" type="password" value={apiKey} onChange={(event) => setApiKey(event.target.value)} autoComplete="off" />}</form><div className="flow-debug-json flow-debug-json-one"><TextArea id="flow-inputs" labelText="运行输入 JSON" rows={9} value={runInputs} onChange={(event) => setRunInputs(event.target.value)} /></div><div className="studio-actions"><span>单步调试不会伪造前置节点输出；依赖值需在运行输入中明确填写</span><div><Button kind="secondary" onClick={debugOne} disabled={busy || !selectedNode || (selectedIsSql && (!ticketId || !apiKey))}>真实调试选中节点</Button><Button onClick={debugAll} disabled={busy || (hasSql && (!ticketId || !apiKey))}>真实调试整个流程</Button></div></div>{displayedResults.length > 0 && <div className="node-result-strip">{displayedResults.map((item, index) => <article key={String(item.nodeId || item.debugRunId || index)}><header><b>{String(item.title || item.nodeId || "单节点")}</b><Status value={String(item.status)} /></header><pre>{pretty(item.output || item.diagnostic)}</pre></article>)}</div>}</section></>}
  </main>;
}
