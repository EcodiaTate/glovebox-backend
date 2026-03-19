from __future__ import annotations

import asyncio
import hashlib
import base64
import logging
import threading
import time

from typing import Any, Dict, List, Optional, Tuple

import httpx
import orjson

from app.core.contracts import (
    FatigueWarning,
    RestArea,
    RestAreaOverlay,
    RestFacilities,
)
from app.core.polyline6 import decode_polyline6
from app.core.settings import settings
from app.core.storage import get_rest_area_pack, put_rest_area_pack
from app.core.time import utc_now_iso
from app.core.geo import bbox_from_coords, haversine_km, min_dist_to_route_with_km, RouteGrid
from app.core.http_client import http_client
from app.core.cache_utils import is_fresh

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────────────────────

# OSM tag → RestArea.type mapping
_OSM_TYPE_MAP: Dict[str, str] = {
    "rest_area": "rest_area",      # highway=rest_area
    "services": "service_station", # highway=services
    "camp_site": "camp_site",      # tourism=camp_site
    "caravan_site": "caravan_site",# tourism=caravan_site
    "toilets": "toilets",          # amenity=toilets
}

# ──────────────────────────────────────────────────────────────
# Geometry helpers (same pattern as places.py)
# ──────────────────────────────────────────────────────────────


def _sample_polyline_with_km(
    poly6: str, interval_km: float
) -> List[Tuple[float, float, float]]:
    """
    Walk the polyline and return (lat, lng, km_along) samples at interval_km spacing,
    including the start and end points.
    """
    pts = decode_polyline6(poly6)
    if not pts or len(pts) < 2:
        return []

    interval_m = max(500.0, interval_km * 1000.0)
    samples: List[Tuple[float, float, float]] = []
    samples.append((float(pts[0][0]), float(pts[0][1]), 0.0))

    dist_acc = 0.0
    next_mark = interval_m

    for i in range(1, len(pts)):
        p0 = (float(pts[i - 1][0]), float(pts[i - 1][1]))
        p1 = (float(pts[i][0]), float(pts[i][1]))
        seg_km = haversine_km(p0, p1)
        seg_m = seg_km * 1000.0
        if seg_m <= 0:
            continue

        while dist_acc + seg_m >= next_mark:
            overshoot = next_mark - dist_acc
            t = max(0.0, min(1.0, overshoot / seg_m))
            lat = p0[0] + (p1[0] - p0[0]) * t
            lng = p0[1] + (p1[1] - p0[1]) * t
            samples.append((lat, lng, next_mark / 1000.0))
            next_mark += interval_m

        dist_acc += seg_m

    # Always include the last point
    last = (float(pts[-1][0]), float(pts[-1][1]), dist_acc / 1000.0)
    if haversine_km(samples[-1][:2], last[:2]) > 0.5:
        samples.append(last)

    return samples


def _route_total_km(poly6: str) -> float:
    pts = decode_polyline6(poly6)
    if not pts or len(pts) < 2:
        return 0.0
    total = 0.0
    for i in range(1, len(pts)):
        total += haversine_km(
            (float(pts[i - 1][0]), float(pts[i - 1][1])),
            (float(pts[i][0]), float(pts[i][1])),
        )
    return total


# ──────────────────────────────────────────────────────────────
# Cache key
# ──────────────────────────────────────────────────────────────

def _rest_key(polyline6: str, sample_interval_km: float, buffer_km: float, algo_version: str) -> str:
    payload = orjson.dumps(
        {
            "algo_version": algo_version,
            "polyline6": polyline6,
            "sample_interval_km": round(sample_interval_km, 3),
            "buffer_km": round(buffer_km, 3),
        },
        option=orjson.OPT_SORT_KEYS,
    )
    h = hashlib.sha256(payload).digest()
    return base64.urlsafe_b64encode(h).decode("ascii").rstrip("=")


# ──────────────────────────────────────────────────────────────
# Overpass HTTP client - delegates to global gate
# ──────────────────────────────────────────────────────────────


# Dedicated Overpass instances for lightweight overlay queries.
async def _fetch_overpass(*, client: httpx.AsyncClient, ql: str) -> Dict[str, Any]:
    """Overpass query routed through the global gate for coordinated concurrency."""
    from app.core.overpass import overpass_fetch
    return await overpass_fetch(ql, label="rest_areas")


# ──────────────────────────────────────────────────────────────
# Overpass QL builder
# ──────────────────────────────────────────────────────────────

_FACILITY_TAGS = [
    "toilets", "drinking_water", "shower", "bbq", "picnic_table",
    "power_supply", "internet_access", "fee", "lit", "covered",
    "shelter", "capacity", "opening_hours", "name", "camp_site",
]

