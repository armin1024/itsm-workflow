import pytest

from app.conditions import evaluate, pointer
from app.workflow import WorkflowDefinition, legacy_steps_to_workflow


def sql_node(node_id="sql-1"):
    return {
        "id": node_id,
        "type": "sql_read",
        "title": "查询客户",
        "config": {"databaseRef": "1/db/db/read/svc", "sqlTemplate": "SELECT id FROM users WHERE name={{name}}"},
        "inputs": [{"name": "name", "type": "string", "source": {"kind": "RUN_INPUT", "key": "name"}}],
    }


def test_validates_read_workflow_and_rejects_mutation():
    value = WorkflowDefinition.model_validate({"entryNodeId": "sql-1", "nodes": [sql_node()], "edges": []})
    assert value.nodes[0].type == "sql_read"
    bad = sql_node()
    bad["config"]["sqlTemplate"] = "DELETE FROM users WHERE name={{name}}"
    with pytest.raises(ValueError, match="不是只读 SQL"):
        WorkflowDefinition.model_validate({"entryNodeId": "sql-1", "nodes": [bad], "edges": []})


def test_condition_requires_default_and_graph_is_acyclic():
    condition = {"id": "route", "type": "condition", "title": "判断状态"}
    end = {"id": "done", "type": "end", "title": "完成"}
    with pytest.raises(ValueError, match="默认边"):
        WorkflowDefinition.model_validate({"entryNodeId": "route", "nodes": [condition, end], "edges": [{"id": "e", "source": "route", "target": "done", "condition": {"path": "/inputs/x", "op": "eq", "value": 1}}]})
    with pytest.raises(ValueError, match="不允许循环"):
        WorkflowDefinition.model_validate({"entryNodeId": "route", "nodes": [condition, end], "edges": [{"id": "e1", "source": "route", "target": "done", "default": True}, {"id": "e2", "source": "done", "target": "route"}]})


def test_legacy_steps_become_linear_graph_with_binding():
    graph = legacy_steps_to_workflow([
        {"id": "step-1", "title": "A", "databaseRef": "1/db/db/read/svc", "sqlTemplate": "SELECT id FROM users", "parameters": []},
        {"id": "step-2", "title": "B", "databaseRef": "1/db/db/read/svc", "sqlTemplate": "SELECT * FROM users WHERE id={{user_id}}", "parameters": [{"name": "user_id", "type": "integer"}], "resultBindings": [{"parameter": "user_id", "sourceStepId": "step-1", "jsonPointer": "/data/0/id"}]},
    ])
    assert [node.id for node in graph.nodes] == ["sql-1", "sql-2"]
    assert graph.nodes[1].inputs[0].source.nodeId == "sql-1"
    assert graph.edges[0].source == "sql-1" and graph.edges[0].target == "sql-2"


def test_condition_evaluator_and_json_pointer():
    state = {"nodes": {"sql-1": {"output": {"data": [{"status": "ACTIVE", "count": 2}]}}}}
    assert pointer(state, "/nodes/sql-1/output/data/0/status") == "ACTIVE"
    assert evaluate({"all": [{"path": "/nodes/sql-1/output/data/0/status", "op": "eq", "value": "ACTIVE"}, {"path": "/nodes/sql-1/output/data/0/count", "op": "gte", "value": 2}]}, state)


def test_condition_branch_and_data_binding_are_independent():
    first = sql_node("sql-1")
    route = {"id": "route", "type": "condition", "title": "判断状态"}
    active = {"id": "sql-active", "type": "sql_read", "title": "查询详情", "config": {"databaseRef": "1/db/db/read/svc", "sqlTemplate": "SELECT * FROM users WHERE id={{customer_id}}"}, "inputs": [{"name": "customer_id", "type": "integer", "source": {"kind": "NODE_OUTPUT", "nodeId": "sql-1", "jsonPointer": "/data/0/id"}}]}
    done = {"id": "done", "type": "end", "title": "完成"}
    graph = WorkflowDefinition.model_validate({"entryNodeId": "sql-1", "nodes": [first, route, active, done], "edges": [{"id": "e1", "source": "sql-1", "target": "route"}, {"id": "e2", "source": "route", "target": "sql-active", "condition": {"path": "/nodes/sql-1/output/data/0/status", "op": "eq", "value": "ACTIVE"}}, {"id": "e3", "source": "route", "target": "done", "default": True}, {"id": "e4", "source": "sql-active", "target": "done"}]})
    assert graph.nodes[2].inputs[0].source.nodeId == "sql-1"
    assert graph.edges[1].condition["op"] == "eq"
