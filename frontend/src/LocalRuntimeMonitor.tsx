import { useEffect, useState } from "react";
import { InlineNotification } from "@carbon/react";
import { ApiError, api } from "./api";
import { browserStore, now, type LocalExtraction, type LocalRun } from "./browserStore";

const activeStates = new Set(["QUEUED", "RUNNING", "PAUSE_REQUESTED", "CANCEL_REQUESTED", "DISPATCHING"]);
export function applyRunEvent(run: LocalRun, event: Record<string, unknown>): LocalRun {
  const snapshot = event.snapshot as Record<string, unknown> | undefined;
  const { snapshot: _snapshot, result: _result, interaction: _interaction, ...safeEvent } = event;
  const next = { ...run, lastEventId: Number(event.eventId), updatedAt: now(), events: [...run.events, safeEvent].slice(-300) };
  if (snapshot) {
    const fields = ["status", "nodeStates", "nodeOutputs", "selectedRoutes", "currentNodeId", "interaction", "diagnostic", "nodeResults"] as const;
    for (const field of fields) if (field in snapshot) Object.assign(next, { [field]: snapshot[field] });
    if (snapshot.nodeStatuses) next.nodeStates = snapshot.nodeStatuses as Record<string, string>;
  } else if (event.type === "NODE_STARTED") {
    next.status = "RUNNING"; next.currentNodeId = String(event.nodeId);
    next.nodeStates = { ...run.nodeStates, [String(event.nodeId)]: "RUNNING" };
  } else if (event.type === "NODE_COMPLETED") {
    const nodeId = String(event.nodeId);
    next.nodeStates = { ...run.nodeStates, [nodeId]: String(event.status) };
    if (event.result != null) next.nodeOutputs = { ...run.nodeOutputs, [nodeId]: event.result };
    if (event.interaction) next.interaction = event.interaction as Record<string, unknown>;
    if (event.diagnostic) next.diagnostic = event.diagnostic;
    if (event.selectedEdgeId) next.selectedRoutes = { ...run.selectedRoutes, [nodeId]: String(event.selectedEdgeId) };
    for (const id of event.skippedNodeIds as string[] || []) next.nodeStates[id] = "SKIPPED";
  } else if (event.type === "RUN_RELEASED") {
    next.status = String(event.status); next.currentNodeId = String(event.currentNodeId || run.currentNodeId);
  }
  return next;
}

export default function LocalRuntimeMonitor() {
  const [error, setError] = useState("");
  useEffect(() => {
    let stopped = false, running = false;
    const poll = async () => {
      if (running || stopped) return;
      running = true;
      try {
        const items = await browserStore.all();
        const tasks = items.filter((item) => (item.kind === "run" && activeStates.has(item.status)) || (item.kind === "extraction" && (item.status === "EXTRACTING" || (item.syncTec01 && item.syncStatus === "PENDING")))) as Array<LocalRun | LocalExtraction>;
        await Promise.all(tasks.map(async (item) => {
          if (item.kind === "run" && item.status === "DISPATCHING" && Date.now() - Date.parse(item.updatedAt) < 3000) return;
          try {
            const response = await api.studioEvents(item.kind === "run" && item.syncTec01 ? item.tec01RunId || item.id : item.id, item.lastEventId);
            if (stopped) return;
            let latest = await browserStore.get<typeof item>(item.id);
            if (!latest) return;
            const syncChanged = response.syncStatus !== undefined && (latest.syncStatus !== response.syncStatus || JSON.stringify(latest.callbackError) !== JSON.stringify(response.callbackError));
            if (response.syncStatus !== undefined) Object.assign(latest, { syncStatus: response.syncStatus, callbackError: response.callbackError });
            for (const event of response.events) {
              if (Number(event.eventId) <= latest.lastEventId) continue;
              if (latest.kind === "run" && event.dispatchId && latest.dispatchId && event.dispatchId !== latest.dispatchId) continue;
              if (latest.kind === "run") latest = applyRunEvent(latest, event);
              else {
                latest = { ...latest, lastEventId: Number(event.eventId), updatedAt: now(), events: [...latest.events, event].slice(-200) };
                if (event.type === "FINISHED") Object.assign(latest, { status: String(event.status), proposal: event.proposal, validation: event.validation, diagnostics: event.diagnostics, error: event.error });
              }
            }
            if (response.events.length || syncChanged) {
              if (latest.kind === "run" && latest.removeAfterCancel && !activeStates.has(latest.status)) await browserStore.remove(latest.id);
              else await browserStore.put(latest);
            }
          } catch (error) {
            if (error instanceof ApiError && error.status === 404) {
              const latest = await browserStore.get<typeof item>(item.id);
              if (!latest) return;
              if (latest.kind === "run") {
                if (latest.removeAfterCancel) await browserStore.remove(latest.id);
                else await browserStore.put({ ...latest, status: "UNKNOWN", nodeStates: Object.fromEntries(Object.entries(latest.nodeStates).map(([id, status]) => [id, status === "RUNNING" ? "UNKNOWN" : status])), diagnostic: { message: "执行服务已重启或临时记录已释放。浏览器保留已完成节点；核对上次执行结果后再重试。" }, updatedAt: now() });
              } else await browserStore.put({ ...latest, status: "FAILED", error: { message: "提取服务已重启，请重新提交提取。" }, updatedAt: now() });
            } else if (error instanceof ApiError && error.status === 401) setError("Studio会话已过期，请刷新页面重新登录。浏览器中的知识和计划仍然保留。");
          }
        }));
      } catch (error) { setError((error as Error).message); }
      finally { running = false; }
    };
    void poll(); const timer = window.setInterval(poll, 1000);
    return () => { stopped = true; window.clearInterval(timer); };
  }, []);
  return error ? <div className="local-storage-alert"><InlineNotification kind="error" title="本地调试状态" subtitle={error} onCloseButtonClick={() => setError("")} /></div> : null;
}
