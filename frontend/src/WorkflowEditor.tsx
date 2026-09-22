import { Button, TextArea, TextInput } from "@carbon/react";
import {
  Background,
  Controls,
  Handle,
  MarkerType,
  MiniMap,
  Position,
  ReactFlow,
  addEdge,
  applyEdgeChanges,
  applyNodeChanges,
  type Connection,
  type Edge,
  type EdgeChange,
  type Node,
  type NodeChange,
  type NodeProps,
} from "@xyflow/react";
import { memo, useEffect, useState } from "react";
import type { WorkflowEdge, WorkflowNode } from "./types";

export type EditableWorkflow = {
  schemaVersion: 1;
  entryNodeId: string;
  nodes: WorkflowNode[];
  edges: WorkflowEdge[];
};
type CardData = { node: WorkflowNode; entry: boolean; [key: string]: unknown };

const labels: Record<string, string> = {
  sql_read: "SQL READ",
  condition: "CONDITION",
  hitl_select: "HITL SELECT",
  hitl_form: "HITL FORM",
  end: "END",
};
const GraphCard = memo(({ data, selected }: NodeProps<Node<CardData>>) => {
  const node = data.node,
    bound = node.inputs.filter(
      (input) => input.source.kind === "NODE_OUTPUT",
    ).length;
  return (
    <article
      className={`author-node type-${node.type} ${selected ? "selected" : ""}`}
    >
      <Handle type="target" position={Position.Left} />
      <div>
        <span>{labels[node.type] || node.type}</span>
        {data.entry && <i>入口</i>}
      </div>
      <h3>{node.title}</h3>
      <p>
        {node.type === "sql_read"
          ? String(node.config.databaseRef || "待配置数据库")
          : node.type === "condition"
            ? "按前置结果选择分支"
            : node.type === "hitl_select"
              ? "从查询结果中人工选择"
              : node.type === "hitl_form"
                ? "等待人工填写结构化参数"
            : "汇总并结束运行"}
      </p>
      <footer>
        {node.type === "sql_read"
          ? `${node.inputs.length} 输入${bound ? ` · ${bound} 数据绑定` : ""}`
          : node.type === "condition"
            ? "条件路由"
            : node.type === "hitl_select"
              ? "候选分页 · 人工确认"
              : node.type === "hitl_form"
                ? "结构化表单"
            : "终点"}
      </footer>
      {node.type !== "end" && (
        <Handle type="source" position={Position.Right} />
      )}
    </article>
  );
});
const nodeTypes = { author: GraphCard };

const sqlNode = (id: string, x: number, y: number): WorkflowNode => ({
  id,
  type: "sql_read",
  title: "只读查询",
  config: {
    databaseRef: "",
    sqlTemplate: "SELECT id FROM table_name WHERE field={{value}}",
  },
  inputs: [
    {
      name: "value",
      type: "string",
      description: "从当前工单上下文确认的查询条件",
      source: { kind: "RUN_INPUT", key: "value" },
    },
  ],
  approvalPolicy: "PLAN",
  timeoutSeconds: 600,
  uiPosition: { x, y },
});
const conditionNode = (id: string, x: number, y: number): WorkflowNode => ({
  id,
  type: "condition",
  title: "判断查询结果",
  config: {},
  inputs: [],
  approvalPolicy: "NONE",
  timeoutSeconds: 60,
  uiPosition: { x, y },
});
const endNode = (id: string, x: number, y: number): WorkflowNode => ({
  id,
  type: "end",
  title: "完成并展示结果",
  config: {},
  inputs: [],
  approvalPolicy: "NONE",
  timeoutSeconds: 60,
  uiPosition: { x, y },
});
const hitlSelectNode = (id: string, x: number, y: number): WorkflowNode => ({
  id, type: "hitl_select", title: "选择候选参数",
  config: { title: "请选择用于后续步骤的参数", selectionMode: "SINGLE", idPath: "/customer_id", labelTemplate: "{{customer_name}} / {{customer_id}}", displayFields: [{ name: "customer_name", label: "客户姓名", path: "/customer_name" }, { name: "customer_id", label: "客户编号", path: "/customer_id" }], outputFields: [{ name: "customer_id", path: "/customer_id" }], minimumSelections: 1, maximumSelections: 1 },
  inputs: [{ name: "rows", type: "array", description: "前置SQL查询结果", source: { kind: "NODE_OUTPUT", nodeId: "", jsonPointer: "/data" } }],
  approvalPolicy: "NONE", timeoutSeconds: 60, uiPosition: { x, y },
});
const hitlFormNode = (id: string, x: number, y: number): WorkflowNode => ({
  id, type: "hitl_form", title: "填写查询参数",
  config: { title: "填写后续查询参数", description: "请确认后续查询所需参数", fields: [{ name: "customer_id", label: "客户编号", type: "string", required: true }] },
  inputs: [], approvalPolicy: "NONE", timeoutSeconds: 60, uiPosition: { x, y },
});

