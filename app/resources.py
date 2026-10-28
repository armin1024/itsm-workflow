"""Bounded data-only CPU work and conservative application data accounting.

This is admission control, not an OS memory/RSS limiter. State transitions and
budget ownership stay on the event loop; workers never mutate runtime state.
"""
from __future__ import annotations

import asyncio
import contextvars
import functools
import sys
import weakref
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from typing import Any, Callable

from app.config import settings

MIB = 1024 * 1024
CPU_SCRATCH_BYTES = 512 * MIB


class ResourceError(RuntimeError):
    def __init__(self, code: str, message: str, *, status_code=422, retryable=False):
        super().__init__(message)
        self.code, self.status_code, self.retryable = code, status_code, retryable


def object_size(value: Any, limit: int | None = None) -> int:
    """Estimate a JSON tree, conservatively charging repeated references.

    Iterators bound traversal scratch memory by depth, not the number of rows.
    No giant identity set is needed; non-JSON/cyclic input is safely bounded.
    """
    total = count = 0
    pending = [iter((value,))]
    while pending:
        try:
            item = next(pending[-1])
        except StopIteration:
            pending.pop()
            continue
        total += sys.getsizeof(item)
        count += 1
        if limit is not None and total > limit:
            return limit + 1
        if count > 10_000_000 or len(pending) > 1024:
            raise ResourceError("JSON_COMPLEXITY_LIMIT", "JSON结构过大、过深或存在循环，请拆分数据")
        if isinstance(item, dict):
            pending.append(iter(item.values()))
            pending.append(iter(item.keys()))
        elif isinstance(item, (list, tuple, deque)):
            pending.append(iter(item))
    return total


class DataBudget:
    """Loop-owned byte reservations for active data and transient caches."""
    def __init__(self):
        self.owners: dict[str, int] = {}

    @property
    def used(self) -> int:
        return sum(self.owners.values())

    def reserve(self, owner: str, amount: int):
        if amount < 0:
            raise ValueError("negative memory reservation")
        proposed = self.used - self.owners.get(owner, 0) + amount
        if proposed > settings.runtime_data_budget_mb * MIB:
            raise ResourceError("RUNTIME_NO_CAPACITY", "服务数据预算不足，请等待活动任务释放或稍后重试", status_code=429, retryable=True)
        self.owners[owner] = amount

    def release(self, owner: str):
        self.owners.pop(owner, None)

    def transfer(self, old: str, new: str, amount: int):
        prior = self.owners.pop(old, None)
        try:
            self.reserve(new, amount)
        except BaseException:
            if prior is not None:
                self.owners[old] = prior
            raise

    def status(self):
        return {"usedBytes": self.used, "budgetBytes": settings.runtime_data_budget_mb * MIB,
                "reservations": len(self.owners), "accounting": "estimated-not-rss"}


data_budget = DataBudget()


class _CpuService:
    def __init__(self):
        self.workers = settings.cpu_max_workers
        # Reserve bounded parsing admission before launching a CLI, without
        # holding a physical CPU worker throughout minutes of network I/O.
        self.slots = asyncio.Semaphore(self.workers + settings.executor_max_active_cli + settings.compiler_max_active_jobs)
        self.worker_slots = asyncio.Semaphore(self.workers)
        self.executor = ThreadPoolExecutor(max_workers=self.workers, thread_name_prefix="itsm-data")


_services: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()


def _service() -> _CpuService:
    loop = asyncio.get_running_loop()
    if loop not in _services:
        _services[loop] = _CpuService()
    return _services[loop]


class CpuLease:
    def __init__(self, service: _CpuService):
        self.service = service

    async def run(self, function: Callable, *args, **kwargs):
        await self.service.worker_slots.acquire()
        try:
            return await self._run(function, *args, **kwargs)
        finally:
            self.service.worker_slots.release()

    async def _run(self, function: Callable, *args, **kwargs):
        context = contextvars.copy_context()
        work = functools.partial(function, *args, **kwargs)
        future = asyncio.get_running_loop().run_in_executor(self.service.executor, context.run, work)
        cancelled = False
        while not future.done():
            try:
                await asyncio.shield(future)
            except asyncio.CancelledError:
                cancelled = True
            except Exception:
                break
        if cancelled:
            # Consume worker exceptions without leaking the reservation early.
            try:
                future.result()
            except BaseException:
                pass
            raise asyncio.CancelledError()
        return future.result()


@asynccontextmanager
async def cpu_slot():
    service = _service()
    await service.slots.acquire()
    try:
        yield CpuLease(service)
    finally:
        service.slots.release()


async def cpu_run(function: Callable, *args, **kwargs):
    async with cpu_slot() as lease:
        return await lease.run(function, *args, **kwargs)


async def shutdown_resources():
    service = _services.pop(asyncio.get_running_loop(), None)
    if service:
        await asyncio.to_thread(service.executor.shutdown, wait=True, cancel_futures=False)
