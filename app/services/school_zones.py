# app/services/school_zones.py
"""
School zone overlay service for Roam.

Data source: Transport for NSW - School Zone Timetables (CC BY 3.0 AU, no auth)
  Sites ArcGIS FeatureServer:
    https://portal.data.nsw.gov.au/arcgis/rest/services/Hosted/TFNSW_School_Zone_Sites_public/FeatureServer/0/query
  Timetable CSV (static):
    https://opendata.transport.nsw.gov.au/data/dataset/001bd1ce.../schoolzone_timetables_20210114.json

Coverage: NSW only. School zones are typically active 8:00-9:30 and 14:30-16:00
on school days (Mon–Fri, excluding NSW public holidays).

Algorithm:
  1. Decode polyline6 → bounding box + buffer.
  2. Query TfNSW School Zone Sites ArcGIS FeatureServer.
  3. For each zone, compute distance from route.
  4. Determine if zone is currently active based on local time + day-of-week.
  5. Cache for 24 hours (zone locations change only with new school year).

Active-hours logic:
  - NSW school zones are active Mon–Fri (school days) only.
  - Morning: 08:00–09:30 AEST/AEDT
  - Afternoon: 14:30–16:00 AEST/AEDT
  - Speed limit during active hours: 40 km/h
  - Outside active hours: normal posted speed limit applies
  - We do not attempt to model NSW public holiday calendars - we flag
    weekends definitively as inactive, weekdays as "check active hours".
"""
from __future__ import annotations

import base64
import hashlib
import logging
from datetime import datetime
from typing import Any, List, Optional, Tuple
from zoneinfo import ZoneInfo

import httpx

from app.core.cache_utils import stable_key, is_fresh
from app.core.geo import decode_polyline6, sample_route, bbox_from_coords, min_dist_to_route
from app.core.storage import put_school_zones_pack, get_school_zones_pack
from app.core.time import utc_now_iso
from app.core.contracts import SchoolZone, SchoolZonesOverlay

logger = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════════
# Constants
# ══════════════════════════════════════════════════════════════

_CACHE_TTL_S = 86_400  # 24 hours
_ALGO_VERSION = "school_zones-1.0"
_MAX_ZONES = 300
_HTTP_TIMEOUT = 20.0

_SCHOOL_ZONES_URL = (
    "https://portal.data.nsw.gov.au/arcgis/rest/services"
    "/Hosted/TFNSW_School_Zone_Sites_public/FeatureServer/0/query"
)

# NSW school zone active windows (local time, AEST/AEDT)
_NSW_TZ = ZoneInfo("Australia/Sydney")
_MORNING_START = (8, 0)    # 08:00
_MORNING_END   = (9, 30)   # 09:30
_AFTERNOON_START = (14, 30)  # 14:30
_AFTERNOON_END   = (16, 0)   # 16:00

_SPEED_LIMIT_ACTIVE_KMH = 40


# ══════════════════════════════════════════════════════════════
# Active-hours logic
# ══════════════════════════════════════════════════════════════

def _check_active_now() -> Tuple[bool, Optional[str]]:
    """
    Returns (is_active, session_name) based on current Sydney time.
    Weekends are never active. Weekday active windows: 08:00-09:30, 14:30-16:00.
    NSW public holidays are NOT modelled - weekdays return True during windows.
    """
    now_sydney = datetime.now(tz=_NSW_TZ)
    # 0=Monday, 6=Sunday
    if now_sydney.weekday() >= 5:
        return False, None

    hm = (now_sydney.hour, now_sydney.minute)

    def _in_window(start: Tuple[int, int], end: Tuple[int, int]) -> bool:
        return start <= hm < end

    if _in_window(_MORNING_START, _MORNING_END):
        return True, "morning"
    if _in_window(_AFTERNOON_START, _AFTERNOON_END):
        return True, "afternoon"
    return False, None


def _sydney_time_str() -> str:
    return datetime.now(tz=_NSW_TZ).strftime("%Y-%m-%dT%H:%M:%S%z")


# ══════════════════════════════════════════════════════════════
# Data fetcher
# ══════════════════════════════════════════════════════════════

def _parse_float(v: Any) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