export function newWorkflow(): EditableWorkflow {
  return {
    schemaVersion: 1,
    entryNodeId: "sql-1",
    nodes: [sqlNode("sql-1", 80, 120), endNode("done", 520, 120)],
    edges: [{ id: "edge-1", source: "sql-1", target: "done" }],
  };
}

const arrow = {
  type: MarkerType.ArrowClosed,
  color: "#66877f",
  width: 18,
  height: 18,
};
function toFlow(definition: EditableWorkflow): {
  nodes: Node<CardData>[];
  edges: Edge[];
} {
  return {
    nodes: definition.nodes.map((node, index) => ({
      id: node.id,
      type: "author",
      position: node.uiPosition || { x: 80 + index * 320, y: 120 },
      data: { node, entry: node.id === definition.entryNodeId },
    })),
    edges: definition.edges.map((edge) => ({
      id: edge.id,
      source: edge.source,
      target: edge.target,
      label: edge.default ? "默认" : edge.label || "",
      type: "bezier",
      markerEnd: arrow,
      data: { default: edge.default, condition: edge.condition },
    })),
  };
}

export default function WorkflowEditor({
  definition,
  onChange,
}: {
  definition: EditableWorkflow;
  onChange: (value: EditableWorkflow) => void;
}) {
  const initial = toFlow(definition),
    [nodes, setNodes] = useState(initial.nodes),
    [edges, setEdges] = useState(initial.edges),
    [entry, setEntryState] = useState(definition.entryNodeId),
    [selectedNode, setSelectedNode] = useState(definition.entryNodeId),
    [selectedEdge, setSelectedEdge] = useState(""),
    [message, setMessage] = useState(""),
    [fullscreen, setFullscreen] = useState(false);
  useEffect(() => {
    const close = (event: KeyboardEvent) => {
      if (event.key === "Escape") setFullscreen(false);
    };
    window.addEventListener("keydown", close);
    return () => window.removeEventListener("keydown", close);
  }, []);
  const emit = (
    nextNodes: Node<CardData>[],
    nextEdges: Edge[],
    nextEntry = entry,
  ) => {
    const validEntry = nextNodes.some((node) => node.id === nextEntry)
      ? nextEntry
      : nextNodes[0]?.id || "";
    onChange({
      schemaVersion: 1,
      entryNodeId: validEntry,
      nodes: nextNodes.map((item) => ({
        ...item.data.node,
        uiPosition: item.position,
      })),
      edges: nextEdges.map((item) => ({
        id: item.id,
        source: item.source,
        target: item.target,
        label: String(item.label || ""),
        default: Boolean(item.data?.default),
        condition: item.data?.condition as Record<string, unknown> | undefined,
      })),
    });
  };
  const nodeChanges = (changes: NodeChange[]) =>
    setNodes((current) => {
      const next = applyNodeChanges(changes, current) as Node<CardData>[];
      emit(next, edges);
      return next;
    });
  const edgeChanges = (changes: EdgeChange[]) =>
    setEdges((current) => {
      const next = applyEdgeChanges(changes, current);
      emit(nodes, next);
      return next;
    });
  const connect = (connection: Connection) => {
    if (
      !connection.source ||
      !connection.target ||
      connection.source === connection.target
    )
      return;
    const source = nodes.find((node) => node.id === connection.source)?.data
      .node;
    if (!source || source.type === "end") return;
    if (
      source.type !== "condition" &&
      edges.some((edge) => edge.source === source.id)
    ) {
      setMessage("普通节点只能连接一个后续节点；请插入条件判断节点进行分支。");
      return;
    }
    const outgoing = edges.filter((edge) => edge.source === source.id);
    const edge: Edge = {
      ...connection,
      id: `edge-${Date.now()}`,
      type: "bezier",
      markerEnd: arrow,
      label:
        source.type === "condition" && outgoing.length === 0
          ? "默认"
          : "条件分支",
      data:
        source.type === "condition"
          ? outgoing.length === 0
            ? { default: true }
            : {
                default: false,
                condition: {
                  path: "/nodes/sql-1/output/data/0/status",
                  op: "eq",
                  value: "ACTIVE",
                },
              }
          : {},
    };
    setEdges((current) => {
      const next = addEdge(edge, current);
      emit(nodes, next);
      return next;
    });
    setSelectedEdge(edge.id);
    setSelectedNode("");
  };
  const add = (type: "sql_read" | "condition" | "hitl_select" | "hitl_form" | "end") => {
    const prefix = type === "sql_read" ? "sql" : type === "condition" ? "condition" : type === "hitl_select" ? "hitl-select" : type === "hitl_form" ? "hitl-form" : "end",
      id = `${prefix}-${Date.now()}`,
      position = {
        x: 120 + (nodes.length % 3) * 360,
        y: 100 + Math.floor(nodes.length / 3) * 220,
      },
      node =
        type === "sql_read"
          ? sqlNode(id, position.x, position.y)
          : type === "condition"
            ? conditionNode(id, position.x, position.y)
            : type === "hitl_select"
              ? hitlSelectNode(id, position.x, position.y)
              : type === "hitl_form"
                ? hitlFormNode(id, position.x, position.y)
            : endNode(id, position.x, position.y),
      flow: Node<CardData> = {
        id,
        type: "author",
        position,
        data: { node, entry: false },
      };
    setNodes((current) => {
      const next = [...current, flow];
      emit(next, edges);
      return next;
    });
    setSelectedNode(id);
    setSelectedEdge("");
  };
  const patchNode = (patch: Partial<WorkflowNode>) =>
    setNodes((current) => {
      const next = current.map((item) =>
        item.id === selectedNode
          ? {
              ...item,
              data: { ...item.data, node: { ...item.data.node, ...patch } },
            }
          : item,
      );
      emit(next, edges);
      return next;
    });
  const removeNode = () => {
    const nextNodes = nodes.filter((item) => item.id !== selectedNode),
      nextEdges = edges.filter(
        (edge) => edge.source !== selectedNode && edge.target !== selectedNode,
      ),
      nextEntry = entry === selectedNode ? nextNodes[0]?.id || "" : entry;
    setNodes(nextNodes);
    setEdges(nextEdges);
    setEntryState(nextEntry);
    emit(nextNodes, nextEdges, nextEntry);
    setSelectedNode(nextNodes[0]?.id || "");
  };
  const markEntry = () => {
    const next = nodes.map((item) => ({
      ...item,
      data: { ...item.data, entry: item.id === selectedNode },
    }));
    setNodes(next);
    setEntryState(selectedNode);
    emit(next, edges, selectedNode);
  };
  const current = nodes.find((item) => item.id === selectedNode)?.data.node,
    edge = edges.find((item) => item.id === selectedEdge);
  const patchEdge = (patch: {
    label?: string;
    default?: boolean;
    condition?: Record<string, unknown>;
  }) =>
    setEdges((currentEdges) => {
      let next = currentEdges.map((item) =>
        item.id === selectedEdge
          ? {
              ...item,
              label: patch.default ? "默认" : (patch.label ?? item.label),
              data: { ...item.data, ...patch },
            }
          : item,
      );
      if (patch.default) {
        const source = edge?.source;
        next = next.map((item) =>
          item.source === source && item.id !== selectedEdge
            ? { ...item, data: { ...item.data, default: false } }
            : item,
        );
      }
      emit(nodes, next);
      return next;
    });
  const syncInputs = () => {
    if (!current || current.type !== "sql_read") return;
    const names = [
        ...String(current.config.sqlTemplate || "").matchAll(
          /\{\{([A-Za-z_][A-Za-z0-9_]*)\}\}/g,
        ),
      ].map((match) => match[1]),
      unique = [...new Set(names)];
    patchNode({
      inputs: unique.map(
        (name) =>
          current.inputs.find((input) => input.name === name) || {
            name,
            type: "string",
            description: "从当前工单上下文确认的查询条件",
            source: { kind: "RUN_INPUT", key: name },
          },
      ),
    });
  };
  const predecessors = current
    ? reachablePredecessors(current.id, nodes, edges)
    : [];
  return (
    <section className={`graph-editor ${fullscreen ? "is-fullscreen" : ""}`}>
      <header className="graph-toolbar">
        <div>
          <p className="overline">VISUAL WORKFLOW</p>
          <h2>流程画布</h2>
        </div>
        <div>
          <Button type="button" size="sm" kind="tertiary" onClick={() => add("sql_read")}>
            ＋ SQL 读
          </Button>
          <Button type="button" size="sm" kind="tertiary" onClick={() => add("condition")}>
            ◇ 条件判断
          </Button>
          <Button type="button" size="sm" kind="tertiary" onClick={() => add("hitl_select")}>＋ 人工选择</Button>
          <Button type="button" size="sm" kind="tertiary" onClick={() => add("hitl_form")}>＋ 人工表单</Button>
          <Button type="button" size="sm" kind="ghost" onClick={() => add("end")}>
            ＋ 结束
          </Button>
          <Button type="button" size="sm" kind="ghost" onClick={() => setFullscreen((value) => !value)}>
            {fullscreen ? "退出全屏" : "全屏编排"}
          </Button>
        </div>
      </header>
      {message && (
        <button
          className="canvas-message"
          type="button"
          onClick={() => setMessage("")}
        >
          {message} ×
        </button>
      )}
      <div className="graph-workspace">
        <div className="editable-canvas">
          <ReactFlow
            nodes={nodes}
            edges={edges}
            nodeTypes={nodeTypes}
            onNodesChange={nodeChanges}
            onEdgesChange={edgeChanges}
            onConnect={connect}
            onNodeClick={(_, node) => {
              setSelectedNode(node.id);
              setSelectedEdge("");
            }}
            onEdgeClick={(_, value) => {
              setSelectedEdge(value.id);
              setSelectedNode("");
            }}
            fitView
            minZoom={0.25}
            maxZoom={1.8}
            deleteKeyCode={null}
            defaultEdgeOptions={{ type: "bezier", markerEnd: arrow }}
            proOptions={{ hideAttribution: true }}
          >
            <Background color="#b8c9c4" gap={24} />
            <MiniMap
              pannable
              zoomable
              nodeColor={(node) =>
                (node.data as CardData).node.type === "condition"
                  ? "#8e6a00"
                  : (node.data as CardData).node.type === "end"
                    ? "#5c8178"
                    : "#007d79"
              }
            />
            <Controls />
          </ReactFlow>
        </div>
        <aside className="author-inspector">
          {current ? (
            <NodeInspector
              node={current}
              predecessors={predecessors}
              isEntry={entry === current.id}
              patch={patchNode}
              setEntry={markEntry}
              remove={removeNode}
              syncInputs={syncInputs}
            />
          ) : edge ? (
            <EdgeInspector
              edge={edge}
              sourceType={
                nodes.find((item) => item.id === edge.source)?.data.node.type ||
                ""
              }
              patch={patchEdge}
              remove={() => {
                const next = edges.filter((item) => item.id !== edge.id);
                setEdges(next);
                emit(nodes, next);
                setSelectedEdge("");
              }}
            />
          ) : (
            <div className="inspector-empty">
              <span>↗</span>
              <h3>选择节点或连线</h3>
              <p>点击卡片编辑配置；从节点右侧端口拖到下一节点完成连线。</p>
            </div>
          )}
        </aside>
      </div>
    </section>
  );
}