def _build_overpass_query(
    min_lat: float, min_lng: float, max_lat: float, max_lng: float,
) -> str:
    bbox = f"{min_lat},{min_lng},{max_lat},{max_lng}"
    # Lean query - only node+way for actual rest areas / services.
    # Camp/caravan sites use node-only (way queries are expensive and
    # `out center` gives us the centroid anyway).
    # Removed regex toilet filters ("highway"~".*") - they cause full
    # tag scans across every toilet node in the bbox and are very slow.
    filters = [
        f'node["highway"="rest_area"]({bbox});',
        f'way["highway"="rest_area"]({bbox});',
        f'node["highway"="services"]({bbox});',
        f'way["highway"="services"]({bbox});',
        f'node["tourism"="camp_site"]({bbox});',
        f'node["tourism"="caravan_site"]({bbox});',
    ]
    union = "\n  ".join(filters)
    ql_timeout = max(10, int(settings.overpass_timeout_s) - 10)
    return f"""[out:json][timeout:{ql_timeout}];
(
  {union}
);
out center tags;"""


# ──────────────────────────────────────────────────────────────
# Element parsing + quality scoring
# ──────────────────────────────────────────────────────────────

def _tag_bool(tags: Dict[str, str], key: str) -> Optional[bool]:
    v = tags.get(key, "").lower()
    if v in ("yes", "true", "1"):
        return True
    if v in ("no", "false", "0"):
        return False
    return None


def _parse_element(el: Dict[str, Any]) -> Optional[RestArea]:
    """Parse a single Overpass element into a RestArea, or None if unrecognisable."""
    tags: Dict[str, str] = el.get("tags") or {}

    # Determine coordinates (nodes have lat/lng; ways have center)
    if el.get("type") == "node":
        lat = el.get("lat")
        lng = el.get("lon")
    else:
        center = el.get("center") or {}
        lat = center.get("lat")
        lng = center.get("lon")

    if lat is None or lng is None:
        return None

    # Determine type
    osm_type = None
    hw = tags.get("highway")
    tourism = tags.get("tourism")
    amenity = tags.get("amenity")

    if hw in ("rest_area",):
        osm_type = "rest_area"
    elif hw == "services":
        osm_type = "service_station"
    elif tourism == "camp_site":
        osm_type = "camp_site"
    elif tourism == "caravan_site":
        osm_type = "caravan_site"
    elif amenity == "toilets":
        osm_type = "toilets"
    else:
        return None

    # Build stable ID from source + type + coords
    osm_id = f"{el.get('type','x')}/{el.get('id', 0)}"
    stable_id_raw = f"overpass::{osm_type}::{round(float(lat), 5)}::{round(float(lng), 5)}"
    h = hashlib.sha256(stable_id_raw.encode()).digest()
    stable_id = base64.urlsafe_b64encode(h).decode("ascii").rstrip("=")[:20]

    # Facilities
    fac = RestFacilities(
        toilets=_tag_bool(tags, "toilets"),
        drinking_water=_tag_bool(tags, "drinking_water"),
        shower=_tag_bool(tags, "shower"),
        bbq=_tag_bool(tags, "bbq"),
        picnic_table=_tag_bool(tags, "picnic_table"),
        power_supply=_tag_bool(tags, "power_supply"),
        internet=_tag_bool(tags, "internet_access"),
        lit=_tag_bool(tags, "lit"),
        shelter=_tag_bool(tags, "covered") or _tag_bool(tags, "shelter"),
        capacity=int(tags["capacity"]) if tags.get("capacity", "").isdigit() else None,
    )

    # Quality score (1-5 based on facility presence)
    score = 0
    if fac.toilets:
        score += 1
    if fac.drinking_water:
        score += 1
    if fac.shelter:
        score += 1
    if fac.bbq or fac.picnic_table:
        score += 1
    if fac.lit:
        score += 1

    fee_val = _tag_bool(tags, "fee")

    return RestArea(
        id=stable_id,
        name=tags.get("name") or tags.get("ref") or None,
        lat=float(lat),
        lng=float(lng),
        type=osm_type,
        quality_score=score,
        facilities=fac,
        opening_hours=tags.get("opening_hours") or None,
        fee=fee_val,
        source="overpass",
    )


# ──────────────────────────────────────────────────────────────
# Government dataset fetchers
# ──────────────────────────────────────────────────────────────

_GOV_TIMEOUT = httpx.Timeout(30.0, connect=15.0)

# In-memory cache for statewide government datasets (they rarely change).
# Each entry: (timestamp, List[RestArea]).  TTL = 6 hours.
_GOV_CACHE_TTL = 6 * 3600.0
_gov_cache: Dict[str, Tuple[float, List["RestArea"]]] = {}
_gov_cache_lock = threading.Lock()
_gov_preload_started = False

