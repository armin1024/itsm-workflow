import { useEffect, useMemo, useState } from "react";
import { InlineLoading, InlineNotification, TextInput } from "@carbon/react";
import { api } from "./api";

type Operation = { method: string; path: string; summary: string; description: string; tags: string[]; operationId: string };

export default function ApiDocs() {
  const [document, setDocument] = useState<Record<string, unknown>>(), [error, setError] = useState(""), [query, setQuery] = useState("");
  useEffect(() => { api.openApi().then(setDocument).catch((value) => setError(value.message)); }, []);
  const operations = useMemo(() => {
    const paths = (document?.paths || {}) as Record<string, Record<string, Record<string, unknown>>>;
    return Object.entries(paths).flatMap(([path, methods]) => Object.entries(methods).filter(([method]) => ["get", "post", "put", "patch", "delete"].includes(method)).map(([method, value]) => ({ method: method.toUpperCase(), path, summary: String(value.summary || value.operationId || ""), description: String(value.description || ""), tags: (value.tags || []) as string[], operationId: String(value.operationId || "") }))).filter((item) => `${item.method} ${item.path} ${item.summary}`.toLowerCase().includes(query.toLowerCase()));
  }, [document, query]);
  return <main className="page api-docs"><header className="page-title"><div><p className="overline">LOCAL OPENAPI REFERENCE</p><h1>API文档</h1><p>由服务自身的OpenAPI JSON渲染，不加载Swagger CDN，适用于完全隔离的内网环境。</p></div><code>{String((document?.info as Record<string, unknown> | undefined)?.version || "")}</code></header>{error && <InlineNotification kind="error" title="无法读取OpenAPI" subtitle={error} hideCloseButton />}{!document ? <InlineLoading description="加载OpenAPI" /> : <><TextInput id="api-search" labelText="筛选接口" placeholder="方法、路径或名称" value={query} onChange={(event) => setQuery(event.target.value)} /><section className="endpoint-list">{operations.map((item) => <article key={`${item.method}-${item.path}`}><span className={`method method-${item.method.toLowerCase()}`}>{item.method}</span><code>{item.path}</code><div><h2>{item.summary}</h2>{item.description && <p>{item.description}</p>}<small>{item.tags.join(" / ") || "default"} · {item.operationId}</small></div></article>)}</section>{operations.length === 0 && <div className="inspector-empty"><span>00</span><h3>没有匹配接口</h3><p>清空筛选词后查看全部Runtime与Studio API。</p></div>}</>}</main>;
}
