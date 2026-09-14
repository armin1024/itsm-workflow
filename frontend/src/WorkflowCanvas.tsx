import { memo, useEffect, useMemo, useState } from "react";
import dagre from "@dagrejs/dagre";
import {
  Background,
  Controls,
  Handle,
  MarkerType,
  MiniMap,
  Position,
  ReactFlow,
  useUpdateNodeInternals,
  type Edge,
  type Node,
  type NodeProps,
} from "@xyflow/react";
import type { NodeStatus, WorkflowEdge, WorkflowNode } from "./types";

type CardData = {
  node: WorkflowNode;
  status: NodeStatus;
  [key: string]: unknown;
};

const labels: Record<string, string> = {
  sql_read: "SQL READ",
  condition: "CONDITION",
  human_input: "INPUT",
  approval: "APPROVAL",
  end: "END",
};

const NodeCard = memo(({ id, data, selected }: NodeProps<Node<CardData>>) => {
  const { node, status } = data;
  const updateNodeInternals = useUpdateNodeInternals();
  useEffect(() => {
    const frame = requestAnimationFrame(() => updateNodeInternals(id));
    return () => cancelAnimationFrame(frame);
  }, [
    id,
    node.type,
    node.title,
    node.inputs.length,
    status,
    updateNodeInternals,
  ]);
  return (
    <article
      className={`flow-node status-${status.toLowerCase()} ${selected ? "selected" : ""}`}
    >
      <Handle
        id="in"
        type="target"
        position={Position.Top}
        isConnectable={false}
      />
      <div className="node-kicker">
        <span>{labels[node.type] || node.type}</span>
        <i aria-label={status}>{status}</i>
      </div>
      <h3>{node.title}</h3>
      {node.type === "sql_read" && (
        <p>{String(node.config.databaseRef || "未配置数据库")}</p>
      )}
      <div className="node-foot">
        <span>{node.inputs.length} 个输入</span>
        <span>{node.timeoutSeconds}s</span>
      </div>
      <Handle
        id="out"
        type="source"
        position={Position.Bottom}
        isConnectable={false}
      />
    </article>
  );
});

const nodeTypes = { workflow: NodeCard };

function layout(
  definition: { nodes: WorkflowNode[]; edges: WorkflowEdge[] },
  statuses: Record<string, NodeStatus>,
) {
  const graph = new dagre.graphlib.Graph().setDefaultEdgeLabel(() => ({}));
  graph.setGraph({
    rankdir: "TB",
    ranksep: 76,
    nodesep: 34,
    marginx: 28,
    marginy: 28,
  });
  definition.nodes.forEach((node) =>
    graph.setNode(node.id, { width: 280, height: 126 }),
  );
  definition.edges.forEach((edge) => graph.setEdge(edge.source, edge.target));
  dagre.layout(graph);
  const nodes: Node<CardData>[] = definition.nodes.map((node) => {
    const point = graph.node(node.id);
    return {
      id: node.id,
      type: "workflow",
      position: { x: point.x - 140, y: point.y - 63 },
      width: 280,
      height: 126,
      style: { width: 280, height: 126 },
      data: { node, status: statuses[node.id] || "PENDING" },
      draggable: false,
      connectable: false,
    };
  });
  const edges: Edge[] = definition.edges.map((edge) => ({
    id: edge.id,
    source: edge.source,
    sourceHandle: "out",
    target: edge.target,
    targetHandle: "in",
    label: edge.default ? "默认" : edge.label,
    type: "bezier",
    markerEnd: {
      type: MarkerType.ArrowClosed,
      color: "#66877f",
      width: 18,
      height: 18,
    },
    animated: statuses[edge.source] === "RUNNING",
    className: edge.default ? "default-edge" : "",
  }));
  return { nodes, edges };
}

export default function WorkflowCanvas({
  definition,
  statuses,
  onSelect,
}: {
  definition: { nodes: WorkflowNode[]; edges: WorkflowEdge[] };
  statuses: Record<string, NodeStatus>;
  onSelect: (id: string) => void;
}) {
  const graph = useMemo(
    () => layout(definition, statuses),
    [definition, statuses],
  );
  const [fullscreen, setFullscreen] = useState(false);
  useEffect(() => {
    const close = (event: KeyboardEvent) => {
      if (event.key === "Escape") setFullscreen(false);
    };
    window.addEventListener("keydown", close);
    return () => window.removeEventListener("keydown", close);
  }, []);
  return (
    <div
      className={`workflow-canvas ${fullscreen ? "is-fullscreen" : ""}`}
      aria-label="工作流执行图"
    >
      <button
        type="button"
        className="canvas-fullscreen"
        onClick={() => setFullscreen((value) => !value)}
      >
        {fullscreen ? "退出全屏" : "全屏查看"}
      </button>
      <ReactFlow
        nodes={graph.nodes}
        edges={graph.edges}
        nodeTypes={nodeTypes}
        onNodeClick={(_, node) => onSelect(node.id)}
        nodesDraggable={false}
        nodesConnectable={false}
        elementsSelectable
        fitView
        fitViewOptions={{ padding: 0.2 }}
        minZoom={0.35}
        maxZoom={1.4}
        proOptions={{ hideAttribution: true }}
      >
        <Background color="#c8d5d2" gap={24} size={1} />
        {definition.nodes.length > 6 && (
          <MiniMap
            pannable
            zoomable
            nodeColor={(node) => {
              const status = (node.data as CardData).status;
              return status === "SUCCEEDED"
                ? "#198038"
                : status === "FAILED"
                  ? "#da1e28"
                  : status === "RUNNING"
                    ? "#007d79"
                    : "#8d8d8d";
            }}
          />
        )}
        <Controls showInteractive={false} />
      </ReactFlow>
    </div>
  );
}
