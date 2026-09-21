from __future__ import annotations

import re
from collections import deque
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator
from sqlglot import exp, parse, parse_one

from app.runtime import NODE_REGISTRY


NODE_ID = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,119}$")
PLACEHOLDER = re.compile(r"\{\{([A-Za-z_][A-Za-z0-9_]*)\}\}")
READ_ROOTS = (exp.Select, exp.Show, exp.Describe, exp.Union)
FORBIDDEN_NODES = (exp.Insert, exp.Update, exp.Delete, exp.Create, exp.Drop, exp.Alter)


class InputSource(BaseModel):
    kind: Literal["RUN_INPUT", "NODE_OUTPUT", "LITERAL"]
    key: str | None = None
    nodeId: str | None = None
    jsonPointer: str | None = None
    value: Any = None


class NodeInput(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    type: Literal["string", "number", "integer", "boolean", "object", "array"] = "string"
    description: str = Field(default="", max_length=500)
    source: InputSource
    required: bool = True


class WorkflowNode(BaseModel):
    id: str
    type: str = Field(min_length=1, max_length=80)
    schemaVersion: int = Field(default=1, ge=1)
    handlerVersion: str | None = Field(default=None, max_length=40)
    title: str = Field(min_length=1, max_length=200)
    config: dict[str, Any] = Field(default_factory=dict)
    inputs: list[NodeInput] = Field(default_factory=list)
    approvalPolicy: Literal["NONE", "PLAN", "NODE"] = "PLAN"
    timeoutSeconds: int = Field(default=600, ge=1, le=7200)
    uiPosition: dict[str, float] | None = None


class WorkflowEdge(BaseModel):
    id: str
    source: str
    target: str
    kind: Literal["NORMAL", "CONDITION", "REFINEMENT"] = "NORMAL"
    label: str = ""
    condition: dict[str, Any] | None = None
    default: bool = False
    maxIterations: int | None = Field(default=None, ge=1, le=5)
    feedbackInputName: str | None = Field(default=None, pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")


class WorkflowDefinition(BaseModel):
    schemaVersion: Literal[1, 2] = 1
    catalogDigest: str | None = None
    entryNodeId: str
    nodes: list[WorkflowNode] = Field(min_length=1, max_length=500)
    edges: list[WorkflowEdge] = Field(default_factory=list, max_length=1000)

    @model_validator(mode="after")
    def validate_graph(self):
        ids = [node.id for node in self.nodes]
        if len(ids) != len(set(ids)) or any(not NODE_ID.fullmatch(item) for item in ids):
            raise ValueError("节点 ID 必须唯一且格式合法")
        if self.entryNodeId not in ids:
            raise ValueError("entryNodeId 不存在")
        node_map = {node.id: node for node in self.nodes}
        outgoing: dict[str, list[WorkflowEdge]] = {item: [] for item in ids}
        refinement_edges: list[WorkflowEdge] = []
        indegree = {item: 0 for item in ids}
        for edge in self.edges:
            if edge.source not in node_map or edge.target not in node_map or edge.source == edge.target:
                raise ValueError(f"边 {edge.id} 引用了无效节点")
            if edge.kind == "REFINEMENT":
                refinement_edges.append(edge)
            else:
                outgoing[edge.source].append(edge)
                indegree[edge.target] += 1
        for node in self.nodes:
            if node.type == "condition":
                edges = outgoing[node.id]
                if not edges or sum(1 for edge in edges if edge.default) != 1:
                    raise ValueError(f"条件节点 {node.id} 必须且只能有一条默认边")
                for edge in edges:
                    if not edge.default and not edge.condition:
                        raise ValueError(f"条件节点 {node.id} 的非默认边必须配置条件")
                    if edge.condition:
                        self._validate_condition(edge.condition)
            elif len(outgoing[node.id]) > 1:
                raise ValueError(f"节点 {node.id} 第一版不允许并行扇出")
            self._validate_node(node, node_map)
        queue = deque([item for item, degree in indegree.items() if degree == 0])
        visited: list[str] = []
        while queue:
            current = queue.popleft()
            visited.append(current)
            for edge in outgoing[current]:
                indegree[edge.target] -= 1
                if indegree[edge.target] == 0:
                    queue.append(edge.target)
        if len(visited) != len(ids):
            raise ValueError("普通控制边不允许循环")
        reachable = {self.entryNodeId}
        queue = deque([self.entryNodeId])
        while queue:
            for edge in outgoing[queue.popleft()]:
                if edge.target not in reachable:
                    reachable.add(edge.target)
                    queue.append(edge.target)
        if reachable != set(ids):
            raise ValueError("工作流包含不可达节点")
        def can_reach(source: str, target: str) -> bool:
            pending, seen = [source], {source}
            while pending:
                current = pending.pop()
                for edge in outgoing[current]:
                    if edge.target == target:
                        return True
                    if edge.target not in seen:
                        seen.add(edge.target)
                        pending.append(edge.target)
            return False
        for node in self.nodes:
            for item in node.inputs:
                if item.source.kind == "NODE_OUTPUT" and not can_reach(str(item.source.nodeId), node.id):
                    raise ValueError(f"节点 {node.id} 只能绑定其前置节点输出")
        for edge in refinement_edges:
            source, target = node_map[edge.source], node_map[edge.target]
            if source.type != "hitl_select" or target.type != "llm_extract":
                raise ValueError(f"REFINEMENT边 {edge.id} 只能从hitl_select指向llm_extract")
            if not can_reach(target.id, source.id):
                raise ValueError(f"REFINEMENT边 {edge.id} 的目标必须是HITL上游节点")
            if edge.maxIterations is None or not edge.feedbackInputName:
                raise ValueError(f"REFINEMENT边 {edge.id} 缺少maxIterations或feedbackInputName")
            feedback_input = next((item for item in target.inputs if item.source.kind == "RUN_INPUT" and item.source.key == edge.feedbackInputName), None)
            if not feedback_input or feedback_input.required:
                raise ValueError(f"REFINEMENT边 {edge.id} 要求目标LLM存在同名可选RUN_INPUT")
            if sum(1 for item in refinement_edges if item.source == source.id) != 1:
                raise ValueError(f"HITL节点 {source.id} 只能有一条REFINEMENT边")
        return self

    @classmethod
    def _validate_condition(cls, rule: dict[str, Any]) -> None:
        if not isinstance(rule, dict):
            raise ValueError("条件必须是对象")
        groups = [key for key in ("all", "any", "not") if key in rule]
        if groups:
            if len(groups) != 1:
                raise ValueError("条件组合只能使用 all、any、not 之一")
            value = rule[groups[0]]
            children = [value] if groups[0] == "not" else value
            if not isinstance(children, list) or not children:
                raise ValueError("条件组合不能为空")
            for child in children:
                cls._validate_condition(child)
            return
        if rule.get("op") not in {"eq", "ne", "gt", "gte", "lt", "lte", "in", "contains", "exists", "empty"} or not str(rule.get("path") or "").startswith("/"):
            raise ValueError("条件操作符或 JSON Pointer 无效")

    @staticmethod
    def _validate_node(node: WorkflowNode, nodes: dict[str, WorkflowNode]) -> None:
        manifest = NODE_REGISTRY.get(node.type, node.schemaVersion).manifest
        names = {item.name for item in node.inputs}
        if len(names) != len(node.inputs):
            raise ValueError(f"节点 {node.id} 输入名称重复")
        for item in node.inputs:
            if item.source.kind == "NODE_OUTPUT":
                if item.source.nodeId not in nodes or not str(item.source.jsonPointer or "").startswith("/"):
                    raise ValueError(f"节点 {node.id} 输出绑定无效")
            if item.source.kind == "RUN_INPUT" and not item.source.key:
                raise ValueError(f"节点 {node.id} 运行输入缺少 key")
        if node.type == "sql_read":
            if any(item.type not in manifest.input_types for item in node.inputs):
                raise ValueError(f"节点 {node.id} 包含 SQL 不支持的输入类型")
            database = str(node.config.get("databaseRef") or "").strip()
            sql = str(node.config.get("sqlTemplate") or "").strip()
            if not database or len(database) > 1000:
                raise ValueError(f"节点 {node.id} 数据库路径无效")
            placeholders = set(PLACEHOLDER.findall(sql))
            if placeholders != names:
                raise ValueError(f"节点 {node.id} SQL 占位符必须与 inputs 完全一致")
            rendered = PLACEHOLDER.sub("0", sql)
            if len(parse(rendered, read="mysql")) != 1:
                raise ValueError(f"节点 {node.id} 只能包含一条 SQL")
            expression = parse_one(rendered, read="mysql")
            if not isinstance(expression, READ_ROOTS) or any(expression.find_all(FORBIDDEN_NODES)):
                raise ValueError(f"节点 {node.id} 不是只读 SQL")
        NODE_REGISTRY.validate_node(node.model_dump(mode="json"))


def legacy_steps_to_workflow(steps: list[dict[str, Any]]) -> WorkflowDefinition:
    if not steps:
        raise ValueError("旧版经验没有步骤")
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    positions = {str(step.get("id") or f"step-{index}"): f"sql-{index}" for index, step in enumerate(steps, start=1)}
    for index, step in enumerate(steps, start=1):
        inputs = []
        bindings = {item.get("parameter"): item for item in step.get("resultBindings", [])}
        for parameter in step.get("parameters", []):
            name = str(parameter.get("name"))
            binding = bindings.get(name)
            if binding:
                source = {"kind": "NODE_OUTPUT", "nodeId": positions.get(str(binding.get("sourceStepId"))), "jsonPointer": binding.get("jsonPointer")}
            else:
                source = {"kind": "RUN_INPUT", "key": name}
            inputs.append({"name": name, "type": parameter.get("type", "string"), "description": parameter.get("description", ""), "source": source})
        nodes.append({"id": f"sql-{index}", "type": "sql_read", "title": step.get("title") or f"SQL 查询 {index}", "config": {"databaseRef": step.get("databaseRef"), "sqlTemplate": step.get("sqlTemplate")}, "inputs": inputs, "approvalPolicy": "PLAN"})
        if index > 1:
            edges.append({"id": f"edge-{index-1}-{index}", "source": f"sql-{index-1}", "target": f"sql-{index}"})
    return WorkflowDefinition.model_validate({"entryNodeId": "sql-1", "nodes": nodes, "edges": edges})