function reachablePredecessors(
  target: string,
  nodes: Node<CardData>[],
  edges: Edge[],
): WorkflowNode[] {
  const reverse = new Map<string, string[]>();
  edges.forEach((edge) =>
    reverse.set(edge.target, [
      ...(reverse.get(edge.target) || []),
      edge.source,
    ]),
  );
  const found = new Set<string>(),
    pending = [...(reverse.get(target) || [])];
  while (pending.length) {
    const id = pending.pop()!;
    if (found.has(id)) continue;
    found.add(id);
    pending.push(...(reverse.get(id) || []));
  }
  return nodes
    .filter((item) => found.has(item.id) && item.data.node.type !== "end")
    .map((item) => item.data.node);
}

function NodeInspector({
  node,
  predecessors,
  isEntry,
  patch,
  setEntry,
  remove,
  syncInputs,
}: {
  node: WorkflowNode;
  predecessors: WorkflowNode[];
  isEntry: boolean;
  patch: (value: Partial<WorkflowNode>) => void;
  setEntry: () => void;
  remove: () => void;
  syncInputs: () => void;
}) {
  const patchInput = (index: number, value: WorkflowNode["inputs"][number]) =>
    patch({
      inputs: node.inputs.map((item, position) =>
        position === index ? value : item,
      ),
    });
  const patchConfig = (key: string, value: unknown) => patch({ config: { ...node.config, [key]: value } });
  const displayFields = (node.config.displayFields as Array<{ name: string; label: string; path: string }> | undefined) || [];
  const outputFields = (node.config.outputFields as Array<{ name: string; path: string }> | undefined) || [];
  const formFields = (node.config.fields as Array<{ name: string; label: string; type: string; required: boolean; minimum?: number; maximum?: number; minLength?: number; maxLength?: number; enum?: unknown[] }> | undefined) || [];
  return (
    <div>
      <p className="overline">NODE CONFIG</p>
      <div className="inspector-title">
        <h3>{labels[node.type] || node.type}</h3>
        {isEntry ? (
          <span>入口节点</span>
        ) : (
          <button type="button" onClick={setEntry}>
            设为入口
          </button>
        )}
      </div>
      <TextInput
        id={`${node.id}-title`}
        labelText="节点标题"
        value={node.title}
        onChange={(event) => patch({ title: event.target.value })}
      />
      {node.type === "sql_read" && (
        <>
          <TextInput
            id={`${node.id}-db`}
            labelText="数据库路径"
            value={String(node.config.databaseRef || "")}
            onChange={(event) =>
              patch({
                config: { ...node.config, databaseRef: event.target.value },
              })
            }
            placeholder="serverid/dbid/dbname/dbuser/service_name"
          />
          <TextArea
            id={`${node.id}-sql`}
            labelText="SQL 模板"
            rows={8}
            value={String(node.config.sqlTemplate || "")}
            onChange={(event) =>
              patch({
                config: { ...node.config, sqlTemplate: event.target.value },
              })
            }
          />
          <Button size="sm" kind="ghost" onClick={syncInputs}>
            从 SQL 同步输入
          </Button>
          <div className="inspector-inputs">
            {node.inputs.map((input, index) => (
              <div key={`${input.name}-${index}`}>
                <header>
                  <code>{input.name}</code>
                  <select
                    value={input.type}
                    onChange={(event) =>
                      patchInput(index, { ...input, type: event.target.value })
                    }
                  >
                    <option>string</option>
                    <option>integer</option>
                    <option>number</option>
                    <option>boolean</option>
                  </select>
                </header>
                <label>
                  输入来源
                  <select
                    value={input.source.kind}
                    onChange={(event) =>
                      patchInput(index, {
                        ...input,
                        source:
                          event.target.value === "NODE_OUTPUT"
                            ? {
                                kind: "NODE_OUTPUT",
                                nodeId: predecessors[0]?.id || "",
                                jsonPointer: "/data/0/id",
                              }
                            : { kind: "RUN_INPUT", key: input.name },
                      })
                    }
                  >
                    <option value="RUN_INPUT">运行输入</option>
                    {predecessors.length > 0 && (
                      <option value="NODE_OUTPUT">前置节点输出</option>
                    )}
                  </select>
                </label>
                {input.source.kind === "NODE_OUTPUT" ? (
                  <>
                    <label>
                      来源节点
                      <select
                        value={input.source.nodeId}
                        onChange={(event) =>
                          patchInput(index, {
                            ...input,
                            source: {
                              ...input.source,
                              nodeId: event.target.value,
                            },
                          })
                        }
                      >
                        {predecessors.map((item) => (
                          <option value={item.id} key={item.id}>
                            {item.title}
                          </option>
                        ))}
                      </select>
                    </label>
                    <label>
                      结果路径 · JSON Pointer
                      <input
                        value={input.source.jsonPointer || ""}
                        onChange={(event) =>
                          patchInput(index, {
                            ...input,
                            source: {
                              ...input.source,
                              jsonPointer: event.target.value,
                            },
                          })
                        }
                        placeholder="/data/0/id"
                      />
                    </label>
                    <p className="binding-note">
                      数据流：{input.source.nodeId || "选择节点"} → {input.name}
                    </p>
                  </>
                ) : (
                  <label>
                    运行参数键
                    <input
                      value={input.source.key || input.name}
                      onChange={(event) =>
                        patchInput(index, {
                          ...input,
                          source: {
                            kind: "RUN_INPUT",
                            key: event.target.value,
                          },
                        })
                      }
                    />
                  </label>
                )}
              </div>
            ))}
          </div>
        </>
      )}
      {node.type === "condition" && (
        <div className="condition-help">
          <b>条件配置在分支连线上</b>
          <p>
            先把前置节点连到本节点，再从本节点连接多个目标。点击出线配置结果路径、运算符和默认分支。
          </p>
        </div>
      )}
      {node.type === "hitl_select" && (
        <div className="hitl-editor">
          <TextInput id={`${node.id}-hitl-title`} labelText="交互标题" value={String(node.config.title || "")} onChange={(event) => patchConfig("title", event.target.value)} />
          <label>选择模式<select value={String(node.config.selectionMode || "SINGLE")} onChange={(event) => { const mode = event.target.value; patch({ config: { ...node.config, selectionMode: mode, minimumSelections: 1, maximumSelections: mode === "SINGLE" ? 1 : Math.max(2, Number(node.config.maximumSelections || 10)) } }); }}><option value="SINGLE">单选</option><option value="MULTIPLE">多选</option></select></label>
          <div className="two-fields"><TextInput id={`${node.id}-id-path`} labelText="候选唯一值路径" value={String(node.config.idPath || "")} onChange={(event) => patchConfig("idPath", event.target.value)} /><TextInput id={`${node.id}-label-template`} labelText="候选标题模板" value={String(node.config.labelTemplate || "")} onChange={(event) => patchConfig("labelTemplate", event.target.value)} /></div>
          {String(node.config.selectionMode) === "MULTIPLE" && <div className="two-fields"><TextInput id={`${node.id}-minimum`} type="number" labelText="最少选择" value={String(node.config.minimumSelections || 1)} onChange={(event) => patchConfig("minimumSelections", Number(event.target.value))} /><TextInput id={`${node.id}-maximum`} type="number" labelText="最多选择" value={String(node.config.maximumSelections || 10)} onChange={(event) => patchConfig("maximumSelections", Number(event.target.value))} /></div>}
          <section className="binding-editor"><b>候选来源</b><label>前置节点<select value={String(node.inputs[0]?.source.nodeId || "")} onChange={(event) => patch({ inputs: [{ ...node.inputs[0], name: "rows", type: "array", source: { kind: "NODE_OUTPUT", nodeId: event.target.value, jsonPointer: node.inputs[0]?.source.jsonPointer || "/data" } }] })}><option value="">请选择</option>{predecessors.map((item) => <option key={item.id} value={item.id}>{item.title}</option>)}</select></label><TextInput id={`${node.id}-rows-path`} labelText="结果数组 JSON Pointer" value={String(node.inputs[0]?.source.jsonPointer || "/data")} onChange={(event) => patch({ inputs: [{ ...node.inputs[0], name: "rows", type: "array", source: { kind: "NODE_OUTPUT", nodeId: node.inputs[0]?.source.nodeId || "", jsonPointer: event.target.value } }] })} /></section>
          <FieldList title="展示字段" fields={displayFields} includeLabel onChange={(value) => patchConfig("displayFields", value)} />
          <FieldList title="下游输出字段" fields={outputFields} onChange={(value) => patchConfig("outputFields", value)} />
        </div>
      )}
      {node.type === "hitl_form" && (
        <div className="hitl-editor">
          <TextInput id={`${node.id}-form-title`} labelText="表单标题" value={String(node.config.title || "")} onChange={(event) => patchConfig("title", event.target.value)} />
          <TextArea id={`${node.id}-form-description`} labelText="填写说明" value={String(node.config.description || "")} onChange={(event) => patchConfig("description", event.target.value)} />
          <FormFieldList fields={formFields} onChange={(value) => patchConfig("fields", value)} />
        </div>
      )}
      <div className="danger-zone">
        <Button size="sm" kind="danger--ghost" onClick={remove}>
          删除节点
        </Button>
      </div>
    </div>
  );
}

