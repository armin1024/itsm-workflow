import pytest

from app.hitl import HitlError, build_candidates, validate_form
from app.workflow import WorkflowDefinition


def select_node():
    return {
        "id": "choose", "type": "hitl_select", "title": "选择客户",
        "config": {"title": "选择客户", "selectionMode": "SINGLE", "idPath": "/customer_id", "labelTemplate": "{{customer_name}} / {{customer_id}}", "displayFields": [{"name": "customer_name", "label": "姓名", "path": "/customer_name"}, {"name": "customer_id", "label": "编号", "path": "/customer_id"}], "outputFields": [{"name": "customer_id", "path": "/customer_id"}], "minimumSelections": 1, "maximumSelections": 1},
        "inputs": [{"name": "rows", "type": "array", "source": {"kind": "NODE_OUTPUT", "nodeId": "sql-1", "jsonPointer": "/data"}}],
    }


def test_candidate_mapping_exposes_only_display_and_declared_output():
    rows = [{"customer_id": "C001", "customer_name": "王五", "secret": "hidden"}, {"customer_id": "C002", "customer_name": "王五", "secret": "hidden-2"}]
    candidates = build_candidates(select_node(), rows)
    assert candidates[0]["label"] == "王五 / C001"
    assert candidates[0]["display"] == {"customer_name": "王五", "customer_id": "C001"}
    assert candidates[0]["values"] == {"customer_id": "C001"}
    assert candidates[0]["candidateId"] != candidates[1]["candidateId"]
    assert "secret" not in str(candidates)


def test_candidate_limits_and_form_validation():
    with pytest.raises(HitlError) as empty:
        build_candidates(select_node(), [])
    assert empty.value.code == "NO_HITL_CANDIDATES"
    fields = [{"name": "customer_id", "label": "编号", "type": "string", "required": True, "minLength": 2}, {"name": "limit", "label": "条数", "type": "integer", "required": True, "minimum": 1, "maximum": 100}]
    assert validate_form(fields, {"customer_id": "C1", "limit": 20})["limit"] == 20
    with pytest.raises(HitlError):
        validate_form(fields, {"customer_id": "", "limit": 0, "extra": True})


def test_workflow_rejects_hitl_without_reachable_node_output_binding():
    node = select_node()
    node["inputs"][0]["source"] = {"kind": "RUN_INPUT", "key": "rows"}
    with pytest.raises(ValueError, match="绑定前置节点输出"):
        WorkflowDefinition.model_validate({"entryNodeId": "choose", "nodes": [node], "edges": []})
