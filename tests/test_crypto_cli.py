import json
import os

import pytest

from app import cli
from app.cli import CliExecutionError, parse_sql_read_output
from app.crypto import SecretBox


def test_secret_box_never_contains_plaintext():
    box = SecretBox(b"1" * 32)
    encrypted = box.seal({"apiKey": "secret-value"}, purpose="test")
    assert b"secret-value" not in encrypted
    assert box.open(encrypted, purpose="test") == {"apiKey": "secret-value"}
    with pytest.raises(Exception):
        box.open(encrypted, purpose="wrong")


@pytest.mark.asyncio
async def test_cli_adapter_uses_argv_and_validates_business_status(tmp_path, monkeypatch):
    executable = tmp_path / "aops-cli"
    executable.write_text("""#!/usr/bin/env python3
import json, os, sys
if '--version' in sys.argv:
    print('aops-cli test-1')
else:
    print(json.dumps({'status': 0, 'data': {'argv': sys.argv[1:], 'hasKey': bool(os.environ.get('AOPS_API_KEY'))}}))
""")
    executable.chmod(0o755)
    monkeypatch.setattr(cli.settings, "aops_cli_path", executable)
    monkeypatch.setattr(cli.settings, "aops_base_url", "http://aops")
    result = await cli.execute_sql_read(database_ref="1/db/db/read/svc", sql="SELECT 1", ticket_id=123, api_key="secret-value", timeout_seconds=5)
    assert result.payload["status"] == 0
    assert result.payload["data"]["hasKey"] is True
    assert "#uatu-123" in result.payload["data"]["argv"]
    assert "secret-value" not in result.stdout.decode()


@pytest.mark.asyncio
async def test_cli_adapter_rejects_status_failure(tmp_path, monkeypatch):
    executable = tmp_path / "aops-cli"
    executable.write_text("#!/usr/bin/env python3\nimport sys\nprint('v1' if '--version' in sys.argv else '{\"status\":1}')\n")
    executable.chmod(0o755)
    monkeypatch.setattr(cli.settings, "aops_cli_path", executable)
    with pytest.raises(CliExecutionError, match="失败状态") as error:
        await cli.execute_sql_read(database_ref="db", sql="SELECT 1", ticket_id=1, api_key="key", timeout_seconds=5)
    assert error.value.code == "AOPS_BUSINESS_ERROR"


@pytest.mark.asyncio
async def test_nonzero_exit_reports_sse_or_stderr_reason_and_redacts_secret(tmp_path, monkeypatch):
    executable = tmp_path / "aops-cli"
    executable.write_text("""#!/usr/bin/env python3
import sys
if '--version' in sys.argv:
    print('aops-cli diagnostic-test')
else:
    print('event:error')
    print('data:数据库路径不存在')
    print('Authorization: Bearer should-never-leak', file=sys.stderr)
    raise SystemExit(7)
""")
    executable.chmod(0o755)
    monkeypatch.setattr(cli.settings, "aops_cli_path", executable)
    with pytest.raises(CliExecutionError, match="数据库路径不存在") as error:
        await cli.execute_sql_read(database_ref="db", sql="SELECT 1", ticket_id=1, api_key="key", timeout_seconds=5)
    assert error.value.exit_code == 7
    diagnostic, _ = cli.redact_diagnostic(error.value.stderr, 1024)
    assert "should-never-leak" not in diagnostic and "Bearer ***" in diagnostic


def test_sql_read_sse_is_mapped_to_rows_for_json_pointer_binding():
    output = b'''event:open\nretry:86400000\ndata:<nil>\n\nevent:uuid\ndata:e6910020-b47e-41f5-8941-17a486999887\n\nevent:title\ndata:["user_id"]\n\nevent:fieldtype\ndata:[{"comment":"user id","name":"user_id","type":"varchar(64)"}]\n\nevent:message\ndata:["000244"]\n\nevent:done\ndata:!ok\n'''
    payload = parse_sql_read_output(output)
    assert payload["status"] == 0
    assert payload["data"] == [{"user_id": "000244"}]
    assert payload["stream"]["uuid"] == "e6910020-b47e-41f5-8941-17a486999887"


def test_sql_read_sse_requires_successful_done_event():
    with pytest.raises(CliExecutionError) as missing:
        parse_sql_read_output(b'event:title\ndata:["id"]\n')
    assert missing.value.code == "INVALID_SSE"
    with pytest.raises(CliExecutionError) as failed:
        parse_sql_read_output(b'event:done\ndata:!error\n')
    assert failed.value.code == "AOPS_BUSINESS_ERROR"
