import { useEffect, useRef, useState } from "react";
import { Button, Checkbox, InlineNotification, Select, SelectItem, TextArea, TextInput } from "@carbon/react";
import { useLocation, useNavigate, useSearchParams } from "react-router-dom";
import { api } from "./api";
import { browserStore, downloadJson, localCatalog, localId, now, useBrowserItems, type LocalKnowledge, type LocalRun } from "./browserStore";
import HitlPanel, { typedValue, ValueInput } from "./HitlPanel";
import LocalRunPanel from "./LocalRunPanel";
import LocalLists from "./LocalLists";
import WorkflowEditor, { newWorkflow, type EditableWorkflow } from "./WorkflowEditor";
import { pretty, Status } from "./ui";

const phraseList = (text: string) => [...new Set(text.split(/[\n,，]/).map((value) => value.trim()).filter(Boolean))];
const date = (value: string) => new Date(value).toLocaleString("zh-CN", { hour12: false });
type InputField = { key: string; label: string; type: string; required: boolean; description?: string };
function workflowInputs(definition: EditableWorkflow): InputField[] {
  const fields = new Map<string, InputField>();
  for (const node of definition.nodes) for (const input of node.inputs) if (input.source.kind === "RUN_INPUT" && input.source.key) {
    const key = input.source.key, old = fields.get(key);
    fields.set(key, { key, label: key, type: input.type, description: input.description, required: old?.required || input.required !== false });
  }
  return [...fields.values()];
}
export default function WorkflowLab() {
  const { items: knowledge, error: storageError } = useBrowserItems<LocalKnowledge>("knowledge"), { items: runs } = useBrowserItems<LocalRun>("run");
  const [search, setSearch] = useSearchParams(), [draft, setDraft] = useState<LocalKnowledge>(), [dirty, setDirty] = useState(false);
  const location = useLocation(), navigate = useNavigate();
  const newRunMode = location.pathname === "/runs/new";
  const [allowed, setAllowed] = useState<Set<string>>(), [error, setError] = useState(""), [message, setMessage] = useState(""), [busy, setBusy] = useState(false);
  const [ticketId, setTicketId] = useState(""), [values, setValues] = useState<Record<string, string>>({}), [syncTec01, setSyncTec01] = useState(false), [tec01RunId, setTec01RunId] = useState("");
  const [selectedNode, setSelectedNode] = useState(""), [singleValues, setSingleValues] = useState<Record<string, string>>({}), [singleKey, setSingleKey] = useState(""), [singleResult, setSingleResult] = useState<Record<string, unknown>>(), [singleInputs, setSingleInputs] = useState<Record<string, unknown>>({});
  const importFile = useRef<HTMLInputElement>(null);
  const [matchText, setMatchText] = useState(""), [negativeText, setNegativeText] = useState(""), [systemText, setSystemText] = useState("");
  const tab = location.pathname.startsWith("/runs") || search.get("tab") === "runs" ? "runs" : "knowledge", selectedId = search.get("knowledge"), runId = search.get("run");
  const selectedRun = runs.find((item) => item.id === runId);
  useEffect(() => { api.studioCatalog().then(async (value) => setAllowed(new Set((await localCatalog(value.nodes)).filter((item) => item.studioEnabled !== false).map((item) => item.type)))).catch((error) => setError(error.message)); }, []);
  useEffect(() => {
    if (selectedId && selectedId !== draft?.id) {
      const item = knowledge.find((item) => item.id === selectedId);
      if (item) { setDraft(structuredClone(item)); setMatchText(item.matchPhrases.join("\n")); setNegativeText(item.negativePhrases.join("\n")); setSystemText(item.systemKeys.join(",")); setDirty(false); setValues({}); setTicketId(item.ticketId ? String(item.ticketId) : ""); setSelectedNode(item.workflowDefinition.entryNodeId); setSingleValues({}); setSingleResult(undefined); }
    }
  }, [selectedId, knowledge, draft?.id]);
  const patch = (value: Partial<LocalKnowledge>) => { setDraft((current) => current ? { ...current, ...value } : current); setDirty(true); setMessage(""); };
  const choose = (item: LocalKnowledge) => { setDraft(structuredClone(item)); setMatchText(item.matchPhrases.join("\n")); setNegativeText(item.negativePhrases.join("\n")); setSystemText(item.systemKeys.join(",")); setDirty(false); setValues({}); setTicketId(item.ticketId ? String(item.ticketId) : ""); setSelectedNode(item.workflowDefinition.entryNodeId); setSingleValues({}); setSingleResult(undefined); navigate(`/knowledge?knowledge=${item.id}`); setMessage(""); setError(""); };
  const create = async () => {
    const value: LocalKnowledge = { id: localId("local_knw"), kind: "knowledge", name: "未命名经验", summary: "", matchPhrases: [], negativePhrases: [], systemKeys: [], workflowDefinition: newWorkflow(), source: "MANUAL", createdAt: now(), updatedAt: now() };
    try { await browserStore.put(value); choose(value); } catch (error) { setError((error as Error).message); }
  };
  const save = async () => {
    if (!draft) return;
    setBusy(true); setError("");
    try { if (!draft.name.trim()) throw new Error("请填写经验名称"); const value = { ...draft, name: draft.name.trim(), updatedAt: now() }; await browserStore.put(value); setDraft(value); setDirty(false); setMessage("经验和可视化流程已保存到当前浏览器。"); }
    catch (error) { setError((error as Error).message); }
    finally { setBusy(false); }
  };
  const plan = async () => {
    if (!draft) return;
    setBusy(true); setError(""); setMessage("");
    try {
      const id = Number(ticketId); if (!Number.isInteger(id) || id <= 0) throw new Error("当前工单ID必须为正整数");
      if (syncTec01 && !tec01RunId.trim()) throw new Error("同步前请填写 tec01 已创建的运行ID");
      if (syncTec01 && runs.some((run) => run.syncTec01 && run.tec01RunId === tec01RunId.trim())) throw new Error("本地已有使用该 tec01 运行ID 的计划，请打开原计划继续，或使用新的运行ID");
      const inputs: Record<string, unknown> = {};
      for (const input of workflowInputs(draft.workflowDefinition)) {
        const raw = values[input.key] || ""; if (!raw && !input.required) continue;
        if (!raw && input.required) throw new Error(`请填写运行参数 ${input.key}`);
        inputs[input.key] = typedValue(raw, input.type);
      }
      const result = await api.planWorkflow({ workflowDefinition: draft.workflowDefinition, ticketId: id, inputs });
      if ((result.missingInputs as unknown[] | undefined)?.length) throw new Error(`运行参数不完整：${pretty(result.missingInputs)}`);
      const definition = (result.normalizedDefinition || draft.workflowDefinition) as EditableWorkflow;
      const value: LocalRun = { id: localId("local_run"), kind: "run", knowledgeId: draft.id, name: draft.name, ticketId: id, workflowSnapshot: definition, workflowContentHash: String(result.workflowContentHash), runInputs: inputs, status: "PLANNED", nodeStates: Object.fromEntries(definition.nodes.map((node) => [node.id, node.id === definition.entryNodeId ? "READY" : "PENDING"])), nodeOutputs: {}, selectedRoutes: {}, currentNodeId: definition.entryNodeId, syncTec01, tec01RunId: syncTec01 ? tec01RunId.trim() : undefined, events: [], lastEventId: 0, plan: result, createdAt: now(), updatedAt: now() };
      const saved = { ...draft, ticketId: id, updatedAt: now() }; await browserStore.put(saved); setDraft(saved); setDirty(false); await browserStore.put(value); navigate(`/runs?run=${value.id}`);
    } catch (error) { setError((error as Error).message); }
    finally { setBusy(false); }
  };
  const node = draft?.workflowDefinition.nodes.find((item) => item.id === selectedNode);
  const debugOne = async (response?: Record<string, unknown>) => {
    if (!node) return;
    setBusy(true); setError("");
    try {
      if (response) { setSingleResult(await api.studioDebugReply(node as unknown as Record<string, unknown>, singleInputs, response)); return; }
      const inputs: Record<string, unknown> = {};
      for (const input of node.inputs) {
        if (input.source.kind === "LITERAL") inputs[input.name] = input.source.value;
        else if (singleValues[input.name] !== undefined && singleValues[input.name] !== "") inputs[input.name] = typedValue(singleValues[input.name], input.type);
        else if (input.required !== false) throw new Error(`请填写节点输入 ${input.name}。单步执行需要明确提供前置结果。`);
      }
      setSingleInputs(inputs); setSingleResult(await api.studioDebugNode({ node, inputs, mode: "TEST", ticketId: ticketId ? Number(ticketId) : undefined }, singleKey || undefined)); setSingleKey("");
    } catch (error) { setError((error as Error).message); }
    finally { setBusy(false); }
  };
  const loadFile = async (file?: File) => {
    if (!file) return; setError("");
    try {
      if (file.size > 10 * 1024 * 1024) throw new Error("本地经验文件不能超过10 MiB");
      const json = JSON.parse(await file.text()), source = json.knowledge || json;
      if (typeof source.name !== "string" || !source.workflowDefinition || !Array.isArray(source.workflowDefinition.nodes) || !Array.isArray(source.workflowDefinition.edges)) throw new Error("文件应包含名称和 workflowDefinition 节点、连线数组");
      const validated = await api.validateWorkflow(source.workflowDefinition);
      const item: LocalKnowledge = { id: localId("local_knw"), kind: "knowledge", name: source.name, summary: String(source.summary || ""), matchPhrases: Array.isArray(source.matchPhrases) ? source.matchPhrases.map(String) : [], negativePhrases: Array.isArray(source.negativePhrases) ? source.negativePhrases.map(String) : [], systemKeys: Array.isArray(source.systemKeys) ? source.systemKeys.map(String) : [], workflowDefinition: source.workflowDefinition, source: "MANUAL", createdAt: now(), updatedAt: now() };
      item.workflowDefinition = validated.normalizedDefinition as EditableWorkflow;
      await browserStore.put(item); choose(item);
    } catch (error) { setError((error as Error).message); }
    finally { if (importFile.current) importFile.current.value = ""; }
  };
  const unsupported = draft?.workflowDefinition.nodes.filter((node) => !["sql_read", "condition", "hitl_form", "end"].includes(node.type)) || [];
  if ((newRunMode && !selectedId) || (tab === "runs" && !runId && !newRunMode) || (tab === "knowledge" && !selectedId)) return <><input className="visually-hidden" type="file" accept="application/json,.json" ref={importFile} onChange={(event) => void loadFile(event.target.files?.[0])} /><LocalLists mode={location.pathname === "/runs/new" ? "knowledge" : tab} selecting={location.pathname === "/runs/new"} knowledge={knowledge} runs={runs} error={error || storageError} onCreate={create} onImport={() => importFile.current?.click()} /></>;
  return <main className={`page local-workbench detail-only ${newRunMode ? "new-plan-only" : ""}`}><header className="page-title"><div><p className="overline">BROWSER WORKSPACE / REAL EXECUTION</p><a className="back-link" href={tab === "runs" ? "/runs" : "/knowledge"} onClick={(event) => { event.preventDefault(); navigate(tab === "runs" ? "/runs" : "/knowledge"); }}>← {tab === "runs" ? "执行中心" : "知识与版本"}</a><h1>{newRunMode ? "创建运行" : tab === "runs" ? "运行详情" : "编辑本地经验"}</h1><p>浏览器保存知识、计划和节点结果。后台仅执行当前任务；刷新后可继续查看和恢复，不保存执行凭据。</p></div><div className="header-actions"><input className="visually-hidden" type="file" accept="application/json,.json" ref={importFile} onChange={(event) => void loadFile(event.target.files?.[0])} /><Button kind="tertiary" onClick={() => importFile.current?.click()}>导入本地经验</Button><Button onClick={create}>手工新建</Button></div></header>{(error || storageError) && <InlineNotification kind="error" title="操作未完成" subtitle={error || storageError} hideCloseButton />}{unsupported.length > 0 && <InlineNotification kind="error" title="此流程含已停用节点" subtitle={`当前版本不支持：${unsupported.map((node) => `${node.title} (${node.type})`).join("、")}。请删除并改用HITL表单与条件判断，原始流程仍然保留。`} hideCloseButton />}{message && <InlineNotification kind="success" title="已保存" subtitle={message} hideCloseButton />}<div className="browser-storage-notice"><b>当前浏览器 · IndexedDB</b><span>同一地址、同一浏览器内保留数据。清理站点数据会删除记录，请按需导出备份。</span></div><div className="local-workbench-layout"><div className="local-main">{tab === "runs" && !newRunMode ? selectedRun ? <LocalRunPanel run={selectedRun} /> : <section className="empty-state"><span>02</span><h2>选择一条测试计划</h2><p>暂停与等待人工输入时保留全部进展；继续执行仅处理未完成节点。</p></section> : draft ? <><section className="local-knowledge-meta"><header><div><p className="overline">LOCAL KNOWLEDGE</p><h2>{draft.name}</h2><p>{dirty ? "有尚未保存的修改" : `已保存 · ${date(draft.updatedAt)}`}</p></div><div className="header-actions"><Button kind="ghost" size="sm" onClick={() => downloadJson(`${draft.id}.json`, { schema: "itsm-studio-knowledge", schemaVersion: 1, knowledge: draft })}>导出经验</Button><Button kind="danger--ghost" size="sm" onClick={async () => { await browserStore.remove(draft.id); setDraft(undefined); setSearch({}); }}>删除</Button><Button size="sm" disabled={busy} onClick={save}>保存到浏览器</Button></div></header><div className="local-metadata-fields"><TextInput id="knowledge-name" labelText="经验名称" value={draft.name} onChange={(event) => patch({ name: event.target.value })} /><TextArea id="knowledge-summary" labelText="摘要" rows={2} value={draft.summary} onChange={(event) => patch({ summary: event.target.value })} /><TextArea id="knowledge-matches" labelText="匹配短语（逗号或换行分隔）" rows={2} value={matchText} onChange={(event) => { setMatchText(event.target.value); patch({ matchPhrases: phraseList(event.target.value) }); }} /><TextArea id="knowledge-negative" labelText="排除短语（逗号或换行分隔）" rows={2} value={negativeText} onChange={(event) => { setNegativeText(event.target.value); patch({ negativePhrases: phraseList(event.target.value) }); }} /><TextInput id="knowledge-systems" labelText="系统标识（逗号分隔）" value={systemText} onChange={(event) => { setSystemText(event.target.value); patch({ systemKeys: phraseList(event.target.value) }); }} /></div></section><WorkflowEditor key={draft.id} definition={draft.workflowDefinition} onChange={(workflowDefinition) => patch({ workflowDefinition })} allowedTypes={allowed} /><section className="local-plan-form" id="plan"><header><p className="overline">PLAN BEFORE EXECUTION</p><h2>{newRunMode ? draft.name : "创建测试计划"}</h2><p>参数按工作流动态生成。创建计划只校验和保存，确认计划后才执行真实操作。</p></header><TextInput id="plan-ticket-id" labelText="当前工单ID *" inputMode="numeric" value={ticketId} onChange={(event) => setTicketId(event.target.value)} /><div className="typed-input-grid">{workflowInputs(draft.workflowDefinition).map((input) => <ValueInput key={input.key} id={`run-input-${input.key}`} label={input.label} type={input.type} description={input.description} required={input.required} value={values[input.key] || ""} onChange={(value) => setValues((current) => ({ ...current, [input.key]: value }))} />)}</div><Checkbox id="sync-tec01" labelText="将执行状态同步到 tec01" checked={syncTec01} onChange={(_, { checked }) => setSyncTec01(checked)} />{syncTec01 && <TextInput id="tec01-run-id" labelText="tec01 已创建的运行ID *" helperText="tec01须先准备好该运行；执行回调将同步节点状态、结果及人工交互请求。" value={tec01RunId} onChange={(event) => setTec01RunId(event.target.value)} />}<div className="panel-actions"><Button onClick={plan} disabled={busy || unsupported.length > 0}>{busy ? "正在校验" : "校验并保存测试计划"}</Button></div></section><details className="local-single-debug"><summary>仅调试一个节点</summary><p>执行真实 Handler。单步不会自动执行前置节点，请明确填写依赖节点的输入。</p><Select id="local-single-node" labelText="要执行的节点" value={selectedNode} onChange={(event) => { setSelectedNode(event.target.value); setSingleValues({}); setSingleResult(undefined); }}>{draft.workflowDefinition.nodes.map((node) => <SelectItem key={node.id} value={node.id} text={`${node.title} (${node.id})`} />)}</Select><div className="typed-input-grid">{node?.inputs.filter((input) => input.source.kind !== "LITERAL").map((input) => <ValueInput key={input.name} id={`single-${input.name}`} label={input.name} type={input.type} required={input.required !== false} description={input.description} value={singleValues[input.name] || ""} onChange={(value) => setSingleValues((current) => ({ ...current, [input.name]: value }))} />)}</div>{node?.type === "sql_read" && <form className="credential-input" onSubmit={(event) => event.preventDefault()}><TextInput id="single-key" type="password" labelText="AOPS_API_KEY（仅本次使用）" autoComplete="off" value={singleKey} onChange={(event) => setSingleKey(event.target.value)} /></form>}<Button onClick={() => debugOne()} disabled={busy || !node || (node.type === "sql_read" && (!ticketId || !singleKey))}>执行此节点</Button>{singleResult && <><Status value={String(singleResult.status)} /><pre>{pretty(singleResult.output || singleResult.diagnostic)}</pre>{singleResult.status === "WAITING_INPUT" && singleResult.interrupt != null && <HitlPanel interaction={singleResult.interrupt as Record<string, unknown>} busy={busy} onSubmit={debugOne} onCancel={() => debugOne({ action: "CANCEL" })} />}</>}</details></> : <section className="empty-state"><span>01</span><h2>建立本地知识</h2><p>手工编排流程，或将工单提取草稿保存到这里，再创建可暂停、继续的测试计划。</p><Button onClick={create}>手工新建经验</Button></section>}</div></div></main>;
}