_QLD_BASE_URL = (
    "https://spatial-gis.information.qld.gov.au/arcgis/rest/services"
    "/Transportation/StateRoadInformation/MapServer/17/query"
)


async def _preload_gov_data() -> None:
    """Fetch all state-wide government rest area data into memory cache.

    Called in the background on first route request so subsequent requests
    are instant (local filter only, no external API calls).
    """
    global _gov_preload_started
    _gov_preload_started = True
    logger.info("rest_areas: starting background preload of government data")
    try:
        async with http_client(timeout=60.0) as client:
            results = await asyncio.gather(
                _fetch_qld_rest_areas(client),
                _fetch_wa_rest_areas(client),
                return_exceptions=True,
            )
        for i, label in enumerate(("QLD", "WA")):
            if isinstance(results[i], Exception):
                logger.warning("rest_areas: preload %s failed: %r", label, results[i])
            else:
                logger.info("rest_areas: preload %s → %d areas", label, len(results[i]))
    except Exception as e:
        logger.warning("rest_areas: preload failed: %r", e)


def _ensure_preload() -> None:
    """Kick off background preload if not already started."""
    global _gov_preload_started
    if _gov_preload_started:
        return
    _gov_preload_started = True
    # Fire-and-forget in a background thread so it doesn't block the request
    def _run():
        asyncio.run(_preload_gov_data())
    threading.Thread(target=_run, daemon=True).start()

_WA_ENDPOINTS: Dict[str, str] = {
    "major": (
        "https://gisservices.mainroads.wa.gov.au/arcgis/rest/services"
        "/OpenData/HVS_Networks_DataPortal/MapServer/2/query"
    ),
    "minor": (
        "https://gisservices.mainroads.wa.gov.au/arcgis/rest/services"
        "/OpenData/HVS_Networks_DataPortal/MapServer/3/query"
    ),
    "heavy_vehicle": (
        "https://gisservices.mainroads.wa.gov.au/arcgis/rest/services"
        "/OpenData/HVS_Networks_DataPortal/MapServer/1/query"
    ),
    "road_stopping": (
        "https://gisservices.mainroads.wa.gov.au/arcgis/rest/services"
        "/OpenData/RoadAssets_DataPortal/MapServer/19/query"
    ),
}


def _geo_bool(val: Any) -> Optional[bool]:
    """Coerce ArcGIS field value to bool (handles 'Y'/'N', 1/0, True/False, strings)."""
    if val is None:
        return None
    if isinstance(val, bool):
        return val
    if isinstance(val, int):
        return bool(val)
    s = str(val).strip().lower()
    if s in ("yes", "y", "true", "1"):
        return True
    if s in ("no", "n", "false", "0"):
        return False
    return None


def _stable_id(source: str, props: Dict[str, Any], lat: float, lng: float) -> str:
    obj_id = props.get("OBJECTID") or props.get("objectid")
    if obj_id is not None:
        raw = f"{source}::{obj_id}"
    else:
        raw = f"{source}::{round(lat, 5)}::{round(lng, 5)}"
    h = hashlib.sha256(raw.encode()).digest()
    return base64.urlsafe_b64encode(h).decode("ascii").rstrip("=")[:20]


def _geojson_centroid(geometry: Dict[str, Any]) -> Optional[Tuple[float, float]]:
    """Extract (lat, lng) from a GeoJSON geometry (Point or Polygon centroid)."""
    gtype = geometry.get("type")
    coords = geometry.get("coordinates")
    if not coords:
        return None
    if gtype == "Point":
        return float(coords[1]), float(coords[0])
    if gtype in ("Polygon", "MultiPolygon"):
        # Rough centroid: average all outer ring coordinates
        if gtype == "Polygon":
            ring = coords[0]
        else:
            ring = coords[0][0]
        lats = [c[1] for c in ring]
        lngs = [c[0] for c in ring]
        return sum(lats) / len(lats), sum(lngs) / len(lngs)
    return None


def _gov_cache_get(key: str) -> Optional[List[RestArea]]:
    with _gov_cache_lock:
        entry = _gov_cache.get(key)
        if entry and (time.monotonic() - entry[0]) < _GOV_CACHE_TTL:
            return entry[1]
    return None


def _gov_cache_set(key: str, areas: List[RestArea]) -> None:
    with _gov_cache_lock:
        _gov_cache[key] = (time.monotonic(), areas)


