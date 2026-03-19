from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from app.core.contracts import (
    BBox4,
    CorridorGraphMeta,
    CorridorGraphPack,
    ElevationProfile,
    ElevationRequest,
    FloodOverlay,
    GradeSegment,
    NavPack,
    NavRequest,
    RouteIntelligenceScore,
    TrafficOverlay,
    HazardOverlay,
    WeatherOverlay,
)
from app.core.errors import bad_request, not_found
from app.services.routing import Routing
from app.services.corridor import Corridor
from app.services.traffic import Traffic
from app.services.hazards import Hazards
from app.services.elevation import Elevation, compute_grade_segments
from app.services.flood import Flood
from app.services.weather import Weather
from app.services.fuel import Fuel
from app.services.rest_areas import RestAreas
from app.services.coverage import Coverage
from app.services.wildlife import Wildlife
from app.services.route_score import RouteScore
from app.core.storage import put_nav_pack
from app.core.settings import settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/nav")


# ──────────────────────────────────────────────────────────────
# Dependency factories
# ──────────────────────────────────────────────────────────────

def get_routing_service() -> Routing:
    return Routing(
        osrm_base_url=settings.osrm_base_url,
        osrm_profile=settings.osrm_profile,
        algo_version=settings.algo_version,
    )


def get_corridor_service() -> Corridor:
    raise RuntimeError("Corridor must be provided by app dependency override")


def get_cache_conn():
    raise RuntimeError("cache conn must be provided by app dependency override")


def get_traffic_service(cache_conn=Depends(get_cache_conn)) -> Traffic:
    return Traffic(conn=cache_conn)


def get_hazards_service(cache_conn=Depends(get_cache_conn)) -> Hazards:
    return Hazards(conn=cache_conn)


def get_elevation_service() -> Elevation:
    from app.core.settings import settings
    return Elevation(timeout_s=30.0, api_key=settings.opentopography_api_key or None)


def get_weather_service(cache_conn=Depends(get_cache_conn)) -> Weather:
    return Weather(conn=cache_conn)


def get_flood_service(cache_conn=Depends(get_cache_conn)) -> Flood:
    return Flood(conn=cache_conn)


def get_fuel_service(cache_conn=Depends(get_cache_conn)) -> Fuel:
    return Fuel(conn=cache_conn)


def get_rest_areas_service(cache_conn=Depends(get_cache_conn)) -> RestAreas:
    return RestAreas(conn=cache_conn)


def get_coverage_service(cache_conn=Depends(get_cache_conn)) -> Coverage:
    return Coverage(conn=cache_conn)


def get_wildlife_service(cache_conn=Depends(get_cache_conn)) -> Wildlife:
    return Wildlife(conn=cache_conn)


def get_route_score_service() -> RouteScore:
    return RouteScore()


# ──────────────────────────────────────────────────────────────
# Route
# ──────────────────────────────────────────────────────────────

@router.post("/route", response_model=NavPack)
def nav_route(
    req: NavRequest,
    svc: Routing = Depends(get_routing_service),
    cache_conn=Depends(get_cache_conn),
) -> NavPack:
    pack = svc.route(req)

    put_nav_pack(
        cache_conn,
        route_key=pack.primary.route_key,
        created_at=pack.primary.created_at,
        algo_version=pack.primary.algo_version,
        pack=pack.model_dump(),
    )
    return pack


# ──────────────────────────────────────────────────────────────
# Corridor
# ──────────────────────────────────────────────────────────────

class CorridorEnsureRequest(BaseModel):
    route_key: str
    geometry: str  # polyline6
    profile: str = "drive"
    buffer_m: int | None = None
    max_edges: int | None = None
    stop_coords: list[list[float]] | None = None  # [[lat, lng], ...]


