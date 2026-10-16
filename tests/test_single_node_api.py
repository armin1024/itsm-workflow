import sys

from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator

from app.main import app


def test_published_json_schemas_include_current_nodes_and_constraints(monkeypatch):
    monkeypatch.setattr("app.main.settings.runtime_service_token", "runtime-token")
    with TestClient(app) as client:
        headers = {"Authorization": "Bearer runtime-token"}
        catalog = client.get("/internal/v1/runtime/schemas", headers=headers)
        assert catalog.status_code == 200
        document = catalog.json()
        for schema in document["schemas"].values():
            Draft202012Validator.check_schema(schema)
        node_types = document["schemas"]["workflowDefinition"]["$defs"]["WorkflowNode"]["properties"]["type"]["enum"]
        assert set(node_types) == {"sql_read", "condition", "hitl_form", "end"}
        assert client.get("/internal/v1/runtime/schemas/singleNode", headers=headers).status_code == 200
        assert client.get("/internal/v1/runtime/schemas/unknown", headers=headers).status_code == 404


def test_downstream_single_sql_real_process_idempotency_and_form_resume(tmp_path, monkeypatch):
    executable, trace = tmp_path / "aops-cli", tmp_path / "calls"
    executable.write_text(f'''#!{sys.executable}
import json, os, sys
if '--version' in sys.argv:
    print('single-node-test')
else:
    assert os.environ.get('AOPS_API_KEY')
    with open({str(trace)!r}, 'a') as record:
        record.write(json.dumps(sys.argv[1:])+'\\n')
    print('event:title\\ndata:["id"]\\n\\nevent:message\\ndata:["C001"]\\n\\nevent:done\\ndata:!ok\\n')
''')
    executable.chmod(0o755)
    monkeypatch.setattr("app.cli.settings.aops_cli_path", executable)
    monkeypatch.setattr("app.main.settings.runtime_service_token", "runtime-token")
    headers = {"Authorization": "Bearer runtime-token", "X-AOPS-Api-Key": "temporary-key"}
    body = {"requestId": "one-request", "ticketId": 100173, "node": {"id": "query", "type": "sql_read", "title": "查询", "config": {"databaseRef": "db/test", "sqlTemplate": "SELECT id FROM users WHERE id={{id}}"}, "inputs": [{"name": "id", "type": "string", "source": {"kind": "RUN_INPUT", "key": "id"}}]}, "inputs": {"id": "C001"}}
    with TestClient(app) as client:
        first = client.post("/internal/v1/runtime/nodes/execute", json=body, headers=headers)
        assert first.status_code == 200 and first.json()["status"] == "SUCCEEDED", first.text
        assert "temporary-key" not in first.text
        second = client.post("/internal/v1/runtime/nodes/execute", json=body, headers=headers)
        assert second.json() == first.json()
        assert len(trace.read_text().splitlines()) == 1 and "#uatu-100173" in trace.read_text()
        conflict = client.post("/internal/v1/runtime/nodes/execute", json={**body, "inputs": {"id": "C002"}}, headers=headers)
        assert conflict.status_code == 409
        node = {"id": "form", "type": "hitl_form", "title": "输入编号", "config": {"title": "输入编号", "fields": [{"name": "id", "label": "编号", "type": "string", "required": True}]}}
        waiting = client.post("/internal/v1/runtime/nodes/execute", json={"requestId": "form-open", "node": node}, headers=headers)
        assert waiting.json()["status"] == "WAITING_INPUT" and waiting.json()["interaction"]["kind"] == "HITL_FORM"
        done = client.post("/internal/v1/runtime/nodes/execute", json={"requestId": "form-submit", "node": node, "resumePayload": {"nodeId": "form", "action": "SUBMIT", "values": {"id": "C001"}}}, headers=headers)
        assert done.json()["output"] == {"values": {"id": "C001"}}