function FieldList({ title, fields, includeLabel = false, onChange }: { title: string; fields: Array<{ name: string; label?: string; path: string }>; includeLabel?: boolean; onChange: (value: Array<{ name: string; label?: string; path: string }>) => void }) {
  const change = (index: number, key: string, value: string) => onChange(fields.map((item, position) => position === index ? { ...item, [key]: value } : item));
  return <section className="field-editor-list"><header><div><b>{title}</b><small>仅声明字段会进入对应视图</small></div><button type="button" onClick={() => onChange([...fields, { name: `field_${fields.length + 1}`, ...(includeLabel ? { label: "字段" } : {}), path: "/field" }])}>＋ 添加</button></header>{fields.map((field, index) => <div className={includeLabel ? "field-row three" : "field-row"} key={index}><input aria-label={`${title}名称`} value={field.name} onChange={(event) => change(index, "name", event.target.value)} placeholder="字段名" />{includeLabel && <input aria-label={`${title}标签`} value={field.label || ""} onChange={(event) => change(index, "label", event.target.value)} placeholder="展示标签" />}<input aria-label={`${title}路径`} value={field.path} onChange={(event) => change(index, "path", event.target.value)} placeholder="/field" /><button type="button" onClick={() => onChange(fields.filter((_, position) => position !== index))}>删除</button></div>)}</section>;
}

