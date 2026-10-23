import { useEffect, useState } from "react";
import { Button, Checkbox, InlineNotification, TextInput } from "@carbon/react";
import { ApiError, api } from "./api";
import { browserStore, downloadJson, localId, now, type LocalRun } from "./browserStore";
import HitlPanel from "./HitlPanel";
import RunCanvas from "./RunCanvas";
import { applyRunEvent } from "./LocalRuntimeMonitor";
import { pretty, Status } from "./ui";
import { needsExecutionReview } from "./runGuards";

const active = new Set(["RUNNING", "QUEUED", "DISPATCHING", "PAUSE_REQUESTED", "CANCEL_REQUESTED"]);
export default function LocalRunPanel({ run }: { run: LocalRun }) {
  const [key, setKey] = useState(""), [confirmed, setConfirmed] = useState(false), [busy, setBusy] = useState(false), [error, setError] = useState(""), [uncertainConfirmed, setUncertainConfirmed] = useState(false);
  useEffect(() => { setKey(""); setConfirmed(false); setError(""); setUncertainConfirmed(false); }, [run.id]);
  const hasSql = run.workflowSnapshot.nodes.some((node) => node.type === "sql_read");
  const uncertain = needsExecutionReview(run.status, run.nodeStates);
  const planHash = typeof run.plan?.planMaterialHash === "string" ? run.plan.planMaterialHash : "";
  const approved = !!planHash && run.confirmation?.planHash === planHash;
  const approve = async () => {
    setBusy(true); setError("");
    try {
      if (!confirmed) throw new Error("请先勾选已核对完整计划");
      if (!planHash) throw new Error("此旧计划缺少确认摘要，请从知识重新创建计划");
      const checked = await api.planWorkflow({ workflowDefinition: run.workflowSnapshot, ticketId: run.ticketId, inputs: run.runInputs, preserveCatalog: true });
      if (checked.valid !== true) throw new Error("运行参数不完整，请重新创建计划并补齐参数");
      if (checked.planMaterialHash !== planHash || checked.workflowContentHash !== run.workflowContentHash) throw new Error("计划内容或参数已变化，请重新创建并核对计划");
      const current = await browserStore.get<LocalRun>(run.id);
      if (!current || current.workflowContentHash !== run.workflowContentHash || current.plan?.planMaterialHash !== planHash) throw new Error("本地计划已变化，请刷新后重新核对");
      await browserStore.put({ ...current, confirmation: { planHash, confirmedAt: now() }, updatedAt: now() });
      setConfirmed(false);
    } catch (error) { setError((error as Error).message); }
    finally { setBusy(false); }
  };
  const dispatch = async (resume?: Record<string, unknown>) => {
    setBusy(true); setError("");
    try {
      if (!approved) throw new Error("请先确认完整执行计划，确认计划不需要AOPS_API_KEY");
      if (hasSql && !key.trim()) throw new Error("计划已确认，请提供本次执行的 AOPS_API_KEY");
      const observed = await api.activity(run.syncTec01 ? run.tec01RunId || run.id : run.id);
      if (observed.active) throw new Error("旧执行仍在活动中，请先暂停或取消并确认结束，不能并发恢复同一运行");
      if (uncertain && !uncertainConfirmed) throw new Error("请先核对上次执行是否到达 AOPS，再确认重新执行不确定节点");
      const prepared: LocalRun = { ...run, dispatchId: localId("dispatch"), status: "DISPATCHING", nodeStates: Object.fromEntries(Object.entries(run.nodeStates).map(([id, status]) => [id, ["UNKNOWN", "RUNNING", "FAILED", "PAUSED"].includes(status) ? "READY" : status])), lastEventId: 0, interaction: null, diagnostic: undefined, updatedAt: now() };
      await browserStore.put(prepared);
      try {
        await api.dispatch({ dispatchId: prepared.dispatchId, runId: run.syncTec01 ? run.tec01RunId : run.id, ticketId: run.ticketId, workflowSnapshot: run.workflowSnapshot, workflowContentHash: run.workflowContentHash, runInputs: run.runInputs, nodeStates: prepared.nodeStates, nodeOutputs: run.nodeOutputs, nodeResults: run.nodeResults || {}, currentNodeId: run.currentNodeId, selectedRoutes: run.selectedRoutes, resumePayload: resume ? { nodeId: run.currentNodeId, ...resume } : null, syncTec01: run.syncTec01, planMaterialHash: planHash }, key || undefined);
        setKey(""); setConfirmed(false);
      } catch (error) {
        // A definitive rejection did not start work. A lost response may already have started it.
        const definitive = error instanceof ApiError && error.status >= 400 && error.status < 500;
        const current = await browserStore.get<LocalRun>(run.id);
        if (current && current.dispatchId === prepared.dispatchId) await browserStore.put({ ...(definitive ? run : current), status: definitive ? run.status : "UNKNOWN", diagnostic: { message: (error as Error).message }, updatedAt: now() });
        throw error;
      }
    } catch (error) { setError((error as Error).message); }
    finally { setBusy(false); }
  };
  const pause = async () => {
    setBusy(true); setError("");
    try {
      const response = await api.command(run.syncTec01 ? run.tec01RunId || run.id : run.id, "PAUSE");
      const current = await browserStore.get<LocalRun>(run.id);
      if (response.accepted === false) {
        if (current) {
          try {
            const result = await api.studioEvents(run.syncTec01 ? run.tec01RunId || run.id : run.id, current.lastEventId);
            let latest = current; for (const event of result.events) latest = applyRunEvent(latest, event);
            if (active.has(latest.status) && !result.active) latest = { ...latest, status: "UNKNOWN", diagnostic: { message: "执行已结束，请核对最后节点结果。" } };
            await browserStore.put(latest);
          } catch (error) {
            if (error instanceof ApiError && error.status === 404) await browserStore.put({ ...current, status: "UNKNOWN", nodeStates: Object.fromEntries(Object.entries(current.nodeStates).map(([id, status]) => [id, status === "RUNNING" ? "UNKNOWN" : status])), diagnostic: { message: "执行服务中无活动任务。请核对上次结果后继续。" }, updatedAt: now() });
            else throw error;
          }
        }
        return;
      }
      if (current && active.has(current.status)) await browserStore.put({ ...current, status: "PAUSE_REQUESTED", updatedAt: now() });
    } catch (error) { setError((error as Error).message); }
    finally { setBusy(false); }
  };
  const cancel = async () => {
    setBusy(true); setError("");
    try {
      if (active.has(run.status)) {
        const current = await browserStore.get<LocalRun>(run.id);
        if (current) await browserStore.put({ ...current, status: "CANCEL_REQUESTED", removeAfterCancel: true, updatedAt: now() });
        const response = await api.command(run.syncTec01 ? run.tec01RunId || run.id : run.id, "CANCEL");
        if (response.accepted === false) await browserStore.remove(run.id);
      } else await browserStore.remove(run.id);
    } catch (error) {
      const current = await browserStore.get<LocalRun>(run.id);
      if (current && current.status === "CANCEL_REQUESTED") await browserStore.put({ ...current, status: run.status, removeAfterCancel: false, updatedAt: now() });
      setError((error as Error).message);
    }
    finally { setBusy(false); }
  };
  const completed = Object.values(run.nodeStates).filter((status) => ["SUCCEEDED", "SKIPPED", "FAILED", "TERMINATED", "CANCELLED"].includes(status)).length;
  const canResume = ["PLANNED", "PAUSED", "FAILED", "UNKNOWN"].includes(run.status);
  const planNodes = ((run.plan?.renderedPlan as Record<string, unknown>)?.nodes || []) as Record<string, unknown>[];
  const fallbackNodes: Record<string, unknown>[] = run.workflowSnapshot.nodes.map((node) => ({ nodeId: node.id, type: node.type, title: node.title, databaseRef: node.config.databaseRef, renderedSql: node.config.sqlTemplate, inputs: node.inputs }));
  const blocked = !approved ? "请先核对并确认执行计划（此步骤无需AOPS_API_KEY）" : hasSql && !key.trim() ? "计划已确认；填写本次AOPS_API_KEY后才能执行" : uncertain && !uncertainConfirmed ? "请先核对并确认上次结果不确定的节点" : "";
  return (
    <section className="local-run-detail">
      <header className="local-detail-heading">
        <div><p className="overline">BROWSER SAVED RUN</p><h2>{run.name}</h2><p>工单 {run.ticketId} · {run.syncTec01 ? `同步 tec01：${run.tec01RunId}` : "仅本地调试"}</p></div>
        <div className="header-actions">
          <Status value={run.status} label={run.status === "PLANNED" && approved ? "已确认，待执行" : undefined} />
          <Button kind="ghost" size="sm" onClick={() => downloadJson(`${run.id}.json`, { schema: "itsm-studio-run", schemaVersion: 1, run })}>导出计划与结果</Button>
          <Button kind="danger--ghost" size="sm" disabled={busy || run.status === "CANCEL_REQUESTED"} onClick={cancel}>{active.has(run.status) ? "取消并移除" : "移除计划"}</Button>
        </div>
      </header>
      {error && <InlineNotification kind="error" title="操作未完成" subtitle={error} hideCloseButton />}
      <div className="local-progress">
        <strong>{completed}<small> / {run.workflowSnapshot.nodes.length}</small></strong><span>已处理节点</span><span>当前：{run.currentNodeId || "尚未开始"}</span>
        {active.has(run.status) && <Button size="sm" kind="secondary" disabled={busy || run.status !== "RUNNING"} onClick={pause}>{run.status === "PAUSE_REQUESTED" ? "等待当前节点结束后暂停" : "暂停"}</Button>}
      </div>
      <RunCanvas run={run} />
      {run.diagnostic != null && <details className="local-diagnostic" open={run.status === "UNKNOWN" || run.status === "FAILED"}><summary>运行诊断</summary><pre>{pretty(run.diagnostic)}</pre></details>}
      {(canResume || run.status === "WAITING_INPUT") && (
        <section className="local-execution-controls">
          <header>
            <h3>{run.status === "PLANNED" ? "确认计划，再提供凭据执行" : "核对计划与当前状态后继续"}</h3>
            <p>计划确认不需要AOPS_API_KEY，也不会执行SQL；执行凭据仅在下发时使用，不保存。</p>
          </header>
          <details className="rendered-plan" open={!approved || run.status === "PLANNED"}>
            <summary>完整执行计划与参数</summary>
            <dl className="proposal-summary">
              <div><dt>工单注释</dt><dd><code>#uatu-{run.ticketId}</code></dd></div>
              <div><dt>运行参数</dt><dd><pre>{pretty(run.runInputs)}</pre></dd></div>
            </dl>
            {(planNodes.length ? planNodes : fallbackNodes).map((node, index) => (
              <article key={String(node.nodeId)}>
                <header><b>{index + 1}. {String(node.title || node.nodeId)}</b><code>{String(node.type || "")}</code></header>
                {node.databaseRef != null && <p>数据库：<code>{String(node.databaseRef)}</code></p>}
                {(node.renderedSql ?? node.sqlTemplate) != null && <pre>{String(node.renderedSql ?? node.sqlTemplate)}</pre>}
                {node.deferred === true && <p className="muted">运行到本节点时，根据前置节点结果或人工输入补齐参数。</p>}
                <details><summary>参数来源</summary><pre>{pretty(node.inputs)}</pre></details>
              </article>
            ))}
          </details>
          {approved ? (
            <InlineNotification kind="success" title="执行计划已确认" subtitle={`确认时间：${new Date(run.confirmation!.confirmedAt).toLocaleString("zh-CN")}。确认与执行分开；未填写凭据时不会执行SQL。`} hideCloseButton />
          ) : (
            <div className="plan-confirmation-actions">
              <Checkbox id="confirm-local-plan" checked={confirmed} labelText={`我已核对工单 ${run.ticketId} 的完整步骤、数据库和参数`} onChange={(event) => setConfirmed(event.target.checked)} />
              <Button kind="secondary" disabled={busy || !confirmed} onClick={approve}>{busy ? "正在核对" : "确认此执行计划"}</Button>
            </div>
          )}
          {hasSql && (
            <form className="credential-input" onSubmit={(event) => event.preventDefault()}>
              <TextInput id="run-aops-key" type="password" labelText="AOPS_API_KEY（仅本次请求使用，不保存）" helperText="此凭据只用于执行SQL，不影响上方计划确认。" autoComplete="off" value={key} onChange={(event) => setKey(event.target.value)} />
            </form>
          )}
          {uncertain && <>
            <InlineNotification kind="warning" title="上次执行结果不确定" subtitle="请先核对AOPS审计和已保存记录，确认旧执行已结束，再决定是否重执行当前节点。" hideCloseButton />
            <Checkbox id="retry-uncertain" labelText="已核对旧执行和审计，确认重新执行不确定节点" checked={uncertainConfirmed} onChange={(event) => setUncertainConfirmed(event.target.checked)} />
          </>}
          {blocked && <p className="muted" role="status">{blocked}</p>}
          {canResume && <div className="panel-actions">
            <Button disabled={busy || !!blocked} onClick={() => dispatch()}>{busy ? "正在下发" : run.status === "PLANNED" ? "执行已确认计划" : run.status === "FAILED" || run.status === "UNKNOWN" ? "确认重试未完成节点" : "继续执行"}</Button>
          </div>}
          {run.status === "WAITING_INPUT" && run.interaction && <HitlPanel interaction={run.interaction} busy={busy || !!blocked} onSubmit={(value) => dispatch(value)} onCancel={cancel} />}
        </section>
      )}
      <section className="local-event-list"><h3>节点事件</h3>
        {run.events.length ? <ol>{run.events.slice(-50).map((event, index) => <li key={`${String(event.eventId)}-${index}`}><time>{new Date(String(event.occurredAt || run.updatedAt)).toLocaleTimeString("zh-CN")}</time><code>{String(event.nodeId || "运行")}</code><span>{String(event.safeSummary || event.reason || event.status || event.type)}</span></li>)}</ol> : <p className="muted">计划尚未执行。</p>}
      </section>
    </section>
  );
}
