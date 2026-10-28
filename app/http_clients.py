"""Lifespan-owned, independent HTTP pools for callbacks and model traffic."""
from __future__ import annotations

from contextlib import asynccontextmanager
from collections.abc import AsyncIterator

import httpx

from app.config import settings


_tec01: httpx.AsyncClient | None = None
_llm: httpx.AsyncClient | None = None


async def start_http_clients() -> None:
    global _tec01, _llm
    if _tec01 is not None or _llm is not None:
        return
    _tec01 = httpx.AsyncClient(limits=httpx.Limits(
        max_connections=settings.tec01_http_max_connections,
        max_keepalive_connections=settings.tec01_http_max_keepalive,
    ))
    _llm = httpx.AsyncClient(limits=httpx.Limits(
        max_connections=settings.compiler_max_active_jobs,
        max_keepalive_connections=settings.compiler_max_active_jobs,
    ))


async def close_http_clients() -> None:
    global _tec01, _llm
    clients, _tec01, _llm = (_tec01, _llm), None, None
    for client in clients:
        if client is not None:
            await client.aclose()


@asynccontextmanager
async def get_tec01_client(*, transport: httpx.AsyncBaseTransport | None = None) -> AsyncIterator[httpx.AsyncClient]:
    # MockTransport remains isolated and is never installed in a production pool.
    if transport is None and _tec01 is not None:
        yield _tec01
    else:
        async with httpx.AsyncClient(transport=transport) as client:
            yield client


@asynccontextmanager
async def get_llm_client() -> AsyncIterator[httpx.AsyncClient]:
    if _llm is not None:
        yield _llm
    else:
        # Unit tests and direct library use do not require an application lifespan.
        async with httpx.AsyncClient() as client:
            yield client