@router.post("/corridor/ensure", response_model=CorridorGraphMeta)
def corridor_ensure(
    req: CorridorEnsureRequest,
    corridor: Corridor = Depends(get_corridor_service),
) -> CorridorGraphMeta:
    if not req.route_key:
        bad_request("bad_corridor_request", "route_key is required")
    if not req.geometry:
        bad_request("bad_corridor_request", "geometry (polyline6) is required")

    buffer_m = int(req.buffer_m or settings.corridor_buffer_m_default)
    max_edges = int(req.max_edges or settings.corridor_max_edges_default)

    stop_tuples = None
    if req.stop_coords:
        stop_tuples = [(c[0], c[1]) for c in req.stop_coords if len(c) >= 2]

    logger.info(">>> NAV corridor.ensure with %d stop_coords", len(stop_tuples) if stop_tuples else 0)
    result = corridor.ensure(
        route_key=req.route_key,
        route_polyline6=req.geometry,
        profile=req.profile or "drive",
        buffer_m=buffer_m,
        max_edges=max_edges,
        stop_coords=stop_tuples,
    )
    # Include pack inline to avoid multi-instance 404 on the separate GET
    meta = result.meta
    meta.pack = result.pack
    return meta


@router.get("/corridor/{corridor_key}", response_model=CorridorGraphPack)
def corridor_get(
    corridor_key: str,
    corridor: Corridor = Depends(get_corridor_service),
) -> CorridorGraphPack:
    pack = corridor.get(corridor_key)
    if not pack:
        not_found("corridor_missing", f"no corridor pack found for {corridor_key}")
        raise AssertionError("unreachable")  # not_found always raises HTTPException
    return pack


# ──────────────────────────────────────────────────────────────
# Elevation
# ──────────────────────────────────────────────────────────────

class ElevationResponse(BaseModel):
    """Elevation profile with optional pre-computed grade segments."""
    profile: ElevationProfile
    grade_segments: list[GradeSegment] = Field(default_factory=list)


@router.post("/elevation", response_model=ElevationResponse)
def nav_elevation(
    req: ElevationRequest,
    svc: Elevation = Depends(get_elevation_service),
) -> ElevationResponse:
    if not req.geometry:
        bad_request("bad_elevation_request", "geometry (polyline6) is required")

    profile = svc.profile(req)

    # Pre-compute grade segments so the frontend can use them directly
    # for fuel range adjustments without recomputing
    grades = compute_grade_segments(profile, segment_length_km=5.0)

    return ElevationResponse(profile=profile, grade_segments=grades)


# ──────────────────────────────────────────────────────────────
# Overlays (traffic + hazards)
# ──────────────────────────────────────────────────────────────

class OverlayPollRequest(BaseModel):
    bbox: BBox4
    cache_seconds: int | None = None
    timeout_s: float | None = None


@router.post("/traffic/poll", response_model=TrafficOverlay)
async def traffic_poll(
    req: OverlayPollRequest,
    traffic: Traffic = Depends(get_traffic_service),
) -> TrafficOverlay:
    if not req.bbox:
        bad_request("bad_overlay_request", "bbox required")
    return await traffic.poll(bbox=req.bbox, cache_seconds=req.cache_seconds, timeout_s=req.timeout_s)


class HazardsPollRequest(BaseModel):
    bbox: BBox4
    sources: list[str] = Field(default_factory=list)
    cache_seconds: int | None = None
    timeout_s: float | None = None


@router.post("/hazards/poll", response_model=HazardOverlay)
async def hazards_poll(
    req: HazardsPollRequest,
    hazards: Hazards = Depends(get_hazards_service),
) -> HazardOverlay:
    if not req.bbox:
        bad_request("bad_overlay_request", "bbox required")
    return await hazards.poll(
        bbox=req.bbox,
        sources=(req.sources or None),
        cache_seconds=req.cache_seconds,
        timeout_s=req.timeout_s,
    )


# ──────────────────────────────────────────────────────────────
# Weather overlay
# ──────────────────────────────────────────────────────────────

