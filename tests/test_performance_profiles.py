from pathlib import Path

import pytest

from app.config import Settings
from tools.benchmark_runtime import PROFILES, arguments, benchmark, sse_fixture, summary, write_report


def test_performance_defaults_and_single_worker_service():
    value = Settings(_env_file=None)
    assert (value.executor_max_active_runs, value.executor_max_active_cli, value.cpu_max_workers, value.compiler_max_active_jobs) == (8, 2, 1, 1)
    assert value.executor_run_memory_mb == 256
    assert value.runtime_data_budget_mb == 2048
    root = Path(__file__).parents[1]
    service = (root / "packaging/itsm-workflow-api.service.in").read_text()
    assert "--workers 1" in service
    assert "WEB_CONCURRENCY" not in service
    for obsolete in ("worker", "compiler", "mcp", "migrate"):
        assert not (root / f"packaging/itsm-workflow-{obsolete}.service.in").exists()


@pytest.mark.parametrize("name", PROFILES)
def test_profiles_match_benchmark_and_validate(name):
    root = Path(__file__).parents[1]
    path = root / "packaging/profiles" / f"{name}.env"
    fields = {}
    for line in path.read_text().splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            key, value = stripped.split("=", 1)
            fields[key.lower()] = int(value)
    for key, expected in PROFILES[name].items():
        assert fields[key] == expected, (name, key)
    validated = Settings(_env_file=None, **fields)
    assert validated.cpu_max_workers <= 2
    assert validated.executor_max_active_cli <= validated.executor_max_active_runs
    assert validated.tec01_callback_max_attempts == 6
    assert validated.tec01_callback_wait_seconds == 70


def test_benchmark_is_safe_by_default_and_large_is_opt_in():
    defaults = arguments([])
    assert defaults.profile == "4c8g" and defaults.runs == 4
    assert defaults.target_p95_ms == 250 and defaults.output is None
    assert not defaults.large
    assert len(sse_fixture()) < 64 * 1024
    with pytest.raises(SystemExit):
        arguments(["--scenario", "large"])
    enabled = arguments(["--scenario", "large", "--large"])
    assert enabled.large


def test_latency_summary_reports_actual_distribution():
    assert summary([])["p95Ms"] is None
    value = summary([1, 2, 3, 4, 100])
    assert value == {"count": 5, "p50Ms": 3.0, "p95Ms": 80.8, "maxMs": 100}


def test_benchmark_report_is_optional_exclusive_and_never_overwrites(tmp_path):
    path = tmp_path / "report.json"
    options = arguments(["--output", str(path)])
    assert options.output == path and not path.exists()
    assert write_report({"测量": 1}) == '{\n  "测量": 1\n}'
    write_report({"measured": 1}, path)
    original = path.read_text()
    with pytest.raises(FileExistsError):
        write_report({"measured": 2}, path)
    assert path.read_text() == original
    with pytest.raises(SystemExit):
        arguments(["--output", str(path)])


@pytest.mark.asyncio
async def test_benchmark_smoke_uses_mock_cli_health_and_returns_budget():
    report = await benchmark(arguments(["--scenario", "normal", "--runs", "1"]))
    assert report["measurement"]["cliHealth"].startswith("mock")
    assert report["scenarios"][0]["accepted"] == 1
    assert report["checks"]["noExternalNetworkOrSubprocess"]
    assert report["checks"]["budgetReturned"]
    assert report["latency"]["health"]["count"] > 0