async def _fetch_qld_rest_areas(client: httpx.AsyncClient) -> List[RestArea]:
    """Fetch ALL QLD government rest areas (state-wide, cached in memory)."""
    cached = _gov_cache_get("qld")
    if cached is not None:
        logger.info("rest_areas: QLD cache hit (%d areas)", len(cached))
        return cached

    features: List[Dict[str, Any]] = []

    async def _get_page(offset: int) -> List[Dict[str, Any]]:
        params = {
            "where": "1=1", "outFields": "*", "f": "geojson",
            "resultRecordCount": "1000", "resultOffset": str(offset),
        }
        r = await client.get(_QLD_BASE_URL, params=params, timeout=_GOV_TIMEOUT)
        r.raise_for_status()
        return r.json().get("features") or []

    results = await asyncio.gather(
        _get_page(0), _get_page(1000), return_exceptions=True,
    )
    for i, res in enumerate(results):
        if isinstance(res, Exception):
            logger.warning("rest_areas: QLD page offset=%d failed: %r", i * 1000, res)
        elif isinstance(res, list):
            features.extend(res)

    areas: List[RestArea] = []
    for feat in features:
        geometry = feat.get("geometry") or {}
        props: Dict[str, Any] = feat.get("properties") or {}

        centroid = _geojson_centroid(geometry)
        if centroid is None:
            continue
        lat, lng = centroid

        fac = RestFacilities(
            toilets=_geo_bool(props.get("toilets") or props.get("TOILETS")),
            drinking_water=_geo_bool(props.get("water") or props.get("WATER") or props.get("drinking_water")),
            bbq=_geo_bool(props.get("BBQ") or props.get("bbq")),
            shelter=_geo_bool(props.get("shelter") or props.get("SHELTER")),
        )

        score = 0
        if fac.toilets:
            score += 1
        if fac.drinking_water:
            score += 1
        if fac.shelter:
            score += 1
        if fac.bbq:
            score += 1

        # accessible_parking is a bonus
        acc = _geo_bool(props.get("accessible_parking") or props.get("accessibleParking") or props.get("ACCESSIBLE_PARKING"))
        if acc:
            score += 1

        name = props.get("name") or props.get("NAME") or props.get("AMENITY_NAME") or None

        areas.append(RestArea(
            id=_stable_id("qld", props, lat, lng),
            name=name,
            lat=lat,
            lng=lng,
            type="rest_area",
            quality_score=score,
            facilities=fac,
            source="qld_gov",
        ))

    logger.info("rest_areas: QLD fetched %d features → %d areas", len(features), len(areas))
    _gov_cache_set("qld", areas)
    return areas


async def _fetch_wa_rest_areas(client: httpx.AsyncClient) -> List[RestArea]:
    """Fetch ALL WA government rest areas state-wide (cached in memory).

    The host may block non-AU IPs - any request failure is silently skipped.
    """
    cached = _gov_cache_get("wa")
    if cached is not None:
        logger.info("rest_areas: WA cache hit (%d areas)", len(cached))
        return cached

    # tier → (quality_score_base, truck_friendly)
    tier_map = {
        "major":          (3, False),
        "minor":          (1, False),
        "heavy_vehicle":  (2, True),
        "road_stopping":  (0, False),
    }

    params: Dict[str, str] = {"where": "1=1", "outFields": "*", "f": "geojson"}

    async def _fetch_tier(tier: str, url: str) -> Tuple[str, List[Dict[str, Any]]]:
        try:
            r = await client.get(url, params=params, timeout=_GOV_TIMEOUT)
            if not r.is_success:
                logger.warning("rest_areas: WA tier=%s returned HTTP %d - skipping", tier, r.status_code)
                return tier, []
            return tier, r.json().get("features") or []
        except Exception as exc:
            logger.warning("rest_areas: WA tier=%s connection error - skipping: %r", tier, exc)
            return tier, []

    tier_results = await asyncio.gather(
        *[_fetch_tier(tier, url) for tier, url in _WA_ENDPOINTS.items()],
    )
    tier_features: Dict[str, List[Dict[str, Any]]] = {}
    for tier, feats in tier_results:
        tier_features[tier] = feats

    areas: List[RestArea] = []
    for tier, features in tier_features.items():
        base_score, truck_friendly = tier_map[tier]

        for feat in features:
            geometry = feat.get("geometry") or {}
            props: Dict[str, Any] = feat.get("properties") or {}

            centroid = _geojson_centroid(geometry)
            if centroid is None:
                continue
            lat, lng = centroid

            fac = RestFacilities(
                toilets=_geo_bool(props.get("TOILETS") or props.get("toilets")),
                drinking_water=_geo_bool(props.get("WATER") or props.get("water") or props.get("DRINKING_WATER")),
                bbq=_geo_bool(props.get("BBQ") or props.get("bbq")),
                shelter=_geo_bool(props.get("SHELTER") or props.get("shelter")),
                picnic_table=_geo_bool(props.get("PICNIC") or props.get("picnic") or props.get("PICNIC_TABLE")),
            )

            score = base_score
            if fac.toilets:
                score += 1
            if fac.drinking_water:
                score += 1
            if fac.shelter:
                score += 1
            if fac.bbq or fac.picnic_table:
                score += 1

            name = (
                props.get("NAME") or props.get("name")
                or props.get("SITE_NAME") or props.get("ASSET_NAME")
                or None
            )

            area = RestArea(
                id=_stable_id(f"wa_{tier}", props, lat, lng),
                name=name,
                lat=lat,
                lng=lng,
                type="rest_area",
                quality_score=score,
                facilities=fac,
                source=f"wa_gov_{tier}",
            )
            # Stash truck_friendly in a way visible to callers without changing the contract
            if truck_friendly:
                area.source = "wa_gov_hv"  # heavy vehicle tier - truck_friendly implied
            areas.append(area)

    total_feats = sum(len(f) for f in tier_features.values())
    logger.info("rest_areas: WA fetched %d features → %d areas", total_feats, len(areas))
    _gov_cache_set("wa", areas)
    return areas


