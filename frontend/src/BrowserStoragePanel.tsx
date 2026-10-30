import { useEffect, useState } from "react";
import { Button, Checkbox, InlineNotification } from "@carbon/react";
import { browserStore, downloadJson, type LocalSummary } from "./browserStore";

const mib = (size: number) => `${(size / 1024 / 1024).toFixed(1)} MiB`;
export default function BrowserStoragePanel() {
  const [status, setStatus] = useState<Awaited<ReturnType<typeof browserStore.status>>>(), [warning, setWarning] = useState("");
  const [open, setOpen] = useState(false), [runs, setRuns] = useState<LocalSummary[]>([]), [selected, setSelected] = useState<Record<string, boolean>>({});
  const [error, setError] = useState(""), [busy, setBusy] = useState(false);
  useEffect(() => {
    let live = true;
    const load = async () => {
      const immediate = browserStore.warning(); if (live) setWarning(immediate.warning);
      try {
        const value = await browserStore.status(); if (!live) return; setStatus(value); setWarning(value.warning);
        if (open) setRuns((await browserStore.summaries("run")).filter(run => run.resultBytes > 0 && ["SUCCEEDED", "TERMINATED", "CANCELLED"].includes(run.status)));
      } catch (value) { if (live) setError((value as Error).message); }
    };
    void load(); const stop = browserStore.subscribe(() => { void load(); });
    return () => { live = false; stop(); };
  }, [open]);
  const clear = async () => {
    const ids = runs.filter(run => selected[run.id]).map(run => run.id);
    if (!ids.length || !window.confirm(`确认清理所选${ids.length}条已结束运行的完整结果？知识、计划和状态保留，结果将不能再导出或用于恢复。`)) return;
    setBusy(true); setError("");
    try { for (const id of ids) await browserStore.clearResults(id); setSelected({}); }
    catch (value) { setError((value as Error).message); }
    finally { setBusy(false); }
  };
  return <section className="browser-storage-panel" aria-label="浏览器本地存储">
    {warning && <InlineNotification kind="warning" title="本地存储状态" subtitle={warning} hideCloseButton />}
    <details open={open} onToggle={event => setOpen(event.currentTarget.open)}><summary>浏览器结果存储 · {status ? `${mib(status.resultBytes)} / ${mib(status.budgetBytes)}软预算` : "正在检查"}</summary>
      <p>只统计完整节点输出；浏览器实际配额还包含知识、计划和其他站点数据。不会自动删除任何历史。超过软预算或浏览器配额时停止本地结果同步，不截断原始结果。以下仅允许手动清理已完成/已终止运行的输出，活动、等待、暂停、失败待恢复与结果未知的运行受保护。</p>
      {error && <InlineNotification kind="error" title="本地存储操作失败" subtitle={error} hideCloseButton />}
      <div className="browser-storage-actions"><Button kind="tertiary" size="sm" disabled={busy} onClick={async () => { setBusy(true); try { downloadJson("itsm-browser-backup.json", { schema: "itsm-browser-backup", schemaVersion: 2, items: await browserStore.exportAll() }); } catch (value) { setError((value as Error).message); } finally { setBusy(false); } }}>导出完整本地备份</Button><Button kind="danger--tertiary" size="sm" disabled={busy || status?.readOnly || !runs.some(run => selected[run.id])} onClick={clear}>清理所选已结束结果</Button></div>
      {runs.map(run => <div className="browser-storage-run" key={run.id}><Checkbox id={`clear-${run.id}`} labelText={`${run.name || run.id} · ${mib(run.resultBytes)}`} checked={!!selected[run.id]} disabled={busy || status?.readOnly} onChange={(_, { checked }) => setSelected(current => ({ ...current, [run.id]: checked }))} /></div>)}
      {!runs.length && <p>没有可清理的已结束运行结果。</p>}
    </details>
  </section>;
}
