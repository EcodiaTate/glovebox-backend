# app/services/speed_cameras.py
"""
Speed camera & road occupancy overlay service for Roam.

Data sources (auto-selected by bbox):
  - NSW TfNSW Speed Cameras ArcGIS FeatureServer (CC-BY 3.0 AU, no auth)
    https://portal.data.nsw.gov.au/arcgis/rest/services/Hosted/TFNSW_Speed_Cameras_public/FeatureServer/0/query
  - QLD Speed Cameras CSV (CC-BY 4.0, no auth)
    https://open-crime-data.s3-ap-southeast-2.amazonaws.com/Crime%20Statistics/RSCT_sites.csv
  - ACT Speed Cameras JSON (ACT Government open data, no auth)
    https://www.data.act.gov.au/api/views/426s-vdu4/rows.json?accessType=DOWNLOAD
  - Brisbane Council Planned Temporary Road Occupancies (CC-BY 4.0, no auth)
    https://data.brisbane.qld.gov.au/api/explore/v2.1/catalog/datasets/planned-temporary-road-occupancies/records

Algorithm
─────────
1. Decode route polyline6 to (lat, lng) pairs.
2. Compute bounding box with buffer.
3. Query NSW speed cameras ArcGIS within bbox (always).
4. If bbox overlaps QLD, fetch QLD cameras CSV and filter to bbox (7d TTL cache).
5. If bbox overlaps ACT, fetch ACT cameras JSON and filter to bbox (7d TTL cache).
6. If bbox overlaps Brisbane area, also query road occupancies.
7. For each camera, compute haversine distance from nearest route point.
8. Classify cameras: fixed_speed, red_light_speed, school_zone.
9. Sort cameras by distance from route.
10. Cache result in speed_camera_packs for 24 hours (cameras rarely move).
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
from typing import Any, Dict, List, Optional, Tuple

import httpx

from app.core.contracts import RoadBlackSpot, SpeedCamera, RoadOccupancy, SpeedCamerasOverlay
from app.core.settings import settings
from app.core.storage import get_cameras_pack, put_cameras_pack
from app.core.time import utc_now_iso
from app.core.geo import bbox_from_coords, bbox_overlaps, decode_polyline6, haversine_km, min_dist_to_route, sample_route, RouteGrid
from app.core.http_client import http_client
from app.core.cache_utils import is_fresh, stable_key

logger = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════════
# Constants
# ══════════════════════════════════════════════════════════════

# Brisbane bounding box for conditional query
_BRISBANE_LAT_MIN = -27.7
_BRISBANE_LAT_MAX = -27.2
_BRISBANE_LNG_MIN = 152.8
_BRISBANE_LNG_MAX = 153.3

# QLD bounding box
_QLD_LAT_MIN = -29.2
_QLD_LAT_MAX = -10.0
_QLD_LNG_MIN = 137.9
_QLD_LNG_MAX = 153.6

# ACT bounding box
_ACT_LAT_MIN = -35.95
_ACT_LAT_MAX = -35.12
_ACT_LNG_MIN = 148.76
_ACT_LNG_MAX = 149.40

_QLD_BLACK_SPOTS_URL = "https://data.qldtraffic.qld.gov.au/blackspotSites.geojson"
_ACT_CAMERAS_URL = (
    "https://www.data.act.gov.au/api/views/426s-vdu4/rows.json?accessType=DOWNLOAD"
)

_MAX_CAMERAS = 500
_MAX_OCCUPANCIES = 100
_HTTP_TIMEOUT = 25.0

# QLD/ACT static camera files are cached state-wide; TTL is separate from route pack
_STATIC_CACHE_TTL_S = 7 * 86_400  # 7 days


# ══════════════════════════════════════════════════════════════
# Geometry helpers
# ══════════════════════════════════════════════════════════════

def _bbox_overlaps_brisbane(
    min_lat: float, min_lng: float, max_lat: float, max_lng: float,
) -> bool:
    return bbox_overlaps(
        min_lat, min_lng, max_lat, max_lng,
        _BRISBANE_LAT_MIN, _BRISBANE_LAT_MAX, _BRISBANE_LNG_MIN, _BRISBANE_LNG_MAX,
    )


# ══════════════════════════════════════════════════════════════
# Overpass helper — delegates to global gate
# ══════════════════════════════════════════════════════════════


async def _overpass_query(
    client: httpx.AsyncClient,
    ql: str,
    warnings: List[str],
    label: str,
) -> Optional[Dict[str, Any]]:
    """Delegate to global Overpass gate."""
    from app.core.overpass import overpass_fetch
    try:
        return await overpass_fetch(ql, label=f"speed_cameras_{label}")
    except Exception as e:
        warnings.append(f"speed_cameras:{label}_overpass: {e}")
        return None


# ══════════════════════════════════════════════════════════════
# Cache helpers
# ══════════════════════════════════════════════════════════════


def _ensure_static_table(conn) -> None:
    """Separate table for state-wide static camera data (QLD, ACT)."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS cameras_static (
            source TEXT PRIMARY KEY,
            fetched_at TEXT NOT NULL,
            cameras_json BLOB NOT NULL
        );
        """
    )
    conn.commit()


def _get_static(conn, source: str) -> Optional[Tuple[str, list]]:
    try:
        import orjson
        cur = conn.execute(
            "SELECT fetched_at, cameras_json FROM cameras_static WHERE source=?;", (source,)
        )
        row = cur.fetchone()
        if not row:
            return None
        return str(row[0]), orjson.loads(row[1])
    except Exception:
        return None


def _put_static(conn, source: str, fetched_at: str, cameras: list) -> None:
    try:
        import orjson
        blob = orjson.dumps(cameras)
        conn.execute(
            "INSERT OR REPLACE INTO cameras_static (source, fetched_at, cameras_json) VALUES (?,?,?);",
            (source, fetched_at, blob),
        )
        conn.commit()
    except Exception as e:
        logger.warning("cameras_static: write failed for %s: %s", source, e)


# ══════════════════════════════════════════════════════════════
# Camera type classification
# ══════════════════════════════════════════════════════════════

def _classify_camera(attrs: Dict[str, Any]) -> str:
    """
    Classify a camera based on its attributes.

    - sz_ field (school zone flag): if truthy → "school_zone"
    - cameras field containing "red light": → "red_light_speed"
    - Otherwise: "fixed_speed"
    """
    sz = attrs.get("sz_")
    if sz and str(sz).strip().lower() not in ("", "0", "no", "false", "n", "null"):
        return "school_zone"

    cameras_desc = str(attrs.get("cameras") or "").lower()
    if "red light" in cameras_desc:
        return "red_light_speed"

    return "fixed_speed"


# ══════════════════════════════════════════════════════════════
# Misc helpers
# ══════════════════════════════════════════════════════════════

def _parse_float(v: Any) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _make_camera_id(source: str, lat: float, lng: float) -> str:
    raw = f"{source}::{round(lat, 5)}::{round(lng, 5)}"
    return base64.urlsafe_b64encode(
        hashlib.sha256(raw.encode()).digest()
    ).decode("ascii").rstrip("=")[:20]


# ══════════════════════════════════════════════════════════════
# Data fetchers
# ══════════════════════════════════════════════════════════════

async def _fetch_nsw_cameras(
    client: httpx.AsyncClient,
    min_lat: float,
    min_lng: float,
    max_lat: float,
    max_lng: float,
    rgrid: "RouteGrid",
    warnings: List[str],
) -> List[SpeedCamera]:
    """Query NSW TfNSW Speed Cameras ArcGIS FeatureServer within bbox."""
    geometry = f"{min_lng},{min_lat},{max_lng},{max_lat}"
    params = {
        "where": "1=1",
        "geometry": geometry,
        "geometryType": "esriGeometryEnvelope",
        "inSR": "4326",
        "outFields": "*",
        "outSR": "4326",
        "f": "json",
    }

    try:
        resp = await client.get(settings.nsw_speed_cameras_url, params=params, timeout=_HTTP_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        warnings.append(f"speed_cameras:nsw_fetch: {e}")
        return []

    features = data.get("features") or []
    cameras: List[SpeedCamera] = []

    for feat in features:
        try:
            attrs = feat.get("attributes") or {}

            lat = _parse_float(attrs.get("lat_1"))
            lng = _parse_float(attrs.get("long_1"))

            if lat is None or lng is None:
                geom = feat.get("geometry") or {}
                lat = _parse_float(geom.get("y"))
                lng = _parse_float(geom.get("x"))

            if lat is None or lng is None:
                continue

            camera_type = _classify_camera(attrs)
            location = str(attrs.get("location") or "").strip()
            road = str(attrs.get("road") or "").strip() or None
            suburb = str(attrs.get("suburb_town") or "").strip() or None
            location_desc = location or road or "Unknown location"
            dist_km = rgrid.dist(lat, lng)

            cameras.append(SpeedCamera(
                id=_make_camera_id("nsw_tfnsw", lat, lng),
                source="nsw_tfnsw",
                camera_type=camera_type,
                location_desc=location_desc,
                road=road,
                suburb=suburb,
                lat=lat,
                lng=lng,
                is_school_zone=(camera_type == "school_zone"),
                distance_from_route_km=round(dist_km, 2),
            ))
        except Exception as e:
            warnings.append(f"speed_cameras:nsw_parse: {e}")

    logger.info("speed_cameras: NSW returned %d features → %d cameras", len(features), len(cameras))
    return cameras


async def _fetch_qld_cameras(
    client: httpx.AsyncClient,
    conn,
    min_lat: float,
    min_lng: float,
    max_lat: float,
    max_lng: float,
    rgrid: "RouteGrid",
    warnings: List[str],
) -> List[SpeedCamera]:
    """
    Fetch QLD speed cameras from OpenStreetMap via Overpass API.

    The QLD Government CSV (RSCT_sites.csv) contains no coordinates — only
    site codes and location names — so we use OSM's `highway=speed_camera`
    and `enforcement=maxspeed` nodes instead.  Overpass supports bbox
    filtering natively, so we only fetch cameras in the route corridor.

    Results are cached state-wide in cameras_static for 7 days.
    """
    _ensure_static_table(conn)

    # Cache key includes bbox so different routes get fresh Overpass results
    cache_key = f"qld_overpass_{min_lat:.2f}_{min_lng:.2f}_{max_lat:.2f}_{max_lng:.2f}"
    cached = _get_static(conn, cache_key)
    if cached:
        fetched_at, cached_list = cached
        if is_fresh(fetched_at, max_age_s=_STATIC_CACHE_TTL_S):
            logger.debug("speed_cameras: QLD Overpass cache hit")
        else:
            cached = None

    if cached is None:
        query = (
            f"[out:json][timeout:15];"
            f"("
            f"  node[\"highway\"=\"speed_camera\"]({min_lat},{min_lng},{max_lat},{max_lng});"
            f"  node[\"enforcement\"=\"maxspeed\"]({min_lat},{min_lng},{max_lat},{max_lng});"
            f");"
            f"out body;"
        )
        data = await _overpass_query(client, query, warnings, "qld")
        if data is None:
            return []

        elements = data.get("elements") or []
        all_cameras = []
        seen: set = set()
        for el in elements:
            lat = _parse_float(el.get("lat"))
            lng = _parse_float(el.get("lon"))
            if lat is None or lng is None:
                continue
            # Deduplicate by rounded coords (speed_camera + enforcement can overlap)
            coord_key = (round(lat, 5), round(lng, 5))
            if coord_key in seen:
                continue
            seen.add(coord_key)

            tags = el.get("tags") or {}
            location = tags.get("name") or tags.get("description") or ""
            maxspeed = tags.get("maxspeed") or ""

            all_cameras.append({
                "lat": lat,
                "lng": lng,
                "location": location,
                "maxspeed": maxspeed,
            })

        now = utc_now_iso()
        _put_static(conn, cache_key, now, all_cameras)
        cached = (now, all_cameras)
        logger.info("speed_cameras: QLD Overpass returned %d elements → %d cameras", len(elements), len(all_cameras))

    _, all_cameras = cached
    cameras: List[SpeedCamera] = []

    for item in all_cameras:
        lat, lng = item["lat"], item["lng"]
        dist_km = rgrid.dist(lat, lng)
        location_desc = item.get("location") or "Queensland"
        if item.get("maxspeed"):
            location_desc = f"{location_desc} ({item['maxspeed']} km/h)".strip(" ()")
            if not item.get("location"):
                location_desc = f"{item['maxspeed']} km/h zone"
        cameras.append(SpeedCamera(
            id=_make_camera_id("qld_osm", lat, lng),
            source="qld_osm",
            camera_type="fixed_speed",
            location_desc=location_desc,
            lat=lat,
            lng=lng,
            is_school_zone=False,
            distance_from_route_km=round(dist_km, 2),
        ))

    logger.info("speed_cameras: QLD in-bbox cameras=%d", len(cameras))
    return cameras


async def _fetch_act_cameras(
    client: httpx.AsyncClient,
    conn,
    min_lat: float,
    min_lng: float,
    max_lat: float,
    max_lng: float,
    rgrid: "RouteGrid",
    warnings: List[str],
) -> List[SpeedCamera]:
    """
    Fetch ACT speed cameras from Socrata JSON endpoint.
    Fields: latitude, longitude, camera_type, location_description,
            decommissioned (Y/N).
    """
    _ensure_static_table(conn)
    cached = _get_static(conn, "act_cameras")
    if cached:
        fetched_at, cached_list = cached
        if is_fresh(fetched_at, max_age_s=_STATIC_CACHE_TTL_S):
            logger.debug("speed_cameras: ACT cameras cache hit")
        else:
            cached = None

    if cached is None:
        try:
            resp = await client.get(_ACT_CAMERAS_URL, timeout=_HTTP_TIMEOUT)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            warnings.append(f"speed_cameras:act_fetch: {e}")
            return []

        all_cameras = []
        rows = data if isinstance(data, list) else (data.get("data") or [])
        # Socrata JSON can be columnar — handle both dict-rows and list-rows
        meta_cols: Optional[List[str]] = None
        if isinstance(data, dict) and "meta" in data:
            try:
                meta_cols = [c["fieldName"] for c in data["meta"]["view"]["columns"]]
            except Exception:
                meta_cols = None

        for row in rows:
            try:
                if meta_cols and isinstance(row, list):
                    rec: Dict[str, Any] = dict(zip(meta_cols, row))
                else:
                    rec = row if isinstance(row, dict) else {}

                # Skip decommissioned
                decomm = str(rec.get("decommissioned") or rec.get("Decommissioned") or "").strip().upper()
                if decomm in ("Y", "YES", "TRUE", "1"):
                    continue

                lat = _parse_float(rec.get("latitude") or rec.get("Latitude"))
                lng = _parse_float(rec.get("longitude") or rec.get("Longitude"))
                if lat is None or lng is None:
                    continue

                cam_type_raw = str(rec.get("camera_type") or rec.get("CameraType") or "").lower()
                if "red" in cam_type_raw:
                    cam_type = "red_light_speed"
                else:
                    cam_type = "fixed_speed"

                location = str(
                    rec.get("location_description") or rec.get("location") or rec.get("Location") or ""
                ).strip()

                all_cameras.append({
                    "lat": lat,
                    "lng": lng,
                    "camera_type": cam_type,
                    "location": location,
                })
            except Exception as e:
                warnings.append(f"speed_cameras:act_parse_row: {e}")

        now = utc_now_iso()
        _put_static(conn, "act_cameras", now, all_cameras)
        cached = (now, all_cameras)

    _, all_cameras = cached
    cameras: List[SpeedCamera] = []

    for item in all_cameras:
        lat, lng = item["lat"], item["lng"]
        if not (min_lat <= lat <= max_lat and min_lng <= lng <= max_lng):
            continue
        dist_km = rgrid.dist(lat, lng)
        cameras.append(SpeedCamera(
            id=_make_camera_id("act", lat, lng),
            source="act_cameras",
            camera_type=item.get("camera_type", "fixed_speed"),
            location_desc=item.get("location") or "ACT",
            lat=lat,
            lng=lng,
            is_school_zone=False,
            distance_from_route_km=round(dist_km, 2),
        ))

    logger.info("speed_cameras: ACT in-bbox cameras=%d", len(cameras))
    return cameras


async def _fetch_brisbane_occupancies(
    client: httpx.AsyncClient,
    warnings: List[str],
) -> List[RoadOccupancy]:
    """
    Query Brisbane Council planned temporary road occupancies.

    This endpoint does not support spatial filtering, so we fetch
    recent records and return them all (the caller already checked
    that the bbox overlaps Brisbane).
    """
    params = {
        "limit": _MAX_OCCUPANCIES,
        "order_by": "start_date DESC",
    }

    try:
        resp = await client.get(
            settings.brisbane_road_occupancies_url, params=params, timeout=_HTTP_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        warnings.append(f"speed_cameras:brisbane_occupancies_fetch: {e}")
        return []

    records = data.get("results") or data.get("records") or []
    occupancies: List[RoadOccupancy] = []

    for rec in records:
        try:
            fields = rec if isinstance(rec, dict) else {}

            road = str(fields.get("road") or fields.get("road_name") or "").strip()
            if not road:
                continue

            suburb = str(fields.get("suburb") or "").strip() or None
            closure_type = str(fields.get("closure_type") or "").strip() or None
            traffic_impact = str(fields.get("traffic_impact") or "").strip() or None
            start_date = str(fields.get("start_date") or "").strip() or None
            end_date = str(fields.get("end_date") or "").strip() or None
            hours = str(fields.get("hours") or "").strip() or None

            raw_id = f"brisbane_occ::{road}::{start_date}::{suburb}"
            occ_id = base64.urlsafe_b64encode(
                hashlib.sha256(raw_id.encode()).digest()
            ).decode("ascii").rstrip("=")[:20]

            occupancies.append(RoadOccupancy(
                id=occ_id,
                source="brisbane_council",
                road=road,
                suburb=suburb,
                closure_type=closure_type,
                traffic_impact=traffic_impact,
                start_date=start_date,
                end_date=end_date,
                hours=hours,
            ))
        except Exception as e:
            warnings.append(f"speed_cameras:brisbane_parse: {e}")

    logger.info(
        "speed_cameras: Brisbane occupancies returned %d records → %d parsed",
        len(records), len(occupancies),
    )
    return occupancies


# ══════════════════════════════════════════════════════════════
# QLD Black Spots - high-crash-frequency sites (CC-BY 4.0, no auth)
# https://data.qldtraffic.qld.gov.au/blackspotSites.geojson
# ══════════════════════════════════════════════════════════════

async def _fetch_qld_black_spots(
    client: httpx.AsyncClient,
    conn,
    min_lat: float,
    min_lng: float,
    max_lat: float,
    max_lng: float,
    rgrid: "RouteGrid",
    warnings: List[str],
) -> List[RoadBlackSpot]:
    """
    Fetch QLD road black spots (high-crash-frequency sites) from the QLD
    Traffic GeoJSON feed. Results are cached state-wide for 7 days.
    """
    cache_key = "qld_black_spots_v1"
    raw = get_cameras_pack(conn, cache_key)
    features: List[Dict[str, Any]] = []

    if raw and is_fresh(raw.get("created_at", ""), max_age_s=_STATIC_CACHE_TTL_S):
        features = raw.get("features", [])
    else:
        try:
            resp = await client.get(_QLD_BLACK_SPOTS_URL, timeout=_HTTP_TIMEOUT)
            resp.raise_for_status()
            data = resp.json()
            features = data.get("features") or []
            put_cameras_pack(
                conn,
                cameras_key=cache_key,
                created_at=utc_now_iso(),
                algo_version="qld_blackspots_v1",
                pack={"created_at": utc_now_iso(), "features": features},
            )
        except Exception as e:
            warnings.append(f"speed_cameras:qld_black_spots_fetch: {e}")
            return []

    spots: List[RoadBlackSpot] = []
    for feat in features:
        if not isinstance(feat, dict):
            continue
        props = feat.get("properties") or {}
        geom = feat.get("geometry") or {}
        coords = geom.get("coordinates")
        if not coords or len(coords) < 2:
            continue
        try:
            lng, lat = float(coords[0]), float(coords[1])
        except (TypeError, ValueError):
            continue

        if not (min_lat <= lat <= max_lat and min_lng <= lng <= max_lng):
            continue

        dist_km = rgrid.dist(lat, lng)

        site_id = str(props.get("site_id") or props.get("SITE_ID") or props.get("id") or "")
        spot_id = base64.urlsafe_b64encode(
            hashlib.sha256(f"qld_bs:{site_id}:{lat:.5f}:{lng:.5f}".encode()).digest()
        ).decode()[:16]

        road = str(props.get("road_name") or props.get("ROAD_NAME") or props.get("road") or "").strip() or None
        desc = str(props.get("location_desc") or props.get("description") or props.get("suburb") or "").strip() or None

        crash_count_raw = props.get("crash_count") or props.get("CRASH_COUNT") or props.get("total_crashes")
        crash_count: Optional[int] = None
        if crash_count_raw is not None:
            try:
                crash_count = int(crash_count_raw)
            except (TypeError, ValueError):
                pass

        spots.append(RoadBlackSpot(
            id=spot_id,
            source="qld_blackspots",
            road=road,
            location_desc=desc,
            lat=lat,
            lng=lng,
            crash_count=crash_count,
            distance_from_route_km=round(dist_km, 2),
        ))

    logger.info("speed_cameras: QLD black spots in-bbox=%d", len(spots))
    return spots


# ══════════════════════════════════════════════════════════════
# Main service class
# ══════════════════════════════════════════════════════════════

class SpeedCameras:
    """
    Speed camera overlay service.

    Queries NSW TfNSW, QLD, ACT speed cameras and Brisbane road occupancies
    near a route polyline. Results are cached in SQLite for 24 hours.
    QLD/ACT static camera files are cached state-wide for 7 days.
    """

    def __init__(self, *, conn) -> None:
        self.conn = conn

    async def along_route(
        self,
        *,
        polyline6: str,
        buffer_km: float = 10.0,
        cache_seconds: int = settings.cameras_cache_seconds,
    ) -> SpeedCamerasOverlay:
        """
        Build a speed camera + road occupancy overlay along a route.

        Args:
            polyline6: Polyline6-encoded route geometry.
            buffer_km: Buffer around route for spatial query (default 10 km).
            cache_seconds: Cache TTL in seconds (default 86400 = 24 hours).

        Returns:
            SpeedCamerasOverlay with cameras sorted by distance from route.
        """
        algo_version = settings.cameras_algo_version

        cameras_key = stable_key("speed_cameras", {
            "polyline6": polyline6,
            "buffer_km": round(buffer_km, 1),
            "algo_version": algo_version,
        })

        cached = get_cameras_pack(self.conn, cameras_key)
        if cached:
            created_at = cached.get("created_at", "")
            if is_fresh(created_at, max_age_s=cache_seconds):
                logger.debug("speed_cameras cache hit: %s", cameras_key)
                return SpeedCamerasOverlay.model_validate(cached)

        coords = decode_polyline6(polyline6)
        if not coords:
            overlay = SpeedCamerasOverlay(
                cameras_key=cameras_key,
                polyline6=polyline6,
                algo_version=algo_version,
                created_at=utc_now_iso(),
                warnings=["Failed to decode route polyline."],
            )
            put_cameras_pack(
                self.conn,
                cameras_key=cameras_key,
                created_at=overlay.created_at,
                algo_version=algo_version,
                pack=overlay.model_dump(),
            )
            return overlay

        route_samples = sample_route(coords, interval_km=2.0)
        rgrid = RouteGrid(route_samples)
        min_lat, min_lng, max_lat, max_lng = bbox_from_coords(coords, buffer_km)
        warnings: List[str] = []

        # NSW bounding box (consistent with geo_registry)
        _NSW_LAT_MIN, _NSW_LAT_MAX = -37.6, -27.5
        _NSW_LNG_MIN, _NSW_LNG_MAX = 140.5, 154.0

        include_nsw = bbox_overlaps(
            min_lat, min_lng, max_lat, max_lng,
            _NSW_LAT_MIN, _NSW_LAT_MAX, _NSW_LNG_MIN, _NSW_LNG_MAX,
        )
        include_qld = bbox_overlaps(
            min_lat, min_lng, max_lat, max_lng,
            _QLD_LAT_MIN, _QLD_LAT_MAX, _QLD_LNG_MIN, _QLD_LNG_MAX,
        )
        include_act = bbox_overlaps(
            min_lat, min_lng, max_lat, max_lng,
            _ACT_LAT_MIN, _ACT_LAT_MAX, _ACT_LNG_MIN, _ACT_LNG_MAX,
        )
        include_brisbane = _bbox_overlaps_brisbane(min_lat, min_lng, max_lat, max_lng)

        # Build tasks dynamically with labels for clean result extraction
        task_labels: List[str] = []
        async with http_client(timeout=_HTTP_TIMEOUT) as client:
            tasks = []

            if include_nsw:
                task_labels.append("nsw")
                tasks.append(_fetch_nsw_cameras(
                    client, min_lat, min_lng, max_lat, max_lng,
                    rgrid, warnings,
                ))
            if include_qld:
                task_labels.append("qld")
                tasks.append(_fetch_qld_cameras(
                    client, self.conn, min_lat, min_lng, max_lat, max_lng,
                    rgrid, warnings,
                ))
            if include_act:
                task_labels.append("act")
                tasks.append(_fetch_act_cameras(
                    client, self.conn, min_lat, min_lng, max_lat, max_lng,
                    rgrid, warnings,
                ))
            if include_brisbane:
                task_labels.append("brisbane")
                tasks.append(_fetch_brisbane_occupancies(client, warnings))
            if include_qld:
                task_labels.append("qld_black_spots")
                tasks.append(_fetch_qld_black_spots(
                    client, self.conn, min_lat, min_lng, max_lat, max_lng,
                    rgrid, warnings,
                ))

            results = await asyncio.gather(*tasks, return_exceptions=True)

        # Unpack results by label
        result_map: Dict[str, Any] = dict(zip(task_labels, results))
        cameras: List[SpeedCamera] = []
        occupancies: List[RoadOccupancy] = []
        black_spots: List[RoadBlackSpot] = []

        for label in ("nsw", "qld", "act"):
            r = result_map.get(label)
            if r is None:
                continue
            if isinstance(r, list):
                cameras.extend(r)
            elif isinstance(r, Exception):
                warnings.append(f"speed_cameras:{label}_error: {r}")

        r = result_map.get("brisbane")
        if isinstance(r, list):
            occupancies = r
        elif isinstance(r, Exception):
            warnings.append(f"speed_cameras:brisbane_error: {r}")

        r = result_map.get("qld_black_spots")
        if isinstance(r, list):
            black_spots = r
        elif isinstance(r, Exception):
            warnings.append(f"speed_cameras:qld_black_spots_error: {r}")

        cameras = [
            c for c in cameras
            if c.distance_from_route_km is not None and c.distance_from_route_km <= buffer_km
        ]
        cameras.sort(key=lambda c: c.distance_from_route_km or 0.0)

        if len(cameras) > _MAX_CAMERAS:
            warnings.append(f"{len(cameras)} cameras found; limited to {_MAX_CAMERAS}.")
            cameras = cameras[:_MAX_CAMERAS]

        logger.info(
            "speed_cameras: polyline=%d chars, bbox=%.3f,%.3f→%.3f,%.3f, "
            "cameras=%d (nsw=%s, qld=%s, act=%s), occupancies=%d, black_spots=%d",
            len(polyline6), min_lat, min_lng, max_lat, max_lng,
            len(cameras), include_nsw, include_qld, include_act,
            len(occupancies), len(black_spots),
        )

        created_at = utc_now_iso()
        overlay = SpeedCamerasOverlay(
            cameras_key=cameras_key,
            polyline6=polyline6,
            algo_version=algo_version,
            created_at=created_at,
            cameras=cameras,
            road_occupancies=occupancies,
            black_spots=black_spots,
            warnings=warnings,
        )

        put_cameras_pack(
            self.conn,
            cameras_key=cameras_key,
            created_at=created_at,
            algo_version=algo_version,
            pack=overlay.model_dump(),
        )

        return overlay