async def _fetch_nsw_rest_areas(
    client: httpx.AsyncClient, bbox: Tuple[float, float, float, float]
) -> List[RestArea]:
    """Fetch NSW rest areas from TfNSW Open Data spatial API.

    bbox = (min_lng, min_lat, max_lng, max_lat)
    Skipped if nsw_rest_areas_api_key is empty.
    """
    api_key = getattr(settings, "nsw_rest_areas_api_key", "")
    if not api_key or not getattr(settings, "nsw_rest_areas_enabled", False):
        return []

    sql = (
        f"SELECT * FROM rest_areas "
        f"WHERE lat BETWEEN {bbox[1]} AND {bbox[3]} "
        f"AND lon BETWEEN {bbox[0]} AND {bbox[2]} "
        f"LIMIT 300"
    )

    try:
        r = await client.get(
            settings.nsw_rest_areas_url,
            params={"format": "json", "q": sql},
            headers={"Authorization": f"apikey {api_key}"},
            timeout=httpx.Timeout(15.0, connect=10.0),
        )
        r.raise_for_status()
        data = r.json()
    except Exception as exc:
        logger.warning("rest_areas: NSW rest areas fetch failed: %r", exc)
        return []

    # API may return {"features": [...]} or {"rows": [...]}
    rows = data.get("features") or data.get("rows") or []
    if not isinstance(rows, list):
        logger.warning("rest_areas: NSW rest areas unexpected response shape")
        return []

    areas: List[RestArea] = []
    for row in rows:
        try:
            # Rows may be dicts or GeoJSON Feature objects
            props: Dict[str, Any] = row.get("properties") or row if isinstance(row, dict) else {}
            lat_val = props.get("lat") or props.get("LAT") or props.get("latitude")
            lon_val = props.get("lon") or props.get("LON") or props.get("longitude")
            if lat_val is None or lon_val is None:
                continue
            lat = float(lat_val)
            lng = float(lon_val)

            fac = RestFacilities(
                toilets=_geo_bool(props.get("toilets") or props.get("TOILETS")),
                drinking_water=_geo_bool(props.get("water") or props.get("WATER") or props.get("drinking_water")),
                bbq=_geo_bool(props.get("bbq") or props.get("BBQ")),
                shelter=_geo_bool(props.get("shelter") or props.get("SHELTER")),
            )

            score = 0
            if fac.toilets:
                score += 1
            if fac.drinking_water:
                score += 1
            if fac.shelter:
                score += 1
            if fac.bbq:
                score += 1

            name = props.get("name") or props.get("NAME") or props.get("site_name") or None

            areas.append(RestArea(
                id=_stable_id("nsw_rest", props, lat, lng),
                name=name,
                lat=lat,
                lng=lng,
                type="rest_area",
                quality_score=score,
                facilities=fac,
                source="nsw_tfnsw",
            ))
        except Exception as exc:
            logger.warning("rest_areas: NSW rest area row parse error: %r", exc)

    logger.info("rest_areas: NSW rest areas returned %d rows → %d areas", len(rows), len(areas))
    return areas


# ──────────────────────────────────────────────────────────────
# Deduplication
# ──────────────────────────────────────────────────────────────

def _dedup(areas: List[RestArea], merge_km: float = 0.05) -> List[RestArea]:
    """Remove duplicate rest areas within merge_km of each other (keep higher quality).

    Uses a grid-cell approach for O(N) performance instead of O(N²).
    """
    # ~50m merge → grid cells of ~0.0005° ≈ 55m
    res = max(0.0005, merge_km / 111.0)
    grid: Dict[Tuple[int, int], RestArea] = {}
    for area in sorted(areas, key=lambda a: -a.quality_score):
        key = (int(area.lat / res), int(area.lng / res))
        if key not in grid:
            grid[key] = area
    return list(grid.values())


