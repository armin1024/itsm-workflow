import asyncio
import json
import threading

import pytest

from app.config import settings
from app.compiler_errors import CompilerFailure
from app.extraction import compile_ticket_evidence
from app.resources import ResourceError, data_budget
from app.runtime.compiler_service import CompilerService
from tests.test_compiler_callbacks import Client, assessment, audit, job


async def dispose(service):
    await service.shutdown()
    for knowledge_id in list(service.records):
        service._evict(knowledge_id)


@pytest.mark.asyncio
async def test_task_creation_failure_releases_admission_and_identity(monkeypatch):
    service = CompilerService(Client())
    baseline = data_budget.used
    def failed_creation(*args, **kwargs):
        raise RuntimeError("task creation refused")
    monkeypatch.setattr("app.runtime.compiler_service.asyncio.create_task", failed_creation)
    with pytest.raises(RuntimeError, match="creation refused"):
        service.submit(job(), sync_tec01=False)
    assert service.active_jobs == 0 and data_budget.used == baseline
    assert not service.records and not service.tasks


@pytest.mark.asyncio
async def test_cancel_before_start_releases_compiler_reservation():
    service = CompilerService(Client())
    baseline = data_budget.used
    assert service.submit(job(), sync_tec01=False)
    task = service.tasks["knw-1"]
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    await asyncio.sleep(0)
    assert service.active_jobs == 0 and data_budget.used == baseline
    assert service.get("knw-1")["status"] == "INTERRUPTED"
    await dispose(service)


@pytest.mark.asyncio
async def test_compiler_rejects_excess_work_but_identical_submit_wins_before_capacity(monkeypatch):
    started, release = asyncio.Event(), asyncio.Event()

    async def blocked(ticket, nodes):
        started.set()
        await release.wait()
        return await assessment(ticket, nodes)

    monkeypatch.setattr(settings, "compiler_max_active_jobs", 1)
    monkeypatch.setattr("app.extraction._assess", blocked)
    service = CompilerService(Client())
    try:
        assert await service.submit_async(job(), sync_tec01=False)
        await asyncio.wait_for(started.wait(), 2)
        assert not await service.submit_async(job(), sync_tec01=False)
        with pytest.raises(ResourceError) as caught:
            await service.submit_async(job("knw-2", "req-2"), sync_tec01=False)
        assert caught.value.code == "COMPILER_NO_CAPACITY" and caught.value.status_code == 429
        assert service.active_jobs == 1 and "knw-2" not in service.records
        release.set()
        await service.tasks["knw-1"]
        assert service.active_jobs == 0
    finally:
        release.set()
        await dispose(service)


@pytest.mark.asyncio
async def test_admission_covers_fetch_preview_and_transfers_to_task(monkeypatch):
    monkeypatch.setattr(settings, "compiler_max_active_jobs", 1)
    monkeypatch.setattr("app.extraction._assess", assessment)
    service = CompilerService(Client())
    try:
        async with service.admission() as reservation:
            with pytest.raises(ResourceError, match="容量"):
                async with service.admission():
                    raise AssertionError("excess request must not fetch ticket evidence")
            assert await service.submit_async(job(), reservation=reservation, sync_tec01=False)
        assert service.active_jobs == 1
        await service.tasks["knw-1"]
        assert service.active_jobs == 0
        with pytest.raises(ValueError):
            async with service.admission():
                raise ValueError("ticket fetch failed")
        assert service.active_jobs == 0
    finally:
        await dispose(service)


@pytest.mark.asyncio
@pytest.mark.parametrize("case,code", [
    ("rows", "AUDIT_OPERATIONS_TOO_LARGE"),
    ("bytes", "EVIDENCE_TOO_LARGE"),
    ("sql", "NO_VALID_OPERATIONS"),
    ("nodes", "COMPILER_NODE_LIMIT_EXCEEDED"),
    ("model", "MODEL_INPUT_TOO_LARGE"),
])
async def test_limits_precede_model_and_never_misreport_as_llm_failure(monkeypatch, case, code):
    model_calls = []

    async def unexpected_model(*args):
        model_calls.append(args)
        raise AssertionError("oversized evidence must not call model")

    monkeypatch.setattr("app.extraction._assess", unexpected_model)
    ticket, rows = {"id": 1}, [audit()]
    if case == "rows":
        monkeypatch.setattr(settings, "compiler_max_audit_operations", 1)
        rows.append(audit(id="other"))
    elif case == "bytes":
        monkeypatch.setattr(settings, "compiler_evidence_max_mb", 1)
        ticket["description"] = "x" * (1024 * 1024)
    elif case == "sql":
        monkeypatch.setattr(settings, "sql_template_max_chars", 10)
    elif case == "nodes":
        rows = [audit(sql=f"SELECT id FROM users WHERE id={i}", id=f"op-{i}") for i in range(500)]
    elif case == "model":
        monkeypatch.setattr(settings, "compiler_model_input_max_chars", 50)
    with pytest.raises((ResourceError, CompilerFailure)) as caught:
        await compile_ticket_evidence(ticket, rows)
    assert caught.value.code == code and model_calls == []
    assert CompilerService._safe_error(caught.value)["code"] == code


@pytest.mark.asyncio
async def test_preparation_runs_outside_event_loop_and_keeps_admission_until_done(monkeypatch):
    entered, finish = threading.Event(), threading.Event()
    from app.extraction import extract_operations as original

    def slow_parse(rows, failed=None):
        entered.set()
        assert finish.wait(2)
        return original(rows, failed)

    monkeypatch.setattr("app.extraction.extract_operations", slow_parse)
    monkeypatch.setattr("app.extraction._assess", assessment)
    service = CompilerService(Client())
    try:
        assert await service.submit_async(job(), sync_tec01=False)
        while not entered.is_set():
            await asyncio.sleep(0)
        task = service.tasks["knw-1"]
        task.cancel()
        await asyncio.sleep(0)
        assert service.active_jobs == 1 and not task.done()
        finish.set()
        await asyncio.gather(task, return_exceptions=True)
        assert service.active_jobs == 0
    finally:
        finish.set()
        await dispose(service)


@pytest.mark.asyncio
async def test_completed_cache_has_one_final_graph_and_byte_based_eviction(monkeypatch):
    monkeypatch.setattr("app.extraction._assess", assessment)
    service = CompilerService(Client())
    baseline = data_budget.used
    try:
        await service.submit_async(job(), sync_tec01=False)
        await service.tasks["knw-1"]
        record = service.records["knw-1"]
        assert record["proposal"] is record["_finalCallback"]["proposal"]
        assert "proposal" not in record["events"][-1]
        assert service.cache_bytes > 0 and data_budget.used > baseline
        first_size = service.cache_bytes
        service.max_cache_bytes = first_size + 1024
        await service.submit_async(job("knw-2", "req-2"), sync_tec01=False)
        await service.tasks["knw-2"]
        assert "knw-1" not in service.records and "knw-2" in service.records
        assert service.cache_bytes <= service.max_cache_bytes
        value = await service.get_async("knw-2")
        value["proposal"]["name"] = "changed"
        assert service.records["knw-2"]["proposal"]["name"] != "changed"
    finally:
        await dispose(service)
    assert data_budget.used == baseline
