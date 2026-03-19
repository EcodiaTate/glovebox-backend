# app/api/fuel.py
"""
Fuel overlay endpoints.

  POST /nav/fuel/poll          - Fuel stations + EV chargers in a bbox
  POST /nav/fuel/along-route   - Fuel along a route corridor (with gap warnings)
"""
from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from app.core.contracts import BBox4, FuelOverlay
from app.core.errors import bad_request
from app.services.fuel import Fuel

router = APIRouter(prefix="/nav/fuel")


# ──────────────────────────────────────────────────────────────
# Dependency
# ──────────────────────────────────────────────────────────────

def get_cache_conn():
    raise RuntimeError("cache conn must be provided by app dependency override")


def get_fuel_service(cache_conn=Depends(get_cache_conn)) -> Fuel:
    return Fuel(conn=cache_conn)


# ──────────────────────────────────────────────────────────────
# Endpoints
# ──────────────────────────────────────────────────────────────

class FuelPollRequest(BaseModel):
    bbox: BBox4
    fuel_types: Optional[List[str]] = None
    cache_seconds: Optional[int] = None
    timeout_s: Optional[float] = None


class FuelAlongRouteRequest(BaseModel):
    polyline6: str
    buffer_km: float = Field(default=15.0, ge=1.0, le=100.0)
    fuel_types: Optional[List[str]] = None
    no_fuel_gap_km: float = Field(default=200.0, ge=50.0)
    cache_seconds: Optional[int] = None
    timeout_s: Optional[float] = None


@router.post("/poll", response_model=FuelOverlay)
async def fuel_poll(
    req: FuelPollRequest,
    fuel: Fuel = Depends(get_fuel_service),
) -> FuelOverlay:
    if not req.bbox:
        bad_request("bad_fuel_request", "bbox required")
    return await fuel.poll(
        bbox=req.bbox,
        fuel_types=req.fuel_types,
        cache_seconds=req.cache_seconds,
        timeout_s=req.timeout_s,
    )


@router.post("/along-route", response_model=FuelOverlay)
async def fuel_along_route(
    req: FuelAlongRouteRequest,
    fuel: Fuel = Depends(get_fuel_service),
) -> FuelOverlay:
    if not req.polyline6:
        bad_request("bad_fuel_request", "polyline6 required")
    return await fuel.along_route(
        polyline6=req.polyline6,
        buffer_km=req.buffer_km,
        fuel_types=req.fuel_types,
        no_fuel_gap_km=req.no_fuel_gap_km,
        cache_seconds=req.cache_seconds,
        timeout_s=req.timeout_s,
    )
