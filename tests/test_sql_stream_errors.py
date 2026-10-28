import json
import sys

import pytest

from app import cli
from app.cli import CliExecutionError, parse_sql_read_output
from app.runtime.planner import validate_workflow
from app.runtime.push_executor import PushExecutor


@pytest.mark.parametrize("event", ["error", "failed", "failure", "ERROR"])
@pytest.mark.parametrize("done", ["", "\n\nevent:done\ndata:!ok\n"])
def test_explicit_stream_error_takes_priority_over_missing_or_successful_done(event, done):
    message = json.dumps({"error": {"message": "数据库无访问权限"}, "token": "do-not-copy-extra-fields"}, ensure_ascii=False)
    wire = f"event:open\ndata:<nil>\n\nevent:{event}\ndata:{message}{done}"
    with pytest.raises(CliExecutionError) as error:
        parse_sql_read_output(wire.encode())
    assert error.value.code == "AOPS_BUSINESS_ERROR"
    assert "数据库无访问权限" in str(error.value)
    assert "do-not-copy-extra-fields" not in str(error.value)
    assert "缺少" not in str(error.value)


@pytest.mark.parametrize("wire", [b"<html><title>502 Bad Gateway</title></html>", b"401 Unauthorized", b"[1,2]", b"\x1b[31mnetwork failed\x1b[0m"])
def test_non_sse_response_is_not_reported_as_missing_done(wire):
    with pytest.raises(CliExecutionError) as error:
        parse_sql_read_output(wire)
    assert error.value.code == "NON_SSE_RESPONSE"
    assert "缺少done" not in str(error.value)


def test_incomplete_rows_never_become_success_or_automatic_retry():
    with pytest.raises(CliExecutionError) as error:
        parse_sql_read_output(b'event:title\ndata:["id"]\n\nevent:message\ndata:[42]\n')
    assert error.value.code == "INVALID_SSE"
    assert "结果可能不完整" in str(error.value)
    assert "勿自动重试" in str(error.value)


def test_upstream_json_error_message_is_preserved_and_credentials_redacted():
    wire = json.dumps({"status": 403, "message": "权限拒绝 Authorization: Bearer short"}).encode()
    with pytest.raises(CliExecutionError) as error:
        parse_sql_read_output(wire)
    assert error.value.code == "AOPS_BUSINESS_ERROR"
    assert "403" in str(error.value) and "权限拒绝" in str(error.value)
    assert "short" not in str(error.value)
    with pytest.raises(CliExecutionError):
        parse_sql_read_output(b'{"status":false,"data":[]}')


def test_success_empty_result_crlf_and_no_final_separator_still_work():
    result = parse_sql_read_output(b'\xef\xbb\xbfevent:open\r\ndata:<nil>\r\n\r\nevent:title\r\ndata:["id"]\r\n\r\nevent:done\r\ndata:!ok')
    assert result["rowCount"] == 0 and result["data"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("response,expected", [
    ('event:error\ndata:{"message":"permission denied"}\n', "AOPS_BUSINESS_ERROR"),
    ("<html>bad gateway</html>", "NON_SSE_RESPONSE"),
    ('event:title\ndata:["id"]\n\nevent:message\ndata:[1]\n', "INVALID_SSE"),
])
async def test_exit_zero_failures_keep_diagnostics_and_stop_following_sql(tmp_path, monkeypatch, response, expected):
    executable = tmp_path / "aops-cli"
    trace = tmp_path / "calls"
    executable.write_text(f'''#!{sys.executable}
import sys
from pathlib import Path
if "--version" in sys.argv:
    print("fixture-cli")
else:
    p=Path({str(trace)!r})
    p.write_text(p.read_text()+"call\\n" if p.exists() else "call\\n")
    print({response!r})
''')
    executable.chmod(0o755)
    monkeypatch.setattr(cli.settings, "aops_cli_path", executable)
    monkeypatch.setattr(cli.settings, "aops_cli_version_requirement", "")
    nodes = [{"id": name, "type": "sql_read", "title": name, "config": {"databaseRef": "fixture/db", "sqlTemplate": "SELECT 1"}} for name in ("a", "b")]
    flow = validate_workflow({"entryNodeId": "a", "nodes": nodes, "edges": [{"id": "next", "source": "a", "target": "b"}]}, "EXECUTE")
    events = []
    async def sink(event):
        events.append(event)
    executor = PushExecutor()
    payload = {"dispatchId": "stream-error", "runId": "stream-error", "ticketId": 1, "workflowContentHash": flow["workflowContentHash"], "workflowSnapshot": flow["normalizedDefinition"]}
    executor.dispatch(payload, "fixture-secret", event_sink=sink, sync_tec01=False)
    await executor.active["stream-error"].task
    completed = next(event for event in events if event["type"] == "NODE_COMPLETED")
    assert completed["status"] == "FAILED" and completed["result"] is None
    assert completed["diagnostic"]["errorCode"] == expected
    assert completed["diagnostic"]["exitCode"] == 0
    assert completed["diagnostic"]["stdout"]
    assert trace.read_text().splitlines() == ["call"]
    assert events[-1]["status"] == "FAILED" and not executor.active