# ──────────────────────────────────────────────────────────────
# Fatigue gap analysis
# ──────────────────────────────────────────────────────────────

def _analyse_fatigue(
    rest_areas: List[RestArea],
    route_total_km: float,
    max_gap_km: float,
    rest_interval_km: float,
) -> List[FatigueWarning]:
    warnings: List[FatigueWarning] = []

    # Sort by km_along; use 0 for start if None
    sorted_areas = sorted(
        [a for a in rest_areas if a.km_along is not None],
        key=lambda a: a.km_along or 0.0,
    )

    # Build checkpoints: route start + each rest area + route end
    checkpoints: List[Tuple[float, Optional[RestArea]]] = [(0.0, None)]
    for a in sorted_areas:
        checkpoints.append((a.km_along or 0.0, a))
    checkpoints.append((route_total_km, None))

    # Check gaps between consecutive rest areas
    for i in range(1, len(checkpoints)):
        prev_km, prev_area = checkpoints[i - 1]
        curr_km, curr_area = checkpoints[i]
        gap = curr_km - prev_km

        if gap > max_gap_km:
            # Name the boundary points
            from_name = prev_area.name or f"{prev_km:.0f}km" if prev_area else "route start"
            to_name = curr_area.name or f"{curr_km:.0f}km" if curr_area else "route end"

            warnings.append(
                FatigueWarning(
                    type="long_gap",
                    message=(
                        f"No rest area for {gap:.0f}km between {from_name} and {to_name}. "
                        f"Consider a mandatory rest break."
                    ),
                    km_from=prev_km,
                    km_to=curr_km,
                    gap_km=round(gap, 1),
                    suggested_stop=None,
                )
            )

    # Suggest rest stops at regular intervals
    next_rest_km = rest_interval_km
    while next_rest_km < route_total_km:
        # Find the nearest rest area within ±30km of the suggested stop
        candidates = [
            a for a in sorted_areas
            if a.km_along is not None and abs((a.km_along or 0.0) - next_rest_km) <= 30.0
        ]
        best = max(candidates, key=lambda a: a.quality_score) if candidates else None

        warnings.append(
            FatigueWarning(
                type="suggested_rest",
                message=(
                    f"Suggested rest stop at {next_rest_km:.0f}km"
                    + (f" - {best.name}" if best and best.name else "")
                ),
                km_from=next_rest_km,
                km_to=None,
                gap_km=None,
                suggested_stop=best,
            )
        )
        next_rest_km += rest_interval_km

    return warnings


# ──────────────────────────────────────────────────────────────
# Places-store bridge - avoids duplicate Overpass calls
# ──────────────────────────────────────────────────────────────

_REST_CATEGORIES = ("rest_area", "camp", "toilet")

def _read_supabase_rest_areas(
    min_lat: float, min_lng: float, max_lat: float, max_lng: float,
) -> List[Dict[str, Any]]:
    """Read rest-area-relevant items from Supabase (persistent across instances).

    Returns items in the same format as _read_places_store.
    """
    try:
        from app.core.settings import settings
        supa_url = getattr(settings, "supabase_url", None)
        supa_key = getattr(settings, "supabase_anon_key", None)
        if not supa_url or not supa_key:
            return []

        import httpx as _httpx
        cats = ",".join(_REST_CATEGORIES)
        params = {
            "select": "osm_type,osm_id,lat,lng,name,category,tags",
            "lat": f"gte.{min_lat}",
            "lng": f"gte.{min_lng}",
            "category": f"in.({cats})",
            "limit": "2000",
        }
        # PostgREST needs separate params for range filters
        url = f"{supa_url}/rest/v1/roam_places_items"
        headers = {
            "apikey": supa_key,
            "Authorization": f"Bearer {supa_key}",
        }
        with _httpx.Client(timeout=5.0) as client:
            resp = client.get(
                url, headers=headers,
                params={
                    "select": "osm_type,osm_id,lat,lng,name,category,tags",
                    "lat": f"gte.{min_lat}",
                    "limit": "2000",
                    "category": f"in.({cats})",
                },
            )
            # Add remaining filters via extra params
            # PostgREST handles multiple same-key params
            resp = client.get(
                url, headers=headers,
                params=[
                    ("select", "osm_type,osm_id,lat,lng,name,category,tags"),
                    ("lat", f"gte.{min_lat}"),
                    ("lat", f"lte.{max_lat}"),
                    ("lng", f"gte.{min_lng}"),
                    ("lng", f"lte.{max_lng}"),
                    ("category", f"in.({cats})"),
                    ("limit", "2000"),
                ],
            )
            if resp.status_code != 200:
                logger.debug("rest_areas: Supabase returned %d", resp.status_code)
                return []
            rows = resp.json()
            return [
                {
                    "osm_type": r.get("osm_type"), "osm_id": r.get("osm_id"),
                    "lat": r.get("lat"), "lng": r.get("lng"),
                    "name": r.get("name"), "category": r.get("category"),
                    "tags": r.get("tags") or {},
                }
                for r in rows
            ]
    except Exception as e:
        logger.debug("rest_areas: Supabase read failed: %r", e)
        return []