function FormFieldList({ fields, onChange }: { fields: Array<{ name: string; label: string; type: string; required: boolean; minimum?: number; maximum?: number; minLength?: number; maxLength?: number; enum?: unknown[] }>; onChange: (value: typeof fields) => void }) {
  const change = (index: number, patch: Record<string, unknown>) => onChange(fields.map((item, position) => position === index ? { ...item, ...patch } : item));
  return <section className="field-editor-list form-fields"><header><div><b>表单字段</b><small>字段值将加密保存</small></div><button type="button" onClick={() => onChange([...fields, { name: `field_${fields.length + 1}`, label: "参数", type: "string", required: true }])}>＋ 添加</button></header>{fields.map((field, index) => <article key={index}><div className="field-row three"><input aria-label="字段名" value={field.name} onChange={(event) => change(index, { name: event.target.value })} placeholder="customer_id" /><input aria-label="字段标签" value={field.label} onChange={(event) => change(index, { label: event.target.value })} placeholder="客户编号" /><select aria-label="字段类型" value={field.type} onChange={(event) => change(index, { type: event.target.value })}><option value="string">字符串</option><option value="integer">整数</option><option value="number">数字</option><option value="boolean">布尔值</option></select><button type="button" onClick={() => onChange(fields.filter((_, position) => position !== index))}>删除</button></div><label className="check-row"><input type="checkbox" checked={field.required} onChange={(event) => change(index, { required: event.target.checked })} />必填</label>{["integer", "number"].includes(field.type) && <div className="constraint-row"><input type="number" aria-label="最小值" placeholder="最小值" value={field.minimum ?? ""} onChange={(event) => change(index, { minimum: event.target.value === "" ? undefined : Number(event.target.value) })} /><input type="number" aria-label="最大值" placeholder="最大值" value={field.maximum ?? ""} onChange={(event) => change(index, { maximum: event.target.value === "" ? undefined : Number(event.target.value) })} /></div>}{field.type === "string" && <div className="constraint-row"><input type="number" aria-label="最短长度" placeholder="最短长度" value={field.minLength ?? ""} onChange={(event) => change(index, { minLength: event.target.value === "" ? undefined : Number(event.target.value) })} /><input type="number" aria-label="最长长度" placeholder="最长长度" value={field.maxLength ?? ""} onChange={(event) => change(index, { maxLength: event.target.value === "" ? undefined : Number(event.target.value) })} /><input aria-label="枚举值" placeholder="枚举值，逗号分隔" value={(field.enum || []).join(",")} onChange={(event) => change(index, { enum: event.target.value.split(",").map((value) => value.trim()).filter(Boolean) })} /></div>}</article>)}</section>;
}

