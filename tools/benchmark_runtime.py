"""Safe ASGI benchmark: simulated SQL/LLM, no external network or CLI process.

Run with the project's Python 3.12 environment. A profile changes admission
settings, not host CPU/RAM limits; constrain those outside this script if needed.
Default workloads are small. --large explicitly enables ONE near-20MiB SSE.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import resource
import sys
import tempfile
import time
from collections import Counter, defaultdict
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx

import app.call_logs as log_module
import app.extraction as extraction_module
import app.main as main_module
from app.cli import CliMetadata, parse_sql_read_output
from app.config import settings
from app.resources import cpu_run, cpu_slot, data_budget
from app.runtime.compiler_service import CompilerService
from app.runtime.push_executor import PushExecutor
from app.tec01_client import Tec01Client


PROFILES = {
    "2c4g": {"executor_max_active_runs": 4, "executor_max_active_cli": 1, "cpu_max_workers": 1, "compiler_max_active_jobs": 1, "runtime_data_budget_mb": 1024},
    "4c8g": {"executor_max_active_runs": 8, "executor_max_active_cli": 2, "cpu_max_workers": 1, "compiler_max_active_jobs": 1, "runtime_data_budget_mb": 2048},
    "8c16g": {"executor_max_active_runs": 16, "executor_max_active_cli": 4, "cpu_max_workers": 2, "compiler_max_active_jobs": 2, "runtime_data_budget_mb": 4096},
    "16c32g": {"executor_max_active_runs": 32, "executor_max_active_cli": 8, "cpu_max_workers": 2, "compiler_max_active_jobs": 4, "runtime_data_budget_mb": 8192},
}


def cgroup_limits():
    """Observe externally configured limits; selecting a profile sets none."""
    try:
        quota, period = Path("/sys/fs/cgroup/cpu.max").read_text().strip().split()
        memory = Path("/sys/fs/cgroup/memory.max").read_text().strip()
        return {"available": True, "cpuQuotaCores": None if quota == "max" else round(int(quota) / int(period), 3),
                "memoryLimitMiB": None if memory == "max" else round(int(memory) / 1024**2, 3)}
    except (OSError, ValueError, ZeroDivisionError):
        return {"available": False, "cpuQuotaCores": None, "memoryLimitMiB": None}


def summary(values):
    ordered = sorted(values)
    if not ordered:
        return {"count": 0, "p50Ms": None, "p95Ms": None, "maxMs": None}

    def percentile(fraction):
        position = (len(ordered) - 1) * fraction
        low = int(position)
        high = min(low + 1, len(ordered) - 1)
        return round(ordered[low] + (ordered[high] - ordered[low]) * (position - low), 3)

    return {"count": len(ordered), "p50Ms": percentile(.5), "p95Ms": percentile(.95), "maxMs": round(ordered[-1], 3)}


def sse_fixture(*, large=False):
    header = b'event:title\ndata:["value","padding"]\n\n'
    row = b'event:message\ndata:[1,"' + b'x' * (512 if large else 8) + b'"]\n\n'
    done = b'event:done\ndata:!ok\n\n'
    count = ((20 * 1024 * 1024 - 1024 - len(header) - len(done)) // len(row)) if large else 32
    return header + row * count + done


def workflow(*, large=False):
    first = {"id": "sql-a", "type": "sql_read", "title": "模拟查询", "config": {"databaseRef": "fixture/db", "sqlTemplate": "SELECT 1"}}
    second = {"id": "sql-b", "type": "sql_read", "title": "引用前置结果", "config": {"databaseRef": "fixture/db", "sqlTemplate": "SELECT {{value}}"}, "inputs": [{"name": "value", "type": "integer", "source": {"kind": "NODE_OUTPUT", "nodeId": "sql-a", "jsonPointer": "/data/0/value"}}]}
    nodes = [first] + ([] if large else [second]) + [{"id": "done", "type": "end", "title": "完成"}]
    return {"entryNodeId": "sql-a", "nodes": nodes, "edges": [{"id": f"edge-{index}", "source": node["id"], "target": nodes[index + 1]["id"]} for index, node in enumerate(nodes[:-1])]}


def evidence(count=1):
    details = json.dumps({"sql": {"serverid": 1, "dbid": "fixture", "dbname": "fixture", "dbuser": "readonly", "service_name": "fixture", "command": "SELECT 1"}})
    return {"requestId": "request1", "ticketId": 1, "ticketInfo": {"id": 1, "event_title": "查询模拟信息"}, "auditTimeline": [{"id": f"op-{index}", "operation": "sql_exec_read", "result": '{"r":true,"e":""}', "details": details, "created_at": index} for index in range(count)]}


class _QuietConsole:
    def write(self, _text):
        return None

    def flush(self):
        return None


async def benchmark(args):
    profile = PROFILES[args.profile]
    timings, outcomes, releases, sql_nodes = defaultdict(list), Counter(), {}, []
    model_active = model_peak = 0
    payload_bytes = {"normal": len(sse_fixture())}
    current_sse = sse_fixture()
    lag, probe_delay = [], []
    scenarios = []
    before_budget = data_budget.used

    async def forbidden(*_args, **_kwargs):
        raise AssertionError("Benchmark attempted an external network or subprocess call")

    async def simulated_cli_metadata(*_args, **_kwargs):
        return CliMetadata("benchmark-simulated-cli", "0" * 64)

    async def callbacks(request):
        body = await cpu_run(json.loads, request.content)
        await asyncio.sleep(.001)
        if "/extractions/" in request.url.path:
            return httpx.Response(200, json={"status": 200, "data": {"knowledgeId": request.url.path.split("/")[-2]}})
        run_id = request.url.path.split("/")[-2]
        if body["type"] == "RUN_RELEASED":
            releases[run_id] = body["status"]
        return httpx.Response(200, json={"status": 200, "data": {"runId": run_id, "status": body.get("runStatus", body.get("status", "RUNNING")), "lastEventId": 1}})

    async def simulated_node(**kwargs):
        if kwargs["node"]["type"] != "sql_read":
            return {"status": "SUCCEEDED", "output": {"ok": True}, "diagnostic": None, "interrupt": None}
        sql_nodes.append(kwargs["node"]["id"])
        async with cpu_slot() as lease:
            await asyncio.sleep(.01)
            parsed = await lease.run(parse_sql_read_output, current_sse)
        output = {key: parsed[key] for key in ("status", "rowCount", "data")}
        return {"status": "SUCCEEDED", "output": output, "diagnostic": None, "interrupt": None}

    async def simulated_model(_ticket, _nodes):
        nonlocal model_active, model_peak
        model_active += 1
        model_peak = max(model_peak, model_active)
        try:
            await asyncio.sleep(.02)
            return {"name": "模拟查询经验", "summary": "根据模拟条件查询信息。", "matchPhrases": ["信息查询"], "negativePhrases": [], "steps": []}
        finally:
            model_active -= 1

    callback_client = Tec01Client(base_url="http://tec01.invalid", token="benchmark-only", transport=httpx.MockTransport(callbacks))
    executor, compiler = PushExecutor(callback_client), CompilerService(callback_client)

    async def request(client, method, path, *, kind, **kwargs):
        started = time.perf_counter()
        response = await client.request(method, path, **kwargs)
        timings[kind].append((time.perf_counter() - started) * 1000)
        outcomes[f"{kind}:{response.status_code}"] += 1
        return response

    async def probes(client, stop):
        while not stop.is_set():
            started = time.perf_counter()
            await request(client, "GET", "/api/v1/health", kind="health")
            await request(client, "GET", "/internal/v1/executions/benchmark-probe", kind="activity")
            await request(client, "POST", "/internal/v1/executions/benchmark-probe/commands", kind="commands", json={"commandId": "probe", "type": "PAUSE"})
            # Probe command intentionally targets no run and must not mutate a workload.
            await asyncio.sleep(.01)
            probe_delay.append(max(0, (time.perf_counter() - started) * 1000 - 10))

    async def ticker(stop):
        while not stop.is_set():
            expected = time.perf_counter() + .005
            await asyncio.sleep(.005)
            lag.append(max(0, (time.perf_counter() - expected) * 1000))

    async def run_cases(client, name, count, *, resume=False, large=False):
        nonlocal current_sse
        current_sse = sse_fixture(large=large)
        payload_bytes[name] = len(current_sse)
        valid = await request(client, "POST", "/internal/v1/runtime/workflows/validate", kind="validate", json={"workflowDefinition": workflow(large=large), "validationMode": "EXECUTE"})
        valid.raise_for_status()
        definition = valid.json()
        start_sql = len(sql_nodes)

        async def one(index):
            run_id = f"benchmark-{name}-{index}"
            body = {"runId": run_id, "dispatchId": run_id + "-dispatch", "ticketId": 1, "workflowSnapshot": definition["normalizedDefinition"], "workflowContentHash": definition["workflowContentHash"], "runInputs": {}}
            if resume:
                body.update(currentNodeId="sql-b", nodeStates={"sql-a": "SUCCEEDED"}, nodeOutputs={"sql-a": {"status": 0, "rowCount": 2000, "data": [{"value": 1, "padding": "restore"} for _ in range(2000)]}})
            started = time.perf_counter()
            result = await request(client, "POST", "/internal/v1/executions/dispatch", kind="dispatch", headers={"X-AOPS-Api-Key": "benchmark-only-not-a-real-key"}, json=body)
            if result.status_code != 200:
                return {"accepted": False, "httpStatus": result.status_code}
            task = executor.active.get(run_id)
            if task:
                await task.task
            timings[f"{name}Run"].append((time.perf_counter() - started) * 1000)
            return {"accepted": True, "status": releases.get(run_id)}

        results = await asyncio.gather(*(one(index) for index in range(count)))
        if any(item.get("accepted") and item.get("status") != "SUCCEEDED" for item in results):
            raise AssertionError(f"{name}: accepted simulation did not finish successfully: {results}")
        if resume and "sql-a" in sql_nodes[start_sql:]:
            raise AssertionError("Resume replayed the already committed SQL node")
        scenarios.append({"name": name, "submitted": count, "accepted": sum(item["accepted"] for item in results), "results": results, "sqlInvocations": len(sql_nodes) - start_sql})

    async def extraction_cases(client, name, count, audit_count):
        started = time.perf_counter()
        template = evidence(audit_count)
        responses = await asyncio.gather(*(request(client, "POST", f"/internal/v1/extractions/benchmark-{name}-{index}", kind="extractionSubmit", json={**template, "requestId": f"request-{name}-{index}"}) for index in range(count)))
        tasks = list(compiler.tasks.values())
        if tasks:
            await asyncio.gather(*tasks)
        terminal = Counter(compiler.statuses.get(f"benchmark-{name}-{index}") for index in range(count) if responses[index].status_code == 202)
        if any(status != "SUCCEEDED" for status in terminal):
            raise AssertionError(f"{name}: accepted compiler simulations failed: {terminal}")
        if model_peak > profile["compiler_max_active_jobs"]:
            raise AssertionError("Compiler concurrency exceeded configured admission")
        timings[name].append((time.perf_counter() - started) * 1000)
        scenarios.append({"name": name, "submitted": count, "auditOperationsPerJob": audit_count, "accepted": sum(response.status_code == 202 for response in responses), "httpStatuses": dict(Counter(response.status_code for response in responses)), "terminalStatuses": dict(terminal)})

    configured = {**profile, "environment": "test", "runtime_service_token": "benchmark-only", "studio_admin_token": "benchmark-studio-only", "tec01_base_url": "http://tec01.invalid", "tec01_service_token": "benchmark-only", "tec01_run_callback_path": "/internal/v1/runs/{runId}/callbacks", "tec01_extraction_callback_path": "/internal/v1/extractions/{knowledgeId}/callbacks"}
    with tempfile.TemporaryDirectory(prefix="itsm-benchmark-") as temporary:
        store = log_module.CallLogStore(Path(temporary).resolve(), console=_QuietConsole())
        with patch.multiple(settings, **configured), patch.object(main_module, "push_executor", executor), patch.object(main_module, "compiler_service", compiler), patch.object(main_module, "inspect_cli", simulated_cli_metadata), patch.object(log_module, "call_logs", store), patch.object(log_module, "initialize", store.start), patch.object(extraction_module, "_assess", simulated_model), patch("app.runtime.push_executor.execute_single_node", simulated_node), patch("httpx.AsyncHTTPTransport.handle_async_request", forbidden), patch("asyncio.create_subprocess_exec", forbidden):
            # Recreate semaphore-bearing executors after profile values apply.
            executor.cli_slots = asyncio.Semaphore(profile["executor_max_active_cli"])
            async with main_module.app.router.lifespan_context(main_module.app):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main_module.app), base_url="http://benchmark.invalid", headers={"Authorization": "Bearer benchmark-only"}) as client:
                    stop = asyncio.Event()
                    probe = asyncio.create_task(probes(client, stop))
                    tick = asyncio.create_task(ticker(stop))
                    try:
                        if args.scenario in {"all", "normal"}:
                            await run_cases(client, "normal", args.runs)
                        if args.scenario in {"all", "resume"}:
                            await run_cases(client, "resume", min(args.runs, 4), resume=True)
                        if args.scenario in {"all", "audit"}:
                            await extraction_cases(client, "audit5000", 1, 5000)
                        if args.scenario in {"all", "burst"}:
                            await extraction_cases(client, "extractionBurst100", 100, 1)
                        if args.large and args.scenario in {"all", "large"}:
                            await run_cases(client, "large", 1, large=True)
                    finally:
                        stop.set()
                        await probe
                        await tick
                        for key in list(compiler.records):
                            compiler._evict(key)
    if data_budget.used != before_budget:
        raise AssertionError("Benchmark leaked application memory reservations")
    unit = 1 if sys.platform == "darwin" else 1024
    health = summary(timings["health"])
    return {
        "profile": args.profile, "settings": profile,
        "measurement": {"transport": "in-process ASGI HTTP routes", "sql": "simulated in-memory SSE; no CLI subprocess", "cliHealth": "mock metadata; no installed CLI inspection", "llm": "simulated 20ms wait; no model service", "tec01": "MockTransport 1ms acknowledgement; no network", "hardwareLimitsApplied": False, "observedCgroupLimits": cgroup_limits(), "host": platform.platform(), "logicalCpuCount": os.cpu_count(), "largeExplicitlyEnabled": args.large},
        "scenarios": scenarios, "inputBytes": payload_bytes,
        "latency": {key: summary(values) for key, values in timings.items()},
        "eventLoopLag": summary(lag), "probeCycleDelay": summary(probe_delay),
        "rss": {"selfPeakMiB": round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * unit / 1024**2, 3), "childrenPeakMiB": round(resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss * unit / 1024**2, 3), "platformRawUnit": "bytes" if sys.platform == "darwin" else "KiB", "note": "OS high-water RSS, not estimated data-budget usage; children include no real CLI workload"},
        "outcomes": dict(outcomes), "peakSimulatedModelCalls": model_peak,
        "checks": {"noExternalNetworkOrSubprocess": True, "resumeDoesNotReplayCommittedNode": True, "compilerAdmissionBounded": True, "budgetReturned": True},
        "targetObservation": {"healthP95TargetMs": args.target_p95_ms, "metOnThisRun": health["p95Ms"] is not None and health["p95Ms"] <= args.target_p95_ms, "capacityGuarantee": False, "note": "A target miss is reported honestly, not hidden or asserted away. Hardware profile names do not emulate CPU/RAM limits."},
    }


def arguments(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", choices=PROFILES, default="4c8g")
    parser.add_argument("--scenario", choices=["all", "normal", "resume", "audit", "burst", "large"], default="all")
    parser.add_argument("--runs", type=int, default=4, help="Small normal runs; admission rejections are measured, not retried")
    parser.add_argument("--large", action="store_true", help="Explicitly add one near-20MiB SSE scenario")
    parser.add_argument("--target-p95-ms", type=float, default=250)
    parser.add_argument("--output", type=Path, help="Optional new JSON report file; existing paths are never overwritten")
    result = parser.parse_args(argv)
    if not 1 <= result.runs <= 128:
        parser.error("--runs must be between 1 and 128")
    if result.scenario == "large" and not result.large:
        parser.error("--scenario large requires explicit --large")
    if result.output is not None:
        if os.path.lexists(result.output):
            parser.error("--output must be a new file; existing paths are never overwritten")
        if not result.output.parent.is_dir():
            parser.error("--output parent directory must already exist")
    return result


def write_report(report, output=None):
    content = json.dumps(report, ensure_ascii=False, indent=2)
    if output is not None:
        # Exclusive creation also handles another writer winning after argument
        # validation, and rejects symlinks instead of following them.
        with Path(output).open("x", encoding="utf-8") as stream:
            stream.write(content + "\n")
    return content


if __name__ == "__main__":
    try:
        options = arguments()
        report = asyncio.run(benchmark(options))
        rendered = write_report(report, options.output)
    except Exception as error:
        print(json.dumps({"benchmarkFailed": True, "errorType": type(error).__name__, "message": str(error)}, ensure_ascii=False))
        raise SystemExit(1)
    print(rendered)
