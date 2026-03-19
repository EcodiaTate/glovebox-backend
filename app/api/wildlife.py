# app/api/wildlife.py
"""
Wildlife hazard overlay endpoints.

  POST /nav/wildlife/along-route  - Wildlife collision risk along a route corridor
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from app.core.contracts import WildlifeOverlay
from app.core.errors import bad_request
from app.core.time import utc_now_iso
from app.services.wildlife import Wildlife

router = APIRouter(prefix="/nav/wildlife")


# ──────────────────────────────────────────────────────────────
# Dependency
# ──────────────────────────────────────────────────────────────

def get_cache_conn():
    raise RuntimeError("cache conn must be provided by app dependency override")


def get_wildlife_service(cache_conn=Depends(get_cache_conn)) -> Wildlife:
    return Wildlife(conn=cache_conn)


# ──────────────────────────────────────────────────────────────
# Endpoints
# ──────────────────────────────────────────────────────────────

class WildlifeAlongRouteRequest(BaseModel):
    polyline6: str
    buffer_km: float = Field(default=10.0, ge=1.0, le=50.0)
    departure_iso: Optional[str] = None
    cache_seconds: Optional[int] = None
    timeout_s: Optional[float] = None


@router.post("/along-route", response_model=WildlifeOverlay)
async def wildlife_along_route(
    req: WildlifeAlongRouteRequest,
    wildlife: Wildlife = Depends(get_wildlife_service),
) -> WildlifeOverlay:
    if not req.polyline6:
        bad_request("bad_wildlife_request", "polyline6 required")
    result = await wildlife.along_route(
        polyline6=req.polyline6,
        buffer_km=req.buffer_km,
        departure_iso=req.departure_iso,
        cache_seconds=req.cache_seconds,
        timeout_s=req.timeout_s,
    )
    if result is None:
        return WildlifeOverlay(
            wildlife_key="disabled",
            polyline6=req.polyline6,
            algo_version="wildlife.v1.disabled",
            created_at=utc_now_iso(),
            warnings=["Wildlife overlay disabled."],
        )
    return result