function EdgeInspector({
  edge,
  sourceType,
  patch,
  remove,
}: {
  edge: Edge;
  sourceType: string;
  patch: (value: {
    label?: string;
    default?: boolean;
    condition?: Record<string, unknown>;
  }) => void;
  remove: () => void;
}) {
  const data = edge.data || {},
    condition = (data.condition || {}) as Record<string, unknown>,
    isCondition = sourceType === "condition";
  const setRule = (key: string, value: unknown) =>
    patch({ condition: { ...condition, [key]: value } });
  return (
    <div>
      <p className="overline">EDGE CONFIG</p>
      <div className="inspector-title">
        <h3>连接规则</h3>
        <span>
          {edge.source} → {edge.target}
        </span>
      </div>
      <TextInput
        id={`${edge.id}-label`}
        labelText="分支名称"
        value={String(edge.label || "")}
        onChange={(event) => patch({ label: event.target.value })}
      />
      {isCondition ? (
        <>
          <label className="check-row">
            <input
              type="checkbox"
              checked={Boolean(data.default)}
              onChange={(event) =>
                patch({
                  default: event.target.checked,
                  condition: event.target.checked ? undefined : condition,
                })
              }
            />
            设为默认分支
          </label>
          {!data.default && (
            <div className="rule-form">
              <TextInput
                id={`${edge.id}-path`}
                labelText="结果路径 · JSON Pointer"
                value={String(condition.path || "")}
                onChange={(event) => setRule("path", event.target.value)}
                placeholder="/nodes/sql-1/output/data/0/status"
              />
              <label>
                运算符
                <select
                  value={String(condition.op || "eq")}
                  onChange={(event) => setRule("op", event.target.value)}
                >
                  <option value="eq">等于</option>
                  <option value="ne">不等于</option>
                  <option value="gt">大于</option>
                  <option value="gte">大于等于</option>
                  <option value="lt">小于</option>
                  <option value="lte">小于等于</option>
                  <option value="in">属于集合</option>
                  <option value="contains">包含</option>
                  <option value="exists">存在</option>
                  <option value="empty">为空</option>
                </select>
              </label>
              <TextInput
                id={`${edge.id}-value`}
                labelText="比较值"
                value={
                  typeof condition.value === "string"
                    ? condition.value
                    : JSON.stringify(condition.value ?? "")
                }
                onChange={(event) => {
                  let value: unknown = event.target.value;
                  try {
                    value = JSON.parse(event.target.value);
                  } catch {
                    /* string */
                  }
                  setRule("value", value);
                }}
                helperText="字符串直接填写；数组或数字可填写 JSON。"
              />
            </div>
          )}
        </>
      ) : (
        <p className="condition-help">普通连线表示上游成功后进入下一个节点。</p>
      )}
      <div className="danger-zone">
        <Button size="sm" kind="danger--ghost" onClick={remove}>
          删除连线
        </Button>
      </div>
    </div>
  );
}
