# app/core/http_client.py
"""
Shared httpx.AsyncClient factory for overlay services.

Centralises connection pooling, timeout defaults, and retry configuration
so each service doesn't spin up its own transport per request.

Usage in services:
    from app.core.http_client import http_client

    async with http_client() as client:
        resp = await client.get(url)

The returned client uses a shared transport with connection pooling,
HTTP/1.1 keep-alive, and sensible defaults for outback-grade latency.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

import httpx

# ──────────────────────────────────────────────────────────────
# Defaults
# ──────────────────────────────────────────────────────────────

_DEFAULT_TIMEOUT = httpx.Timeout(25.0, connect=10.0)
_DEFAULT_LIMITS = httpx.Limits(
    max_connections=80,
    max_keepalive_connections=20,
    keepalive_expiry=30.0,
)

# Module-level shared transport - created once, reused across requests.
# AsyncHTTPTransport is thread-safe and async-safe.
_shared_transport: httpx.AsyncHTTPTransport | None = None


def _get_transport() -> httpx.AsyncHTTPTransport:
    global _shared_transport
    if _shared_transport is None:
        _shared_transport = httpx.AsyncHTTPTransport(
            retries=2,
            limits=_DEFAULT_LIMITS,
        )
    return _shared_transport


@asynccontextmanager
async def http_client(
    *,
    timeout: httpx.Timeout | float | None = None,
) -> AsyncIterator[httpx.AsyncClient]:
    """
    Yield an AsyncClient backed by a shared connection-pooled transport.

    Args:
        timeout: Override the default 25s timeout. Pass a float for a
                 uniform timeout or an httpx.Timeout for granular control.
    """
    t = timeout if timeout is not None else _DEFAULT_TIMEOUT
    if isinstance(t, (int, float)):
        t = httpx.Timeout(float(t), connect=10.0)

    async with httpx.AsyncClient(
        transport=_get_transport(),
        timeout=t,
        follow_redirects=True,
    ) as client:
        yield client


async def shutdown_http_client() -> None:
    """Close the shared transport. Call during app shutdown."""
    global _shared_transport
    if _shared_transport is not None:
        await _shared_transport.aclose()
        _shared_transport = None
