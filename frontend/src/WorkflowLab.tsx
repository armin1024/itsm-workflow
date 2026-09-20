import { useEffect, useState } from "react";
import { Button, InlineNotification } from "@carbon/react";
import { api } from "./api";
import WorkflowEditor, { newWorkflow, type EditableWorkflow } from "./WorkflowEditor";
import { pretty } from "./ui";

export default function WorkflowLab() {
  const [definition, setDefinition] = useState<EditableWorkflow>(newWorkflow());
  const [result, setResult] = useState<Record<string, unknown>>();
  const [error, setError] = useState("");
  const [allowedTypes, setAllowedTypes] = useState<Set<string>>();
  useEffect(() => { api.studioCatalog().then((value) => setAllowedTypes(new Set(value.nodes.filter((node) => node.studioEnabled !== false).map((node) => node.type)))); }, []);
  const validate = async () => { setError(""); try { setResult(await api.validateWorkflow(definition as unknown as Record<string, unknown>)); } catch (value) { setError((value as Error).message); } };
  return <main className="page workflow-lab"><header className="page-title"><div><p className="overline">DAG AUTHORING / TEST</p><h1>流程编排</h1><p>拖拽节点、连接条件和结果依赖，然后使用正式Registry执行静态校验。</p></div><Button onClick={validate}>校验流程</Button></header>{error && <InlineNotification kind="error" title="流程校验失败" subtitle={error} hideCloseButton />}<WorkflowEditor definition={definition} onChange={setDefinition} allowedTypes={allowedTypes} />{result && <section className="validation-result"><h2>校验通过</h2><p>Catalog <code>{String(result.catalogDigest).slice(0, 16)}</code></p><pre>{pretty(result)}</pre></section>}</main>;
}
