import { memo, useEffect, useMemo, useState } from "react";
import { Button, InlineLoading, InlineNotification, Select, SelectItem } from "@carbon/react";
import { browserStore } from "./browserStore";

/** Bounded visual summary only. Stored/exported data is never truncated. */
export function previewValue(value: unknown, depth = 0): unknown {
  if (typeof value === "string") return value.length > 400 ? value.slice(0, 400) + "…（长字段预览）" : value;
  if (!value || typeof value !== "object") return value;
  if (depth >= 3) return Array.isArray(value) ? `[数组，共${value.length}项]` : "[对象，展开完整导出查看]";
  if (Array.isArray(value)) return [...value.slice(0, 10).map(item => previewValue(item, depth + 1)), ...(value.length > 10 ? [`另有${value.length - 10}项`] : [])];
  const result: Record<string, unknown> = {}; let count = 0;
  for (const key in value) {
    if (!Object.hasOwn(value, key)) continue;
    if (count++ >= 20) { result["其余字段"] = "请查看完整导出"; break; }
    result[key] = previewValue((value as Record<string, unknown>)[key], depth + 1);
  }
  return result;
}
const cell = (value: unknown) => typeof value === "string" ? String(previewValue(value)) : JSON.stringify(previewValue(value)) ?? "";
export const ResultPreview = memo(function ResultPreview({ value }: { value: unknown }) {
  const [page, setPage] = useState(1), [size, setSize] = useState(100);
  useEffect(() => { setPage(1); }, [value]);
  const rows = Array.isArray(value) ? value : value && typeof value === "object" && Array.isArray((value as Record<string, unknown>).data) ? (value as { data: unknown[] }).data : undefined;
  const currentPage = Math.max(1, Math.min(page, Math.ceil((rows?.length || 0) / size) || 1));
  const visible = useMemo(() => rows?.slice((currentPage - 1) * size, currentPage * size), [rows, currentPage, size]);
  const columns = useMemo(() => {
    const names = new Set<string>();
    for (const row of visible?.slice(0, 5) || []) {
      if (row && typeof row === "object" && !Array.isArray(row)) for (const key in row) { if (Object.hasOwn(row, key)) names.add(key); if (names.size >= 20) break; }
      else names.add("值");
      if (names.size >= 20) break;
    }
    return [...names];
  }, [visible]);
  if (!rows) return <><p className="muted">仅显示有界结构预览，完整内容保留在原始结果中。</p><pre>{JSON.stringify(previewValue(value), null, 2)}</pre></>;
  return <div className="result-preview"><p>共{rows.length}行 · 第{currentPage}/{Math.ceil(rows.length / size) || 1}页。页面仅预览最多20列及长字段摘要，原始结果和完整导出不截断。</p>
    <div className="result-preview-controls"><Button kind="ghost" size="sm" disabled={currentPage <= 1} onClick={() => setPage(currentPage - 1)}>上一页</Button><Button kind="ghost" size="sm" disabled={currentPage * size >= rows.length} onClick={() => setPage(currentPage + 1)}>下一页</Button><Select id="result-preview-size" labelText="每页行数" size="sm" value={size} onChange={event => { setSize(Number(event.target.value)); setPage(1); }}><SelectItem value={100} text="100行" /><SelectItem value={200} text="200行" /></Select></div>
    <div className="result-preview-scroll"><table><thead><tr>{columns.map(name => <th key={name}>{name}</th>)}</tr></thead><tbody>{visible?.map((row, index) => <tr key={(currentPage - 1) * size + index}>{columns.map(name => <td key={name}>{cell(row && typeof row === "object" && !Array.isArray(row) ? (row as Record<string, unknown>)[name] : row)}</td>)}</tr>)}</tbody></table></div>
  </div>;
});

export const NodeOutputPreview = memo(function NodeOutputPreview({ runId, nodeId, revision, available, cleared }: { runId: string; nodeId: string; revision: string; available: boolean; cleared?: boolean }) {
  const [value, setValue] = useState<unknown>(), [error, setError] = useState(""), [loading, setLoading] = useState(false);
  useEffect(() => {
    let live = true; setValue(undefined); setError("");
    if (!available) { setLoading(false); return; }
    setLoading(true);
    void browserStore.output(runId, nodeId).then(result => { if (live) setValue(result); }).catch(value => { if (live) setError(value.message); }).finally(() => { if (live) setLoading(false); });
    return () => { live = false; };
  }, [runId, nodeId, revision, available]);
  if (loading) return <InlineLoading description="按需读取本节点完整结果" />;
  if (error) return <InlineNotification kind="error" title="无法读取节点结果" subtitle={error} hideCloseButton />;
  if (value === undefined) return <p>{cleared ? "该运行结果已手动清理，计划与状态保留；不可使用已清理的结果恢复。" : "尚未产生已保存结果"}</p>;
  return <ResultPreview value={value} />;
});

export const ContextPreview = memo(function ContextPreview({ value }: { value: unknown }) {
  const [expanded, setExpanded] = useState(false), [selected, setSelected] = useState("");
  const fields = useMemo(() => value && typeof value === "object" && !Array.isArray(value) ? Object.keys(value).slice(0, 100) : [], [value]);
  const key = fields.includes(selected) ? selected : fields[0];
  return <details onToggle={event => setExpanded(event.currentTarget.open)}><summary>查看前置节点上下文</summary>{expanded && <>
    {fields.length > 0 && <Select id="hitl-context-field" labelText="上下文字段" value={key} onChange={event => setSelected(event.target.value)}>{fields.map(field => <SelectItem key={field} value={field} text={field} />)}</Select>}
    <ResultPreview value={key ? (value as Record<string, unknown>)[key] : value} />
  </>}</details>;
});
