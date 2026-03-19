from __future__ import annotations

import asyncio
import hashlib
import io
import logging

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.core.contracts import (
    BBox4, OfflineBundleManifest, RouteIntelligenceScore,
    TripPreferences, resolve_categories, density_budget_multiplier,
)
from app.core.errors import bad_request, not_found
from app.core.storage import get_manifest, put_score_pack
from app.core.time import utc_now_iso
from app.services.bundle import Bundle
from app.services.corridor import Corridor
from app.services.coverage import Coverage
from app.services.flood import Flood
from app.services.fuel import Fuel
from app.services.hazards import Hazards
from app.services.places import Places
from app.services.rest_areas import RestAreas
from app.services.route_score import RouteScore
from app.services.traffic import Traffic
from app.services.weather import Weather
from app.services.wildlife import Wildlife
from app.services.emergency_services import EmergencyServices
from app.services.heritage import Heritage
from app.services.air_quality import AirQuality
from app.services.bushfire import Bushfire
from app.services.speed_cameras import SpeedCameras
from app.services.toilets import Toilets
from app.services.school_zones import SchoolZones
from app.services.roadkill import Roadkill

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/bundle")


def get_bundle_service() -> Bundle:
    raise RuntimeError("Bundle must be provided by app dependency override")


def get_corridor_service() -> Corridor:
    raise RuntimeError("Corridor must be provided by app dependency override")


def get_places_service() -> Places:
    raise RuntimeError("Places must be provided by app dependency override")


def get_cache_conn():
    raise RuntimeError("cache conn must be provided by app dependency override")


def get_traffic_service(cache_conn=Depends(get_cache_conn)) -> Traffic:
    return Traffic(conn=cache_conn)


def get_hazards_service(cache_conn=Depends(get_cache_conn)) -> Hazards:
    return Hazards(conn=cache_conn)


def get_weather_service(cache_conn=Depends(get_cache_conn)) -> Weather:
    return Weather(conn=cache_conn)


def get_coverage_service(cache_conn=Depends(get_cache_conn)) -> Coverage:
    return Coverage(conn=cache_conn)


def get_fuel_service(cache_conn=Depends(get_cache_conn)) -> Fuel:
    return Fuel(conn=cache_conn)


def get_flood_service(cache_conn=Depends(get_cache_conn)) -> Flood:
    return Flood(conn=cache_conn)


def get_wildlife_service(cache_conn=Depends(get_cache_conn)) -> Wildlife:
    return Wildlife(conn=cache_conn)


def get_rest_areas_service(cache_conn=Depends(get_cache_conn)) -> RestAreas:
    return RestAreas(conn=cache_conn)


def get_route_score_service() -> RouteScore:
    return RouteScore()


def get_emergency_service(cache_conn=Depends(get_cache_conn)) -> EmergencyServices:
    return EmergencyServices(conn=cache_conn)


def get_heritage_service(cache_conn=Depends(get_cache_conn)) -> Heritage:
    return Heritage(conn=cache_conn)


def get_air_quality_service(cache_conn=Depends(get_cache_conn)) -> AirQuality:
    return AirQuality(conn=cache_conn)


def get_bushfire_service(cache_conn=Depends(get_cache_conn)) -> Bushfire:
    return Bushfire(conn=cache_conn)


def get_speed_cameras_service(cache_conn=Depends(get_cache_conn)) -> SpeedCameras:
    return SpeedCameras(conn=cache_conn)


def get_toilets_service(cache_conn=Depends(get_cache_conn)) -> Toilets:
    return Toilets(conn=cache_conn)


def get_school_zones_service(cache_conn=Depends(get_cache_conn)) -> SchoolZones:
    return SchoolZones(conn=cache_conn)


def get_roadkill_service(cache_conn=Depends(get_cache_conn)) -> Roadkill:
    return Roadkill(conn=cache_conn)


class BundleBuildRequest(BaseModel):
    plan_id: str
    route_key: str
    geometry: str  # polyline6
    profile: str = "drive"
    buffer_m: int | None = None
    max_edges: int | None = None
    styles: list[str] = []
    departure_iso: str | None = None
    avg_speed_kmh: float = 90.0
    # Trip preferences - controls stop density & category filtering
    trip_prefs: TripPreferences | None = None


