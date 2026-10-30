import { useEffect, useMemo, useState } from "react";
import { Button, InlineLoading, InlineNotification, TextInput } from "@carbon/react";
import { api } from "./api";

type Field = { name: string; in?: string; required?: boolean; description?: string; schema?: Record<string, unknown> };
type Operation = { method: string; path: string; summary: string; description: string; tags: string[]; operationId: string; parameters: Field[]; requestBody?: Record<string, unknown> };

function schemaType(schema: Record<string, unknown>): string {
  const types: Record<string,string> = { string: "字符串", integer: "整数", number: "数字", boolean: "布尔值", object: "JSON对象", array: "数组", null: "可为空" };
  if (schema.anyOf) return (schema.anyOf as Record<string,unknown>[]).map(schemaType).join(" / ");
  return types[String(schema.type)] || (schema.$ref ? "结构化对象" : "按业务约束");
}

function Fields({ fields }: { fields: Field[] }) {
  return <div className="api-field-table"><div className="api-field-row api-field-head"><span>参数 / 技术字段</span><span>位置 / 类型</span><span>说明</span></div>{fields.map(field => <div className="api-field-row" key={`${field.in}-${field.name}`}><span><strong>{String(field.schema?.title || field.name)}</strong><code>{field.name}</code></span><span>{({query:"查询参数",path:"路径参数",header:"请求头",cookie:"会话Cookie",body:"请求正文"} as Record<string,string>)[field.in || "body"]}<small>{schemaType(field.schema || {})} · {field.required ? "必填" : "可选"}</small></span><span>{field.description || String(field.schema?.description || "请参考JSON约束")}{Array.isArray(field.schema?.enum) && <small>允许值：{(field.schema.enum as unknown[]).join("、")}</small>}</span></div>)}</div>;
}

export default function ApiDocs() {
  const [document, setDocument] = useState<Record<string, unknown>>(), [error, setError] = useState(""), [query, setQuery] = useState("");
  const requestFields = (operation: Operation): Field[] => {
    const content = operation.requestBody?.content as Record<string, { schema?: Record<string,unknown> }> | undefined;
    let schema = content?.["application/json"]?.schema || {};
    if (typeof schema.$ref === "string") {
      const components = document?.components as { schemas?: Record<string, Record<string,unknown>> } | undefined;
      schema = components?.schemas?.[schema.$ref.split("/").pop()!] || {};
    }
    const required = (schema.required || []) as string[];
    return Object.entries((schema.properties || {}) as Record<string, Record<string,unknown>>).map(([name,value]) => ({ name, in:"body", required:required.includes(name), schema:value, description:String(value.description || "") }));
  };
  const reload = () => { setError(""); api.openApi().then(setDocument).catch((value) => setError(value.message)); };
  useEffect(() => { reload(); }, []);
  const operations = useMemo(() => {
    const paths = (document?.paths || {}) as Record<string, Record<string, Record<string, unknown>>>;
    return Object.entries(paths).flatMap(([path, methods]) => Object.entries(methods).filter(([method]) => ["get", "post", "put", "patch", "delete"].includes(method)).map(([method, value]) => ({ method: method.toUpperCase(), path, summary: String(value.summary || value.operationId || ""), description: String(value.description || ""), tags: (value.tags || []) as string[], operationId: String(value.operationId || ""), parameters: (value.parameters || []) as Field[], requestBody: value.requestBody as Record<string,unknown> | undefined }))).filter((item) => `${item.method} ${item.path} ${item.summary} ${item.description} ${item.tags.join(" ")}`.toLowerCase().includes(query.toLowerCase()));
  }, [document, query]);
  return <main className="page api-docs"><header className="page-title"><div><p className="overline">本地接口说明</p><h1>API文档</h1><p>由服务自身的OpenAPI JSON渲染，不加载Swagger CDN，适用于完全隔离的内网环境。</p></div><code>{String((document?.info as Record<string, unknown> | undefined)?.version || "")}</code></header>{error && <InlineNotification kind="error" title="无法读取OpenAPI" subtitle={error} hideCloseButton />}{!document ? error ? <Button kind="tertiary" onClick={reload}>重新读取接口说明</Button> : <InlineLoading description="加载OpenAPI" /> : <><TextInput id="api-search" labelText="筛选接口" placeholder="中文功能、方法或接口路径" value={query} onChange={(event) => setQuery(event.target.value)} /><section className="endpoint-list">{operations.map((item) => <article key={`${item.method}-${item.path}`}><span className={`method method-${item.method.toLowerCase()}`}>{item.method}</span><code>{item.path}</code><div><h2>{item.summary}</h2>{item.description && <p>{item.description}</p>}<small>{item.tags.join(" / ") || "服务接口"}</small>{(item.parameters.length > 0 || item.requestBody) && <details className="api-parameter-details"><summary>查看中文参数与请求结构</summary>{item.parameters.length > 0 && <Fields fields={item.parameters} />}{item.requestBody && <><h3>请求正文（JSON）</h3><Fields fields={requestFields(item)} /><p>技术字段名保持英文，按上方中文说明填写；复杂节点和连线还需通过Workflow JSON约束校验。</p></>}</details>}<small>技术标识：{item.operationId}</small></div></article>)}</section>{operations.length === 0 && <div className="inspector-empty"><span>00</span><h3>没有匹配接口</h3><p>清空筛选词后查看全部Runtime与Studio API。</p></div>}</>}</main>;
}
