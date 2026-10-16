import { useState } from "react";
import { Button, Select, SelectItem, TextInput } from "@carbon/react";

export type FilterOption = { value: string; label: string };
export type ExactFilter = { key: string; label: string; placeholder?: string; type?: "text" | "datetime-local" };

export function ListToolbar({
  keyword,
  placeholder,
  status,
  statusOptions,
  loading,
  lastUpdated,
  onKeywordChange,
  onKeywordCommit,
  onStatusChange,
  onRefresh,
  onClear,
  exactFields = [],
  exactValues = {},
  onExactChange,
}: {
  keyword: string;
  placeholder: string;
  status: string;
  statusOptions: FilterOption[];
  loading: boolean;
  lastUpdated?: Date;
  onKeywordChange: (value: string) => void;
  onKeywordCommit: () => void;
  onStatusChange: (value: string) => void;
  onRefresh: () => void;
  onClear: () => void;
  exactFields?: ExactFilter[];
  exactValues?: Record<string, string>;
  onExactChange?: (key: string, value: string) => void;
}) {
  const [advanced, setAdvanced] = useState(false);
  const active = exactFields.filter((field) => exactValues[field.key]);
  return (
    <section className="list-toolbar" aria-label="列表查询">
      <form
        onSubmit={(event) => {
          event.preventDefault();
          onKeywordCommit();
        }}
      >
        <TextInput
          id="list-keyword"
          labelText="关键词"
          placeholder={placeholder}
          value={keyword}
          onChange={(event) => onKeywordChange(event.target.value)}
        />
        <Select id="list-status" labelText="状态" value={status} onChange={(event) => onStatusChange(event.target.value)}>
          {statusOptions.map((option) => <SelectItem value={option.value} text={option.label} key={option.value} />)}
        </Select>
        <div className="toolbar-actions">
          {exactFields.length > 0 && <Button type="button" size="sm" kind="tertiary" onClick={() => setAdvanced((value) => !value)}>精确筛选{active.length ? ` · ${active.length}` : ""}</Button>}
          <Button
            type="button"
            size="sm"
            kind="ghost"
            onClick={onRefresh}
            disabled={loading}
          >
            {loading ? "查询中" : "刷新"}
          </Button>
          <Button
            type="button"
            size="sm"
            kind="ghost"
            onClick={onClear}
          disabled={!keyword && !status && !active.length}
          >
            清空
          </Button>
        </div>
      </form>
      <p aria-live="polite">
        {lastUpdated
          ? `更新于 ${lastUpdated.toLocaleTimeString()}`
          : "等待查询"}
      </p>
      {active.length > 0 && <div className="filter-chips">{active.map((field) => <button type="button" key={field.key} onClick={() => onExactChange?.(field.key, "")}>{field.label}：{exactValues[field.key]} <span>×</span></button>)}</div>}
      {advanced && <aside className="exact-filter-panel" aria-label="精确筛选条件">
        <header><div><strong>精确筛选</strong><small>不同字段之间按 AND 组合</small></div><button type="button" onClick={() => setAdvanced(false)}>关闭</button></header>
        <div>{exactFields.map((field) => <TextInput key={field.key} id={`exact-${field.key}`} type={field.type || "text"} labelText={field.label} placeholder={field.placeholder} value={exactValues[field.key] || ""} onChange={(event) => onExactChange?.(field.key, event.target.value)} />)}</div>
      </aside>}
    </section>
  );
}

function pageNumbers(page: number, total: number): number[] {
  if (total <= 7) return Array.from({ length: total }, (_, index) => index + 1);
  const start = Math.max(1, Math.min(page - 2, total - 4));
  return Array.from({ length: 5 }, (_, index) => start + index);
}

export function ListPagination({
  page,
  pageSize,
  total,
  totalPages,
  loading,
  onPageChange,
  onPageSizeChange,
}: {
  page: number;
  pageSize: number;
  total: number;
  totalPages: number;
  loading: boolean;
  onPageChange: (page: number) => void;
  onPageSizeChange: (size: number) => void;
}) {
  return (
    <nav className="list-pagination" aria-label="分页">
      <span>
        共 <b>{total}</b> 条
      </span>
      <div className="page-buttons">
        <button
          type="button"
          onClick={() => onPageChange(page - 1)}
          disabled={loading || page <= 1}
        >
          上一页
        </button>
        {pageNumbers(page, totalPages).map((value) => (
          <button
            type="button"
            key={value}
            className={value === page ? "active" : ""}
            aria-current={value === page ? "page" : undefined}
            onClick={() => onPageChange(value)}
            disabled={loading}
          >
            {value}
          </button>
        ))}
        <button
          type="button"
          onClick={() => onPageChange(page + 1)}
          disabled={loading || totalPages === 0 || page >= totalPages}
        >
          下一页
        </button>
      </div>
      <label>
        每页
        <select
          value={pageSize}
          onChange={(event) => onPageSizeChange(Number(event.target.value))}
        >
          <option value="20">20</option>
          <option value="50">50</option>
          <option value="100">100</option>
        </select>
      </label>
      <span className="page-summary">
        第 {page} / {totalPages || 0} 页
      </span>
    </nav>
  );
}
