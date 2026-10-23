from jsonschema import Draft202012Validator
from fastapi.testclient import TestClient
import pytest

from app.main import app


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr("app.main.settings.runtime_service_token", "runtime-test")
    with TestClient(app) as http:
        yield http


def test_published_json_schemas_keep_workflow_and_node_constraints(client):
    headers = {"Authorization": "Bearer runtime-test"}
    response = client.get("/internal/v1/runtime/schemas", headers=headers)
    assert response.status_code == 200
    document = response.json()
    assert set(document["schemas"]) == {"workflowDefinition", "dispatch", "extraction"}
    for schema in document["schemas"].values():
        Draft202012Validator.check_schema(schema)
    node_types = document["schemas"]["workflowDefinition"]["$defs"]["WorkflowNode"]["properties"]["type"]["enum"]
    assert set(node_types) == {"sql_read", "condition", "hitl_form", "end"}
    assert client.get("/internal/v1/runtime/schemas/dispatch", headers=headers).status_code == 200
    assert client.get("/internal/v1/runtime/schemas/singleNode", headers=headers).status_code == 404
    for node in document["nodes"]:
        assert "studioEnabled" not in node and "allowSingleNodeDebug" not in node
        assert not {"debugConfig", "debugInputs", "debugFixture"}.intersection(node.get("uiSchema", {}))