def _read_places_store(
    min_lat: float, min_lng: float, max_lat: float, max_lng: float,
    conn,
) -> List[Dict[str, Any]]:
    """Read rest-area-relevant items from the places SQLite cache.

    Returns raw dicts with lat, lng, name, category, tags.
    Returns [] if the places table doesn't exist or is empty for this bbox.
    """
    try:
        placeholders = ",".join("?" for _ in _REST_CATEGORIES)
        sql = f"""
        SELECT osm_type, osm_id, lat, lng, name, category, tags_json
        FROM places_items
        WHERE lat >= ? AND lat <= ? AND lng >= ? AND lng <= ?
          AND category IN ({placeholders})
        LIMIT 2000
        """
        params: list = [min_lat, max_lat, min_lng, max_lng, *_REST_CATEGORIES]
        cur = conn.cursor()
        cur.execute(sql, params)
        rows = cur.fetchall()
        return [
            {
                "osm_type": r[0], "osm_id": r[1],
                "lat": r[2], "lng": r[3],
                "name": r[4], "category": r[5],
                "tags": orjson.loads(r[6]) if r[6] else {},
            }
            for r in rows
        ]
    except Exception as e:
        logger.debug("rest_areas: places_store read failed: %r", e)
        return []


def _place_item_to_rest_area(item: Dict[str, Any]) -> Optional[RestArea]:
    """Convert a places_items row into a RestArea."""
    lat = item.get("lat")
    lng = item.get("lng")
    if lat is None or lng is None:
        return None

    cat = item.get("category", "")
    tags = item.get("tags") or {}

    # Map places category → RestArea.type
    # Places stores highway=services as "rest_area", camp_site/caravan_site as "camp"
    type_map = {
        "rest_area": "rest_area",
        "camp": "camp_site",
        "toilet": "toilets",
    }
    osm_type = type_map.get(cat)
    if not osm_type:
        return None

    # Refine camp → camp_site vs caravan_site from original tags
    if cat == "camp":
        tourism = tags.get("tourism", "")
        if tourism == "caravan_site":
            osm_type = "caravan_site"

    # Build stable ID
    stable_id_raw = f"places::{osm_type}::{round(float(lat), 5)}::{round(float(lng), 5)}"
    h = hashlib.sha256(stable_id_raw.encode()).digest()
    stable_id = base64.urlsafe_b64encode(h).decode("ascii").rstrip("=")[:20]

    # Extract facilities from tags
    fac = RestFacilities(
        toilets=_tag_bool(tags, "toilets"),
        drinking_water=_tag_bool(tags, "drinking_water"),
        shower=_tag_bool(tags, "shower"),
        bbq=_tag_bool(tags, "bbq"),
        picnic_table=_tag_bool(tags, "picnic_table"),
        power_supply=_tag_bool(tags, "power_supply"),
        internet=_tag_bool(tags, "internet_access"),
        lit=_tag_bool(tags, "lit"),
        shelter=_tag_bool(tags, "covered") or _tag_bool(tags, "shelter"),
        capacity=int(tags["capacity"]) if tags.get("capacity", "").isdigit() else None,
    )

    score = sum([
        bool(fac.toilets), bool(fac.drinking_water), bool(fac.shelter),
        bool(fac.bbq or fac.picnic_table), bool(fac.lit),
    ])

    return RestArea(
        id=stable_id,
        name=item.get("name") or tags.get("name") or tags.get("ref") or None,
        lat=float(lat),
        lng=float(lng),
        type=osm_type,
        quality_score=score,
        facilities=fac,
        opening_hours=tags.get("opening_hours") or None,
        fee=_tag_bool(tags, "fee"),
        source="places_store",
    )


# ──────────────────────────────────────────────────────────────
# Main service class
# ──────────────────────────────────────────────────────────────