class WeatherForecastRequest(BaseModel):
    polyline6: str
    departure_iso: str
    avg_speed_kmh: float = Field(default=90.0, gt=0)
    sample_interval_km: float | None = Field(default=None, gt=0)


@router.post("/weather/forecast", response_model=WeatherOverlay)
async def weather_forecast(
    req: WeatherForecastRequest,
    weather: Weather = Depends(get_weather_service),
) -> WeatherOverlay:
    if not req.polyline6:
        bad_request("bad_weather_request", "polyline6 required")
    if not req.departure_iso:
        bad_request("bad_weather_request", "departure_iso required")
    return await weather.forecast_along_route(
        polyline6=req.polyline6,
        departure_iso=req.departure_iso,
        avg_speed_kmh=req.avg_speed_kmh,
        sample_interval_km=req.sample_interval_km,
    )


# ──────────────────────────────────────────────────────────────
# Flood gauge overlay
# ──────────────────────────────────────────────────────────────

class FloodPollRequest(BaseModel):
    bbox: BBox4


@router.post("/flood/poll", response_model=FloodOverlay)
async def flood_poll(
    req: FloodPollRequest,
    flood: Flood = Depends(get_flood_service),
) -> FloodOverlay:
    if not req.bbox:
        bad_request("bad_flood_request", "bbox required")
    return await flood.poll(bbox=req.bbox)


# ──────────────────────────────────────────────────────────────
# Route Intelligence Score
# ──────────────────────────────────────────────────────────────

class RouteScoreRequest(BaseModel):
    polyline6: str
    bbox: BBox4
    departure_iso: str
    avg_speed_kmh: float = Field(default=90.0, gt=0)


@router.post("/route-score", response_model=RouteIntelligenceScore)
async def route_score(
    req: RouteScoreRequest,
    traffic: Traffic = Depends(get_traffic_service),
    hazards: Hazards = Depends(get_hazards_service),
    weather: Weather = Depends(get_weather_service),
    flood: Flood = Depends(get_flood_service),
    fuel: Fuel = Depends(get_fuel_service),
    rest: RestAreas = Depends(get_rest_areas_service),
    coverage: Coverage = Depends(get_coverage_service),
    wildlife: Wildlife = Depends(get_wildlife_service),
    scorer: RouteScore = Depends(get_route_score_service),
) -> RouteIntelligenceScore:
    if not req.polyline6:
        bad_request("bad_score_request", "polyline6 required")
    if not req.departure_iso:
        bad_request("bad_score_request", "departure_iso required")

    # Fetch all overlays concurrently - failures are caught individually
    results = await asyncio.gather(
        traffic.poll(bbox=req.bbox),
        hazards.poll(bbox=req.bbox),
        weather.forecast_along_route(
            polyline6=req.polyline6,
            departure_iso=req.departure_iso,
            avg_speed_kmh=req.avg_speed_kmh,
        ),
        flood.poll(bbox=req.bbox),
        fuel.along_route(polyline6=req.polyline6),
        rest.along_route(polyline6=req.polyline6),
        coverage.along_route(polyline6=req.polyline6),
        wildlife.along_route(polyline6=req.polyline6),
        return_exceptions=True,
    )

    traffic_ov, hazards_ov, weather_ov, flood_ov, fuel_ov, rest_ov, coverage_ov, wildlife_ov = results

    # Replace exceptions with None and log them
    def _safe(val, name: str):
        if isinstance(val, Exception):
            logger.warning("route-score: %s overlay failed: %s", name, val)
            return None
        return val

    return scorer.compute(
        traffic=_safe(traffic_ov, "traffic"),
        hazards=_safe(hazards_ov, "hazards"),
        weather=_safe(weather_ov, "weather"),
        flood=_safe(flood_ov, "flood"),
        fuel=_safe(fuel_ov, "fuel"),
        rest=_safe(rest_ov, "rest"),
        coverage=_safe(coverage_ov, "coverage"),
        wildlife=_safe(wildlife_ov, "wildlife"),
    )