@router.post("/build", response_model=OfflineBundleManifest)
async def build_bundle(
    req: BundleBuildRequest,
    bundle: Bundle = Depends(get_bundle_service),
    corridor: Corridor = Depends(get_corridor_service),
    places: Places = Depends(get_places_service),
    traffic: Traffic = Depends(get_traffic_service),
    hazards: Hazards = Depends(get_hazards_service),
    weather: Weather = Depends(get_weather_service),
    coverage: Coverage = Depends(get_coverage_service),
    fuel: Fuel = Depends(get_fuel_service),
    flood: Flood = Depends(get_flood_service),
    wildlife: Wildlife = Depends(get_wildlife_service),
    rest_areas: RestAreas = Depends(get_rest_areas_service),
    route_score: RouteScore = Depends(get_route_score_service),
    emergency_svc: EmergencyServices = Depends(get_emergency_service),
    heritage_svc: Heritage = Depends(get_heritage_service),
    air_quality_svc: AirQuality = Depends(get_air_quality_service),
    bushfire_svc: Bushfire = Depends(get_bushfire_service),
    speed_cameras_svc: SpeedCameras = Depends(get_speed_cameras_service),
    toilets_svc: Toilets = Depends(get_toilets_service),
    school_zones_svc: SchoolZones = Depends(get_school_zones_service),
    roadkill_svc: Roadkill = Depends(get_roadkill_service),
) -> OfflineBundleManifest:
    if not req.plan_id:
        bad_request("bad_bundle_request", "plan_id required")
    if not req.route_key:
        bad_request("bad_bundle_request", "route_key required")
    if not req.geometry:
        bad_request("bad_bundle_request", "geometry required")

    profile = req.profile or "drive"
    buffer_m = int(req.buffer_m or 5000)
    max_edges = int(req.max_edges or 2000000)

    # 1) Fetch places FIRST - we need stop coordinates for the corridor.
    #    search_bundle is sync (httpx.Client) so run in thread executor.
    #    Pass trip preferences to control density + category filtering.
    loop = asyncio.get_event_loop()
    ppack = None

    # Resolve categories and density from user preferences
    _trip_prefs = req.trip_prefs
    _enabled_cats = resolve_categories(_trip_prefs) if _trip_prefs else None
    _density_mult = density_budget_multiplier(_trip_prefs.stop_density) if _trip_prefs else 1.0

    try:
        ppack = await loop.run_in_executor(
            None,
            lambda: places.search_bundle(
                polyline6=req.geometry,
                categories=_enabled_cats,
                density_multiplier=_density_mult,
            ),
        )
    except Exception as exc:
        logger.warning("bundle places fetch failed (non-fatal): %s", exc)

    # Extract stop coordinates for corridor building
    stop_coords: list[tuple[float, float]] = []
    if ppack and hasattr(ppack, "items") and ppack.items:
        for item in ppack.items:
            stop_coords.append((item.lat, item.lng))
    logger.info("corridor stop_coords: %d from places (ppack=%s, items=%d)",
                len(stop_coords), type(ppack).__name__ if ppack else "None",
                len(ppack.items) if ppack and hasattr(ppack, "items") and ppack.items else 0)

    # 2) Build corridor using stop locations + route spine
    logger.info(">>> BUNDLE corridor.ensure with %d stop_coords", len(stop_coords))
    ensure_result = corridor.ensure(
        route_key=req.route_key,
        route_polyline6=req.geometry,
        profile=profile,
        buffer_m=buffer_m,
        max_edges=max_edges,
        stop_coords=stop_coords,
    )
    cmeta = ensure_result.meta
    cpack = ensure_result.pack or corridor.get(cmeta.corridor_key)
    if not cpack:
        not_found("corridor_missing", f"no corridor pack found for {cmeta.corridor_key}")

    # 3) All remaining overlays - run concurrently.

    async def _safe(coro_or_awaitable, name: str):
        """Await a coroutine; return None and log on any exception."""
        try:
            return await coro_or_awaitable
        except Exception as exc:
            logger.warning("bundle overlay '%s' failed (non-fatal): %s", name, exc)
            return None

    async def _maybe_weather():
        if not req.departure_iso:
            return None
        return await weather.forecast_along_route(
            polyline6=req.geometry,
            departure_iso=req.departure_iso,
            avg_speed_kmh=req.avg_speed_kmh,
        )

    async def _maybe_coverage():
        from app.core.settings import settings as _s
        if not _s.coverage_enabled:
            return None
        return await coverage.along_route(polyline6=req.geometry)

    async def _maybe_flood():
        from app.core.settings import settings as _s
        if not _s.flood_enabled:
            return None
        return await flood.poll(bbox=cpack.bbox)

    async def _maybe_wildlife():
        from app.core.settings import settings as _s
        if not _s.wildlife_enabled:
            return None
        return await wildlife.along_route(
            polyline6=req.geometry,
            departure_iso=req.departure_iso,
        )

    (
        tpack,
        hpack,
        wpack,
        cov_pack,
        fuel_pack,
        flood_pack,
        wildlife_pack,
        rest_pack,
        emergency_pack,
        heritage_pack,
        aqi_pack,
        bushfire_pack,
        cameras_pack,
        toilets_pack,
        school_zones_pack,
        roadkill_pack,
    ) = await asyncio.gather(
        _safe(traffic.poll(bbox=cpack.bbox), "traffic"),
        _safe(hazards.poll(bbox=cpack.bbox), "hazards"),
        _safe(_maybe_weather(), "weather"),
        _safe(_maybe_coverage(), "coverage"),
        _safe(fuel.along_route(polyline6=req.geometry), "fuel"),
        _safe(_maybe_flood(), "flood"),
        _safe(_maybe_wildlife(), "wildlife"),
        _safe(rest_areas.along_route(polyline6=req.geometry), "rest_areas"),
        _safe(emergency_svc.along_route(polyline6=req.geometry), "emergency"),
        _safe(heritage_svc.along_route(polyline6=req.geometry), "heritage"),
        _safe(air_quality_svc.along_route(polyline6=req.geometry), "air_quality"),
        _safe(bushfire_svc.along_route(polyline6=req.geometry), "bushfire"),
        _safe(speed_cameras_svc.along_route(polyline6=req.geometry), "speed_cameras"),
        _safe(toilets_svc.along_route(polyline6=req.geometry), "toilets"),
        _safe(school_zones_svc.along_route(polyline6=req.geometry), "school_zones"),
        _safe(roadkill_svc.along_route(polyline6=req.geometry), "roadkill"),
    )

    # 3) Route intelligence score - synchronous, uses overlay results gathered above.
    score_pack = None
    score_key = None
    try:
        score_result = route_score.compute(
            weather=wpack,
            fuel=fuel_pack,
            flood=flood_pack,
            rest=rest_pack,
            coverage=cov_pack,
            wildlife=wildlife_pack,
            traffic=tpack,
            hazards=hpack,
        )
        # Generate a stable key from route_key so the score can be cached and retrieved.
        score_key = "score_" + hashlib.sha1(req.route_key.encode()).hexdigest()[:16]
        from app.core.settings import settings as _s
        put_score_pack(
            bundle.conn,
            score_key=score_key,
            created_at=utc_now_iso(),
            algo_version=getattr(_s, "score_algo_version", "1"),
            pack=score_result.model_dump(),
        )
        score_pack = score_result
    except Exception as exc:
        logger.warning("route_score compute failed (non-fatal): %s", exc)

    # 4) Manifest
    return bundle.build_manifest(
        plan_id=req.plan_id,
        route_key=req.route_key,
        styles=req.styles,
        navpack_ready=True,
        corridor_key=cmeta.corridor_key,
        corridor_ready=True,
        places_key=(ppack.places_key if ppack else None),
        places_ready=(ppack is not None),
        traffic_key=(tpack.traffic_key if tpack else None),
        traffic_ready=(tpack is not None),
        hazards_key=(hpack.hazards_key if hpack else None),
        hazards_ready=(hpack is not None),
        weather_key=(wpack.weather_key if wpack else None),
        weather_ready=(wpack is not None),
        coverage_key=(cov_pack.coverage_key if cov_pack else None),
        coverage_ready=(cov_pack is not None),
        fuel_key=(fuel_pack.fuel_key if fuel_pack else None),
        fuel_ready=(fuel_pack is not None),
        flood_key=(flood_pack.flood_key if flood_pack else None),
        flood_ready=(flood_pack is not None),
        wildlife_key=(wildlife_pack.wildlife_key if wildlife_pack else None),
        wildlife_ready=(wildlife_pack is not None),
        rest_key=(rest_pack.rest_key if rest_pack else None),
        rest_ready=(rest_pack is not None),
        score_key=score_key,
        score_ready=(score_pack is not None),
        emergency_key=(emergency_pack.emergency_key if emergency_pack else None),
        emergency_ready=(emergency_pack is not None),
        heritage_key=(heritage_pack.heritage_key if heritage_pack else None),
        heritage_ready=(heritage_pack is not None),
        aqi_key=(aqi_pack.aqi_key if aqi_pack else None),
        aqi_ready=(aqi_pack is not None),
        bushfire_key=(bushfire_pack.bushfire_key if bushfire_pack else None),
        bushfire_ready=(bushfire_pack is not None),
        cameras_key=(cameras_pack.cameras_key if cameras_pack else None),
        cameras_ready=(cameras_pack is not None),
        toilets_key=(toilets_pack.toilets_key if toilets_pack else None),
        toilets_ready=(toilets_pack is not None),
        school_zones_key=(school_zones_pack.school_zones_key if school_zones_pack else None),
        school_zones_ready=(school_zones_pack is not None),
        roadkill_key=(roadkill_pack.roadkill_key if roadkill_pack else None),
        roadkill_ready=(roadkill_pack is not None),
    )


