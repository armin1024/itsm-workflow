import { useEffect } from "react";
import { Button, InlineNotification } from "@carbon/react";
import { Link } from "react-router-dom";
import type { LocalKnowledge, LocalRun } from "./browserStore";
import { ListPagination, ListToolbar } from "./ListControls";
import { useListQuery } from "./useListQuery";
import { Status } from "./ui";

const supported = new Set(["sql_read", "condition", "hitl_form", "end"]);
const time = (value: string) => new Date(value).toLocaleString("zh-CN", { hour12: false });
export default function LocalLists({ mode, knowledge, runs, selecting = false, error, onCreate, onImport }: { mode: "knowledge" | "runs"; knowledge: LocalKnowledge[]; runs: LocalRun[]; selecting?: boolean; error: string; onCreate: () => void; onImport: () => void }) {
  const query = useListQuery(mode === "runs" ? "statusGroup" : "status", "");
  const exact = query.exactFilters;
  const filtered = (mode === "runs" ? runs : knowledge).filter((item) => {
    const haystack = `${item.id} ${item.name} ${item.ticketId || ""} ${item.kind === "knowledge" ? `${item.summary} ${item.matchPhrases.join(" ")} ${item.systemKeys.join(" ")}` : item.currentNodeId}`;
    if (query.keyword && !haystack.toLowerCase().includes(query.keyword.toLowerCase())) return false;
    if (exact.ticketId && String(item.ticketId || "") !== exact.ticketId.trim()) return false;
    if (mode === "knowledge") {
      const entry = item as LocalKnowledge, valid = entry.workflowDefinition.nodes.every((node) => supported.has(node.type));
      if (query.status === "DRAFT" && !valid || query.status === "UNSUPPORTED" && valid) return false;
      if (exact.knowledgeId && entry.id !== exact.knowledgeId.trim()) return false;
      if (exact.nameExact && entry.name !== exact.nameExact.trim()) return false;
      if (exact.sourceType && entry.source !== exact.sourceType.trim()) return false;
    } else {
      const run = item as LocalRun;
      if (query.status === "ACTIVE" && !["RUNNING", "QUEUED", "DISPATCHING", "PAUSE_REQUESTED", "CANCEL_REQUESTED"].includes(run.status)) return false;
      if (query.status === "WAITING" && !["WAITING_INPUT", "PAUSED", "PLANNED", "UNKNOWN"].includes(run.status)) return false;
      if (query.status === "SUCCEEDED" && run.status !== "SUCCEEDED") return false;
      if (query.status === "FAILED" && !["FAILED", "TERMINATED", "CANCELLED"].includes(run.status)) return false;
      if (exact.runId && run.id !== exact.runId.trim()) return false;
      if (exact.knowledgeId && run.knowledgeId !== exact.knowledgeId.trim()) return false;
      if (exact.currentNodeId && run.currentNodeId !== exact.currentNodeId.trim()) return false;
    }
    return true;
  });
  const total = filtered.length, totalPages = Math.ceil(total / query.pageSize), page = Math.max(1, Math.min(query.page, totalPages || 1)), items = filtered.slice((page - 1) * query.pageSize, page * query.pageSize);
  useEffect(() => { if (page !== query.page) query.setPage(page); }, [page, query.page]);
  const hasQuery = Boolean(query.keyword || query.status || Object.values(exact).some(Boolean));
  return <main className="page"><header className="page-title"><div><p className="overline">{mode === "runs" ? "RUN CONTROL" : "KNOWLEDGE / WORKFLOW"}</p><h1>{selecting ? "创建运行" : mode === "runs" ? "执行中心" : "知识与版本"}</h1><p>{selecting ? "选择一条本地经验，填写参数并生成执行计划。" : mode === "runs" ? "查看当前节点、等待原因和真实执行结果。计划与结果保存在当前浏览器。" : "管理本地经验草稿与流程定义，正式审核和发布由tec01管理。"}</p></div><div className="header-actions">{mode === "runs" ? <Button as={Link} to="/runs/new">创建运行</Button> : !selecting && <><Button kind="tertiary" onClick={onImport}>导入JSON</Button><Button kind="secondary" as={Link} to="/compiler">从工单提取</Button><Button onClick={onCreate}>手工新建</Button></>}</div></header><ListToolbar keyword={query.draftKeyword} placeholder={mode === "runs" ? "工单ID / 运行ID / 经验名称" : "知识ID / 名称 / 摘要 / 匹配短语"} status={query.status} statusOptions={mode === "runs" ? [{ value: "", label: "全部状态" }, { value: "ACTIVE", label: "执行中" }, { value: "WAITING", label: "等待处理" }, { value: "SUCCEEDED", label: "已完成" }, { value: "FAILED", label: "失败或取消" }] : [{ value: "", label: "全部状态" }, { value: "DRAFT", label: "本地草稿" }, { value: "UNSUPPORTED", label: "含已停用节点" }]} loading={false} lastUpdated={new Date()} onKeywordChange={query.setDraftKeyword} onKeywordCommit={query.commitKeyword} onStatusChange={query.setStatus} onRefresh={() => window.dispatchEvent(new Event("itsm-studio-browser-data"))} onClear={query.clear} exactValues={exact} onExactChange={query.setExactFilter} exactFields={mode === "runs" ? [{ key: "runId", label: "运行ID" }, { key: "knowledgeId", label: "知识ID" }, { key: "ticketId", label: "工单ID" }, { key: "currentNodeId", label: "当前节点ID" }] : [{ key: "knowledgeId", label: "知识ID" }, { key: "nameExact", label: "完整名称" }, { key: "ticketId", label: "来源工单ID" }, { key: "sourceType", label: "来源类型", placeholder: "MANUAL / EXTRACTION" }]} />{error && <InlineNotification kind="error" title="本地数据操作失败" subtitle={error} hideCloseButton />}{!items.length ? <section className="empty-state"><span>00</span><h2>{hasQuery ? "当前条件没有匹配记录" : mode === "runs" ? "还没有运行记录" : "还没有本地知识"}</h2><p>{hasQuery ? "调整关键词或状态，或者清空查询条件。" : mode === "runs" ? "选择一条本地知识创建计划，确认后执行。" : "从工单提取草稿，或手工编排工作流并保存到浏览器。"}</p>{hasQuery ? <Button onClick={query.clear}>清空查询条件</Button> : mode === "runs" ? <Button as={Link} to="/runs/new">创建第一条运行</Button> : <Button onClick={onCreate}>手工新建经验</Button>}</section> : <>{mode === "runs" ? <section className="run-table local-run-table" aria-label="运行记录"><div className="run-row header"><span>状态</span><span>工单 / 运行</span><span>经验名称</span><span>当前节点</span><span>进度</span><span>创建时间</span></div>{(items as LocalRun[]).map((run) => <Link className="run-row" to={`/runs?run=${run.id}`} key={run.id}><Status value={run.status} label={run.status === "PLANNED" && typeof run.plan?.planMaterialHash === "string" && run.confirmation?.planHash === run.plan.planMaterialHash ? "已确认，待执行" : undefined} /><span><strong>#{run.ticketId}</strong><small title={run.id}>{run.id}</small></span><span>{run.name}</span><span>{run.currentNodeId || "尚未开始"}</span><span>{Object.values(run.nodeStates).filter((status) => ["SUCCEEDED", "SKIPPED", "FAILED", "TERMINATED", "CANCELLED"].includes(status)).length}/{run.workflowSnapshot.nodes.length}</span><time>{time(run.createdAt)}</time></Link>)}</section> : <section className="knowledge-grid local-knowledge-grid">{(items as LocalKnowledge[]).map((item) => { const valid = item.workflowDefinition.nodes.every((node) => supported.has(node.type)); return <article key={item.id}><div><Status value={valid ? "DRAFT" : "UNSUPPORTED"} /><span>{item.source === "MANUAL" ? "手工新建" : "工单提取"}</span></div><Link className="knowledge-link" to={`${selecting ? "/runs/new" : "/knowledge"}?knowledge=${item.id}`}><h2>{item.name}</h2><p>{item.summary || "尚未填写摘要"}</p></Link><dl className="local-card-facts"><div><dt>节点</dt><dd>{item.workflowDefinition.nodes.length}</dd></div><div><dt>更新</dt><dd>{time(item.updatedAt)}</dd></div></dl><footer><code title={item.id}>{item.id}</code><Link to={`${selecting ? "/runs/new" : "/knowledge"}?knowledge=${item.id}`}>{selecting ? "创建计划 →" : "查看与编辑 →"}</Link></footer></article>; })}</section>}<ListPagination page={page} pageSize={query.pageSize} total={total} totalPages={totalPages} loading={false} onPageChange={query.setPage} onPageSizeChange={query.setPageSize} /></>}<p className="local-storage-footnote">数据保存在当前浏览器的IndexedDB。清理站点数据会删除本地记录；AOPS_API_KEY仅在执行时临时提供。</p></main>;
}
