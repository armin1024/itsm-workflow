import { useEffect, useRef, useState } from "react";
import { Button, InlineNotification } from "@carbon/react";
import { ApiError, api } from "./api";
import { BrowserStorageError, browserStore, now, type LocalExtraction, type LocalRun, type LocalSummary } from "./browserStore";
import { POLLED_STATUSES } from "./localDatabase";
import { applyRunEvent, mapWithConcurrency, pollDelay } from "./runEvents";
export { applyRunEvent } from "./runEvents";

export default function LocalRuntimeMonitor() {
  const [error, setError] = useState(""), [paused, setPaused] = useState<Record<string, string>>({});
  const pausedRef = useRef(new Map<string, string>()), restartRef = useRef<() => void>(() => {});
  useEffect(() => {
    let stopped = false, running = false, timer: number | undefined;
    const schedule = (delay: number) => { if (stopped) return; clearTimeout(timer); timer = window.setTimeout(() => void poll(), delay); };
    const sync = async (item: LocalSummary) => {
      if (pausedRef.current.has(item.id) || stopped) return;
      if (item.kind === "run" && item.status === "DISPATCHING" && Date.now() - Date.parse(item.updatedAt) < 3000) return;
      try {
        const response = await api.studioEvents(item.kind === "run" && item.syncTec01 ? item.tec01RunId || item.id : item.id, item.lastEventId);
        if (stopped || !response.events.length && response.syncStatus === undefined) return;
        const latest = await browserStore.update<LocalRun | LocalExtraction>(item.id, current => {
          let changed = false, next = current;
          if (response.historyGap && next.kind === "run" && !next.eventHistoryIncomplete) { next = { ...next, eventHistoryIncomplete: true }; changed = true; }
          if (response.syncStatus !== undefined && (next.syncStatus !== response.syncStatus || JSON.stringify(next.callbackError) !== JSON.stringify(response.callbackError))) {
            next = { ...next, syncStatus: response.syncStatus, callbackError: response.callbackError }; changed = true;
          }
          for (const event of response.events) {
            if (Number(event.eventId) <= next.lastEventId) continue;
            if (next.kind === "run" && event.dispatchId && next.dispatchId && event.dispatchId !== next.dispatchId) continue;
            changed = true;
            if (next.kind === "run") next = applyRunEvent(next, event);
            else {
              const { proposal: _proposal, validation: _validation, diagnostics: _diagnostics, ...safeEvent } = event;
              next = { ...next, lastEventId: Number(event.eventId), updatedAt: now(), events: [...next.events, safeEvent].slice(-200) };
              if (event.type === "FINISHED") Object.assign(next, { status: String(event.status), proposal: event.proposal, validation: event.validation, diagnostics: event.diagnostics, extractionOutcome: event.extractionOutcome, publishable: event.publishable, error: event.error });
            }
          }
          return changed ? next : undefined;
        });
        if (latest?.kind === "run" && latest.removeAfterCancel && !POLLED_STATUSES.has(latest.status)) await browserStore.remove(latest.id);
      } catch (value) {
        if (value instanceof BrowserStorageError) {
          pausedRef.current.set(item.id, value.message); setPaused(Object.fromEntries(pausedRef.current));
        } else if (value instanceof ApiError && value.status === 404) {
          try {
            const latest = await browserStore.metadata<LocalRun | LocalExtraction>(item.id);
            if (!latest) return;
            if (latest.kind === "run" && latest.removeAfterCancel) await browserStore.remove(latest.id);
            else if (latest.kind === "run") await browserStore.put({ ...latest, status: "UNKNOWN", nodeStates: Object.fromEntries(Object.entries(latest.nodeStates).map(([id, status]) => [id, status === "RUNNING" ? "UNKNOWN" : status])), diagnostic: { message: "执行服务已重启或临时缓存已过期。已保存事实仍保留；未保存结果需核实，系统不会自动重放SQL。" }, updatedAt: now() });
            else await browserStore.put({ ...latest, status: "FAILED", error: { message: "提取服务已重启或临时结果已过期，请核对已有记录后再决定重新提交。" }, updatedAt: now() });
          } catch (storageError) { pausedRef.current.set(item.id, (storageError as Error).message); setPaused(Object.fromEntries(pausedRef.current)); }
        } else if (value instanceof ApiError && value.status === 401) {
          setError("Studio会话已过期，请刷新页面重新登录。浏览器知识和计划仍保留。");
          pausedRef.current.set(item.id, "会话已过期，重新登录后恢复同步"); setPaused(Object.fromEntries(pausedRef.current));
        } else setError((value as Error).message);
      }
    };
    const poll = async () => {
      if (running || stopped) return;
      running = true; let hasTasks = false;
      try {
        const items = await browserStore.active();
        const tasks = items.filter(item => !pausedRef.current.has(item.id)); hasTasks = tasks.length > 0;
        await mapWithConcurrency(tasks, 4, sync);
      } catch (value) { setError((value as Error).message); }
      finally { running = false; if (hasTasks && !stopped) schedule(pollDelay(document.hidden)); }
    };
    restartRef.current = () => { pausedRef.current.clear(); setPaused({}); setError(""); if (!running) schedule(0); };
    const stopSubscription = browserStore.subscribe(() => { if (!running) schedule(0); });
    const visibility = () => { if (!running) schedule(document.hidden ? 5000 : 0); };
    document.addEventListener("visibilitychange", visibility); schedule(0);
    return () => { stopped = true; clearTimeout(timer); stopSubscription(); document.removeEventListener("visibilitychange", visibility); };
  }, []);
  return error || Object.keys(paused).length ? <div className="local-storage-alert">
    {error && <InlineNotification kind="error" title="本地调试同步" subtitle={error} onCloseButtonClick={() => setError("")} />}
    {Object.keys(paused).length > 0 && <><InlineNotification kind="error" title="本地写入未完成，已暂停自动同步" subtitle={`${Object.keys(paused).length}条任务暂停：${Object.values(paused)[0]} 服务端临时结果有期限，过期后需核实。`} hideCloseButton /><Button kind="tertiary" size="sm" onClick={() => restartRef.current()}>已检查存储，恢复结果同步</Button></>}
  </div> : null;
}