class ScoreRefreshRequest(BaseModel):
    route_key: str
    bbox: BBox4


@router.post("/score/refresh", response_model=RouteIntelligenceScore)
async def refresh_score(
    req: ScoreRefreshRequest,
    bundle: Bundle = Depends(get_bundle_service),
    traffic: Traffic = Depends(get_traffic_service),
    hazards: Hazards = Depends(get_hazards_service),
    route_score: RouteScore = Depends(get_route_score_service),
) -> RouteIntelligenceScore:
    """Re-fetch traffic & hazards, recompute and cache the route intelligence score."""
    if not req.route_key:
        bad_request("bad_score_refresh_request", "route_key required")

    async def _safe(coro, name: str):
        try:
            return await coro
        except Exception as exc:
            logger.warning("score_refresh overlay '%s' failed (non-fatal): %s", name, exc)
            return None

    tpack, hpack = await asyncio.gather(
        _safe(traffic.poll(bbox=req.bbox), "traffic"),
        _safe(hazards.poll(bbox=req.bbox), "hazards"),
    )

    score_result = route_score.compute(traffic=tpack, hazards=hpack)
    score_key = "score_" + hashlib.sha1(req.route_key.encode()).hexdigest()[:16]
    from app.core.settings import settings as _s
    put_score_pack(
        bundle.conn,
        score_key=score_key,
        created_at=utc_now_iso(),
        algo_version=getattr(_s, "score_algo_version", "1"),
        pack=score_result.model_dump(),
    )
    return score_result


@router.get("/{plan_id}", response_model=OfflineBundleManifest)
def get_bundle(plan_id: str, cache_conn=Depends(get_cache_conn)) -> OfflineBundleManifest:
    row = get_manifest(cache_conn, plan_id)
    if not row:
        not_found("bundle_missing", f"no manifest for plan_id {plan_id}")
    return OfflineBundleManifest.model_validate(row)


@router.get("/{plan_id}/download")
def download_bundle(
    plan_id: str,
    bundle: Bundle = Depends(get_bundle_service),
) -> StreamingResponse:
    z = bundle.build_zip(plan_id=plan_id)
    return StreamingResponse(
        io.BytesIO(z.zip_bytes),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="roam_bundle_{plan_id}.zip"'},
    )
