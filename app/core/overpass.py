# app/core/overpass.py
"""
Global Overpass API client with concurrency control, instance rotation,
and rate-limit awareness.

Problem: Multiple services (places, rest_areas, speed_cameras) all hit
Overpass simultaneously during trip enrichment, causing 429s, timeouts,
and 403s from exhausting all instances.

Solution: A SINGLE global gate (threading.Semaphore) shared by both
sync (places.py thread pool) and async (overlays) callers:
  - Limits total concurrent Overpass requests across ALL services
  - Rotates through instances round-robin
  - Enforces minimum spacing between requests to the same instance
  - Fast connect timeout (3s) so dead instances fail quickly

Usage:
    from app.core.overpass import overpass_fetch, overpass_fetch_sync

    # Async (rest_areas, speed_cameras)
    data = await overpass_fetch(ql)

    # Sync (places.py thread pool)
    data = overpass_fetch_sync(ql)
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Any, Dict, Optional

import httpx

from app.core.settings import settings

logger = logging.getLogger(__name__)

# ── Configuration ─────────────────────────────────────────────

# Max concurrent Overpass requests across ALL services (sync + async).
# With 3 instances allowing ~2 concurrent each = 6 total is safe.
_MAX_CONCURRENT = 6

# Minimum seconds between requests to the SAME instance.
_MIN_INSTANCE_SPACING_S = 0.3

# Retryable HTTP status codes.
_RETRYABLE = frozenset({429, 502, 503, 504})

# ── State ─────────────────────────────────────────────────────

# SINGLE unified semaphore - shared by sync and async callers.
# This prevents sync places queries from starving async overlay queries.
_global_sem = threading.Semaphore(_MAX_CONCURRENT)

# Per-instance last-request timestamp (thread-safe via _timing_lock).
_timing_lock = threading.Lock()
_instance_last_ts: Dict[str, float] = {}

# Round-robin counter.
_robin_counter = 0
_robin_lock = threading.Lock()


def _get_urls() -> list[str]:
    urls = [settings.overpass_url]
    fallbacks = getattr(settings, "overpass_fallback_urls", None) or []
    urls.extend(fallbacks)
    return urls


def _next_url() -> str:
    """Pick the next instance via round-robin."""
    global _robin_counter
    urls = _get_urls()
    with _robin_lock:
        idx = _robin_counter % len(urls)
        _robin_counter += 1
    return urls[idx]


def _wait_for_instance(url: str) -> None:
    """Block until MIN_INSTANCE_SPACING_S has elapsed since last request to this host."""
    host = url.split("/")[2]
    with _timing_lock:
        last = _instance_last_ts.get(host, 0.0)
    wait = _MIN_INSTANCE_SPACING_S - (time.monotonic() - last)
    if wait > 0:
        time.sleep(wait)
    with _timing_lock:
        _instance_last_ts[host] = time.monotonic()


# ── Public API: async ─────────────────────────────────────────

async def overpass_fetch(
    ql: str,
    *,
    timeout_s: float | None = None,
    label: str = "overpass",
) -> Dict[str, Any]:
    """
    Execute an Overpass QL query with global concurrency control.

    Uses the shared threading.Semaphore via run_in_executor to avoid
    blocking the event loop while waiting for a slot.
    """
    timeout = timeout_s or float(getattr(settings, "overpass_timeout_s", 25))
    urls = _get_urls()
    attempts = len(urls)
    last_exc: Optional[Exception] = None

    for i in range(attempts):
        url = _next_url()

        # Acquire the shared semaphore in a thread to avoid blocking the event loop
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, _global_sem.acquire)
        try:
            # Instance spacing (non-blocking sleep)
            host = url.split("/")[2]
            with _timing_lock:
                last = _instance_last_ts.get(host, 0.0)
            wait = _MIN_INSTANCE_SPACING_S - (time.monotonic() - last)
            if wait > 0:
                await asyncio.sleep(wait)
            with _timing_lock:
                _instance_last_ts[host] = time.monotonic()

            try:
                async with httpx.AsyncClient(
                    timeout=httpx.Timeout(timeout, connect=3.0),
                    follow_redirects=True,
                ) as client:
                    t0 = time.monotonic()
                    resp = await client.post(url, data={"data": ql})
                    elapsed = time.monotonic() - t0
                    logger.info("[overpass] %s %s → %d in %.1fs", label, host, resp.status_code, elapsed)

                if resp.status_code in _RETRYABLE:
                    last_exc = httpx.HTTPStatusError(
                        f"{resp.status_code}", request=resp.request, response=resp,
                    )
                    continue

                if resp.status_code == 403:
                    logger.warning("[overpass] %s %s returned 403, skipping", label, host)
                    last_exc = httpx.HTTPStatusError(
                        "403", request=resp.request, response=resp,
                    )
                    continue

                resp.raise_for_status()
                return resp.json()

            except httpx.HTTPStatusError:
                raise
            except Exception as e:
                logger.warning("[overpass] %s %s error: %s", label, host, type(e).__name__)
                last_exc = e
        finally:
            _global_sem.release()

    raise last_exc or RuntimeError(f"[overpass] {label}: all instances failed")


# ── Public API: sync (for places.py thread pool) ─────────────

def overpass_fetch_sync(
    ql: str,
    *,
    timeout_s: float | None = None,
    label: str = "overpass",
) -> Dict[str, Any]:
    """
    Synchronous version for use in thread pools.

    Shares the same _global_sem with the async path - this prevents
    sync places queries from starving async overlay queries.
    """
    timeout = timeout_s or float(getattr(settings, "overpass_timeout_s", 25))
    urls = _get_urls()
    attempts = len(urls)
    last_exc: Optional[Exception] = None

    for i in range(attempts):
        url = _next_url()

        _global_sem.acquire()
        try:
            _wait_for_instance(url)
            host = url.split("/")[2]
            try:
                with httpx.Client(
                    timeout=httpx.Timeout(timeout, connect=3.0),
                    follow_redirects=True,
                ) as client:
                    t0 = time.monotonic()
                    resp = client.post(url, data={"data": ql})
                    elapsed = time.monotonic() - t0
                    logger.info("[overpass] %s %s → %d in %.1fs", label, host, resp.status_code, elapsed)

                if resp.status_code in _RETRYABLE:
                    last_exc = httpx.HTTPStatusError(
                        f"{resp.status_code}", request=resp.request, response=resp,
                    )
                    continue

                if resp.status_code == 403:
                    logger.warning("[overpass] %s %s returned 403, skipping", label, host)
                    last_exc = httpx.HTTPStatusError(
                        "403", request=resp.request, response=resp,
                    )
                    continue

                resp.raise_for_status()
                return resp.json()

            except httpx.HTTPStatusError:
                raise
            except Exception as e:
                logger.warning("[overpass] %s %s error: %s", label, host, type(e).__name__)
                last_exc = e
        finally:
            _global_sem.release()

    raise last_exc or RuntimeError(f"[overpass] {label}: all instances failed")