class RestAreas:
    def __init__(self, *, conn) -> None:
        self.conn = conn

    async def along_route(
        self,
        *,
        polyline6: str,
        sample_interval_km: float = 8.0,
        buffer_km: float = 5.0,
    ) -> RestAreaOverlay:
        algo_version = settings.rest_algo_version
        cache_seconds = settings.rest_cache_seconds

        key = _rest_key(polyline6, sample_interval_km, buffer_km, algo_version)

        # Cache hit
        cached = get_rest_area_pack(self.conn, key)
        if cached and is_fresh(cached.get("created_at", ""), max_age_s=cache_seconds):
            logger.debug("rest_areas cache hit: %s", key)
            return RestAreaOverlay.model_validate(cached)

        # Build route samples
        samples = _sample_polyline_with_km(polyline6, sample_interval_km)
        if not samples:
            logger.warning("rest_areas: failed to decode polyline")
            overlay = RestAreaOverlay(
                rest_key=key,
                polyline6=polyline6,
                algo_version=algo_version,
                created_at=utc_now_iso(),
                warnings=["Failed to decode route polyline"],
            )
            put_rest_area_pack(
                self.conn,
                rest_key=key,
                created_at=overlay.created_at,
                algo_version=algo_version,
                pack=overlay.model_dump(),
            )
            return overlay

        route_total_km = samples[-1][2] if samples else 0.0

        # Build bbox around route
        min_lat, min_lng, max_lat, max_lng = bbox_from_coords(samples, buffer_km)

        warnings: List[str] = []
        raw_areas: List[RestArea] = []

        # Build spatial grid index for fast nearest-sample lookups
        grid = RouteGrid(samples)

        # ── Strategy: read from places SQLite cache first (populated by
        # the places step which runs before overlays).  This avoids a
        # duplicate Overpass call that competes for the same instances and
        # adds 10-90s of latency.  Fall back to Overpass only if the
        # places store yields nothing.
        t0 = time.monotonic()
        places_items = _read_places_store(min_lat, min_lng, max_lat, max_lng, self.conn)
        if places_items:
            for item in places_items:
                area = _place_item_to_rest_area(item)
                if area is None:
                    continue
                dist_km, km_along = grid.dist_and_km(area.lat, area.lng)
                if dist_km > buffer_km:
                    continue
                area.distance_from_route_km = round(dist_km, 2)
                area.km_along = round(km_along, 2)
                raw_areas.append(area)
            logger.info(
                "rest_areas: read %d items from places store → %d in corridor (%.1fs)",
                len(places_items), len(raw_areas), time.monotonic() - t0,
            )
        else:
            # Fallback 1: try Supabase (fast, persistent across instances)
            supa_items = _read_supabase_rest_areas(min_lat, min_lng, max_lat, max_lng)
            if supa_items:
                for item in supa_items:
                    area = _place_item_to_rest_area(item)
                    if area is None:
                        continue
                    dist_km, km_along = grid.dist_and_km(area.lat, area.lng)
                    if dist_km > buffer_km:
                        continue
                    area.distance_from_route_km = round(dist_km, 2)
                    area.km_along = round(km_along, 2)
                    raw_areas.append(area)
                logger.info(
                    "rest_areas: read %d items from Supabase → %d in corridor (%.1fs)",
                    len(supa_items), len(raw_areas), time.monotonic() - t0,
                )
            else:
                # Fallback 2: direct Overpass query (last resort)
                ql = _build_overpass_query(min_lat, min_lng, max_lat, max_lng)
                try:
                    overpass_result = await _fetch_overpass(client=None, ql=ql)
                except Exception as e:
                    logger.warning("rest_areas: Overpass query failed: %r", e)
                    warnings.append(f"Overpass query failed: {e}")
                    overpass_result = {}
                logger.info("rest_areas: Overpass fallback completed in %.1fs", time.monotonic() - t0)

                for el in overpass_result.get("elements") or []:
                    area = _parse_element(el)
                    if area is None:
                        continue
                    dist_km, km_along = grid.dist_and_km(area.lat, area.lng)
                    if dist_km > buffer_km:
                        continue
                    area.distance_from_route_km = round(dist_km, 2)
                    area.km_along = round(km_along, 2)
                    raw_areas.append(area)

        # Deduplicate
        areas = _dedup(raw_areas)

        # Sort by km along route
        areas.sort(key=lambda a: a.km_along or 0.0)

        logger.info(
            "rest_areas: polyline=%d chars, bbox=%.3f,%.3f→%.3f,%.3f, "
            "raw=%d deduped=%d route_km=%.1f",
            len(polyline6), min_lat, min_lng, max_lat, max_lng,
            len(raw_areas), len(areas), route_total_km,
        )

        # Fatigue analysis
        fatigue = _analyse_fatigue(
            areas,
            route_total_km,
            max_gap_km=settings.fatigue_max_gap_km,
            rest_interval_km=settings.fatigue_rest_interval_km,
        )

        overlay = RestAreaOverlay(
            rest_key=key,
            polyline6=polyline6,
            algo_version=algo_version,
            created_at=utc_now_iso(),
            rest_areas=areas,
            fatigue_warnings=fatigue,
            warnings=warnings,
        )

        put_rest_area_pack(
            self.conn,
            rest_key=key,
            created_at=overlay.created_at,
            algo_version=algo_version,
            pack=overlay.model_dump(),
        )

        return overlay