async def _fetch_nsw_school_zones(
    client: httpx.AsyncClient,
    min_lat: float,
    min_lng: float,
    max_lat: float,
    max_lng: float,
    route_samples: List[Tuple[float, float]],
    buffer_km: float,
    is_active: bool,
    active_session: Optional[str],
    warnings: List[str],
) -> List[SchoolZone]:
    geometry = f"{min_lng},{min_lat},{max_lng},{max_lat}"
    params = {
        "where": "1=1",
        "geometry": geometry,
        "geometryType": "esriGeometryEnvelope",
        "inSR": "4326",
        "outFields": "*",
        "outSR": "4326",
        "resultRecordCount": 2000,
        "f": "json",
    }

    try:
        resp = await client.get(_SCHOOL_ZONES_URL, params=params, timeout=_HTTP_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        warnings.append(f"school_zones:fetch: {e}")
        return []

    features = data.get("features") or []
    zones: List[SchoolZone] = []

    for feat in features:
        try:
            attrs = feat.get("attributes") or {}
            geom = feat.get("geometry") or {}

            lat = _parse_float(attrs.get("lat") or attrs.get("latitude") or attrs.get("Lat"))
            lng = _parse_float(attrs.get("lon") or attrs.get("longitude") or attrs.get("Lon") or attrs.get("long"))

            if lat is None or lng is None:
                lat = _parse_float(geom.get("y"))
                lng = _parse_float(geom.get("x"))

            if lat is None or lng is None:
                continue

            dist_km = min_dist_to_route(lat, lng, route_samples)
            if dist_km > buffer_km:
                continue

            # Build stable ID
            raw_id = f"school_zone::{round(lat, 5)}::{round(lng, 5)}"
            zid = base64.urlsafe_b64encode(
                hashlib.sha256(raw_id.encode()).digest()
            ).decode().rstrip("=")[:20]

            # Field names vary; try several common patterns
            school_name = (
                str(attrs.get("school_name") or attrs.get("SchoolName") or
                    attrs.get("site_description") or attrs.get("SiteDescription") or "").strip() or None
            )
            road = (
                str(attrs.get("road") or attrs.get("Road") or attrs.get("street") or "").strip() or None
            )
            suburb = (
                str(attrs.get("suburb") or attrs.get("Suburb") or attrs.get("locality") or "").strip() or None
            )

            zones.append(SchoolZone(
                id=zid,
                school_name=school_name,
                lat=lat,
                lng=lng,
                road=road,
                suburb=suburb,
                state="NSW",
                speed_limit_active_kmh=_SPEED_LIMIT_ACTIVE_KMH,
                is_currently_active=is_active,
                active_session=active_session if is_active else None,
                distance_from_route_km=round(dist_km, 2),
            ))
        except Exception as e:
            warnings.append(f"school_zones:parse: {e}")

    logger.info(
        "school_zones: NSW returned %d features → %d zones (active=%s)",
        len(features), len(zones), is_active,
    )
    return zones


# ══════════════════════════════════════════════════════════════
# Main service class
# ══════════════════════════════════════════════════════════════

class SchoolZones:
    """
    School zone overlay service (NSW only).

    Queries TfNSW school zone sites near a route and annotates each
    with whether the 40 km/h limit is currently active.
    """

    def __init__(self, *, conn) -> None:
        self.conn = conn

    async def along_route(
        self,
        *,
        polyline6: str,
        buffer_km: float = 10.0,
        cache_seconds: int = _CACHE_TTL_S,
    ) -> SchoolZonesOverlay:
        zones_key = stable_key("school_zones", {
            "polyline6": polyline6,
            "buffer_km": round(buffer_km, 1),
            "algo_version": _ALGO_VERSION,
        })

        cached = get_school_zones_pack(self.conn, zones_key)
        if cached:
            created_at = cached.get("created_at", "")
            if is_fresh(created_at, max_age_s=cache_seconds):
                # Re-evaluate active status against current time (not cached)
                is_active, active_session = _check_active_now()
                overlay = SchoolZonesOverlay.model_validate(cached)
                for z in overlay.zones:
                    z.is_currently_active = is_active
                    z.active_session = active_session if is_active else None
                overlay.active_count = sum(1 for z in overlay.zones if z.is_currently_active)
                overlay.checked_at_local = _sydney_time_str()
                logger.debug("school_zones cache hit (active=%s): %s", is_active, zones_key)
                return overlay

        coords = decode_polyline6(polyline6)
        if not coords:
            overlay = SchoolZonesOverlay(
                school_zones_key=zones_key,
                polyline6=polyline6,
                algo_version=_ALGO_VERSION,
                created_at=utc_now_iso(),
                warnings=["Failed to decode route polyline."],
            )
            put_school_zones_pack(self.conn, school_zones_key=zones_key, created_at=overlay.created_at, algo_version=_ALGO_VERSION, pack=overlay.model_dump())
            return overlay

        route_samples = sample_route(coords, interval_km=2.0)
        min_lat, min_lng, max_lat, max_lng = bbox_from_coords(coords, buffer_km)
        warnings: List[str] = []

        # NSW-only data source - skip fetch for routes entirely outside NSW
        _NSW_LAT_MIN, _NSW_LAT_MAX = -37.6, -27.5
        _NSW_LNG_MIN, _NSW_LNG_MAX = 140.5, 154.0
        if max_lat < _NSW_LAT_MIN or min_lat > _NSW_LAT_MAX or max_lng < _NSW_LNG_MIN or min_lng > _NSW_LNG_MAX:
            overlay = SchoolZonesOverlay(
                school_zones_key=zones_key,
                polyline6=polyline6,
                algo_version=_ALGO_VERSION,
                created_at=utc_now_iso(),
                warnings=["Route does not pass through NSW - school zone data not available outside NSW."],
            )
            put_school_zones_pack(self.conn, school_zones_key=zones_key, created_at=overlay.created_at, algo_version=_ALGO_VERSION, pack=overlay.model_dump())
            return overlay

        is_active, active_session = _check_active_now()

        async with httpx.AsyncClient(follow_redirects=True, timeout=httpx.Timeout(_HTTP_TIMEOUT)) as client:
            zones = await _fetch_nsw_school_zones(
                client, min_lat, min_lng, max_lat, max_lng,
                route_samples, buffer_km, is_active, active_session, warnings,
            )

        zones.sort(key=lambda z: z.distance_from_route_km or 0.0)

        if len(zones) > _MAX_ZONES:
            warnings.append(f"{len(zones)} school zones found; limited to {_MAX_ZONES}.")
            zones = zones[:_MAX_ZONES]

        logger.info(
            "school_zones: polyline=%d chars, zones=%d, active=%s",
            len(polyline6), len(zones), is_active,
        )

        created_at = utc_now_iso()
        overlay = SchoolZonesOverlay(
            school_zones_key=zones_key,
            polyline6=polyline6,
            algo_version=_ALGO_VERSION,
            created_at=created_at,
            zones=zones,
            active_count=sum(1 for z in zones if z.is_currently_active),
            checked_at_local=_sydney_time_str(),
            warnings=warnings,
        )
        put_school_zones_pack(self.conn, school_zones_key=zones_key, created_at=created_at, algo_version=_ALGO_VERSION, pack=overlay.model_dump())
        return overlay
