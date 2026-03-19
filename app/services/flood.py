# app/services/flood.py
"""
Flood gauge overlay service - BOM KiWIS real-time river heights.

Station list  : bom.gov.au/waterdata/data/stationdata.json (~8000 stations)
Live readings : BOM KiWIS queryServices API (no auth required)

Attribution is legally required: DATA_OWNER_NAME must be displayed
for each station per the data owner's licence conditions.

Design:
- Station list is cached in SQLite and refreshed every flood_station_refresh_hours.
- A simple in-process sorted list is built from the cached stations so bbox
  queries can be answered without hitting the BOM station endpoint every call.
- For a given bbox, up to _MAX_STATIONS_PER_REQUEST stations are selected and
  their latest Water Course Level timeseries are fetched concurrently.
- Severity is relative (ratio vs. 24h-ago reading); no official thresholds.
- Trend is computed from the last 3–6 hours of readings.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
from datetime import datetime, timedelta, timezone
from typing import Any, List, Optional, Tuple

import httpx
from shapely.geometry import Point, Polygon
from shapely.ops import unary_union

from app.core.contracts import BBox4, FloodCamera, FloodCatchment, FloodGauge, FloodOverlay
from app.core.settings import settings
from app.core.storage import (
    get_flood_pack,
    get_flood_stations,
    put_flood_pack,
    put_flood_stations,
)
from app.core.time import utc_now_iso
from app.core.geo import decode_polyline6, haversine_km
from app.core.cache_utils import is_fresh, stable_key


# ══════════════════════════════════════════════════════════════
# Constants
# ══════════════════════════════════════════════════════════════

_MAX_STATIONS_PER_REQUEST = 20   # Cap KiWIS calls per bbox query
_MAX_CONCURRENT_KIWIS = 5        # httpx semaphore for KiWIS reads
_KIWIS_THROTTLE_S = 0.5          # Delay between batches of concurrent calls
_TREND_HOURS = 6                 # Hours of history used for trend detection
_STEADY_THRESHOLD = 0.05         # < 5% change → "steady"

# BOM station data: filter to level/discharge parameters
_WANTED_PARAM_TYPES = {"Water Course Level", "Water Course Discharge"}

# QLD flood cameras GeoJSON feed (CC-BY 4.0, no auth)
_QLD_FLOOD_CAMERAS_URL = "https://data.qldtraffic.qld.gov.au/floodcameras.geojson"
_QLD_FLOOD_CAMERAS_TTL = 300  # 5 min - real-time feed


# ══════════════════════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════════════════════


def _catchment_to_shape(catchment) -> Optional[object]:
    """Build a Shapely geometry from a FloodCatchment's GeoJSON rings."""
    try:
        geom = catchment.geometry
        geo_type = geom.get("type", "")
        coords = geom.get("coordinates") or []
        if geo_type == "Polygon":
            if not coords:
                return None
            return Polygon(coords[0], coords[1:])
        if geo_type == "MultiPolygon":
            polys = [Polygon(rings[0], rings[1:]) for rings in coords if rings]
            return unary_union(polys) if polys else None
    except Exception:
        return None
    return None


def _route_intersects_warning_catchments(
    coords: List[Tuple[float, float]],
    warning_catchments: list,
) -> bool:
    """
    Point-in-polygon test: return True if any route point falls within any
    level='warning' catchment polygon. Replaces the previous bbox fast-path
    which produced false positives for non-rectangular catchments.
    """
    shapes = [_catchment_to_shape(c) for c in warning_catchments]
    shapes = [s for s in shapes if s is not None]
    if not shapes:
        return False
    for lat, lng in coords:
        pt = Point(lng, lat)
        for shape in shapes:
            try:
                if shape.contains(pt):
                    return True
            except Exception:
                continue
    return False


def _utc_ago_iso(hours: float) -> str:
    dt = datetime.now(timezone.utc) - timedelta(hours=hours)
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


def _parse_float(v: Any) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# ══════════════════════════════════════════════════════════════
# Station list management
# ══════════════════════════════════════════════════════════════

def _filter_stations(raw_stations: list) -> list:
    """
    Keep only gauges with Water Course Level or Discharge parameters
    that have valid lat/lng.
    """
    seen: dict[str, dict] = {}
    for s in raw_stations:
        param = s.get("parametertype_name", "")
        if param not in _WANTED_PARAM_TYPES:
            continue
        try:
            lat = float(s["station_latitude"])
            lng = float(s["station_longitude"])
        except (KeyError, TypeError, ValueError):
            continue
        # Prefer Water Course Level over Discharge for the same station
        sno = str(s.get("station_no", ""))
        if not sno:
            continue
        existing = seen.get(sno)
        if existing is None or param == "Water Course Level":
            seen[sno] = {
                "station_no": sno,
                "station_name": str(s.get("station_name", sno)),
                "lat": lat,
                "lng": lng,
                "data_owner": str(s.get("DATA_OWNER_NAME", "Bureau of Meteorology")),
            }
    return list(seen.values())


def _stations_in_bbox(stations: list, bbox: BBox4) -> list:
    return [
        s for s in stations
        if bbox.minLat <= s["lat"] <= bbox.maxLat
        and bbox.minLng <= s["lng"] <= bbox.maxLng
    ]


async def _fetch_station_list(client: httpx.AsyncClient) -> list:
    resp = await client.get(settings.bom_station_data_url, timeout=60.0)
    resp.raise_for_status()
    data = resp.json()
    # The BOM station JSON is a list of dicts
    if isinstance(data, list):
        return _filter_stations(data)
    # Some versions wrap in a dict
    if isinstance(data, dict):
        for key in ("stationList", "stations", "data", "features"):
            if key in data and isinstance(data[key], list):
                return _filter_stations(data[key])
    return []


# ══════════════════════════════════════════════════════════════
# BOM Flood Watch/Warning catchment polygons (ArcGIS FeatureServer)
# ══════════════════════════════════════════════════════════════

def _arcgis_rings_to_geojson(rings: list) -> dict:
    """Convert ArcGIS polygon rings to a GeoJSON Polygon."""
    return {"type": "Polygon", "coordinates": rings}


async def _fetch_catchment_layer(
    client: httpx.AsyncClient,
    base_url: str,
    layer: int,
    bbox: BBox4,
    level: str,
    warnings: list,
) -> List[dict]:
    """Fetch one FeatureServer layer (0=watch, 1=warning) within bbox."""
    geometry = f"{bbox.minLng},{bbox.minLat},{bbox.maxLng},{bbox.maxLat}"
    params = {
        "f": "json",
        "where": "1=1",
        "geometry": geometry,
        "geometryType": "esriGeometryEnvelope",
        "inSR": "4326",
        "outFields": "aac,dist_name,office",
        "outSR": "4326",
        "returnGeometry": "true",
    }
    url = f"{base_url}/{layer}/query"
    try:
        resp = await client.get(url, params=params, timeout=20.0)
        resp.raise_for_status()
        data = resp.json()
        features = data.get("features") or []
        results: List[dict] = []
        for feat in features:
            attrs = feat.get("attributes") or {}
            geom = feat.get("geometry") or {}
            rings = geom.get("rings")
            if not rings:
                continue
            results.append({
                "aac": str(attrs.get("aac") or ""),
                "dist_name": str(attrs.get("dist_name") or ""),
                "level": level,
                "geometry": _arcgis_rings_to_geojson(rings),
            })
        return results
    except Exception as e:
        warnings.append(f"flood:catchments:layer{layer}: {e}")
        return []


async def _fetch_bom_flood_catchments(
    client: httpx.AsyncClient,
    bbox: BBox4,
    warnings: list,
) -> List[FloodCatchment]:
    """Fetch Watch (layer 0) and Warning (layer 1) catchments concurrently."""
    base_url = settings.bom_flood_catchments_url
    watch_task = _fetch_catchment_layer(client, base_url, 0, bbox, "watch", warnings)
    warning_task = _fetch_catchment_layer(client, base_url, 1, bbox, "warning", warnings)
    watch_rows, warning_rows = await asyncio.gather(watch_task, warning_task)
    catchments: List[FloodCatchment] = []
    for row in watch_rows + warning_rows:
        try:
            catchments.append(FloodCatchment(**row))
        except Exception as e:
            warnings.append(f"flood:catchment parse error: {e}")
    return catchments


# ══════════════════════════════════════════════════════════════
# KiWIS timeseries helpers
# ══════════════════════════════════════════════════════════════

async def _get_ts_id(
    client: httpx.AsyncClient,
    station_no: str,
    warnings: list,
) -> Optional[str]:
    """Fetch the ts_id for Water Course Level at this station."""
    params = {
        "service": "kisters",
        "type": "queryServices",
        "request": "getTimeseriesList",
        "datasource": "0",
        "format": "json",
        "ts_name": "Water Course Level",
        "returnfields": "station_name,station_no,ts_id,coverage",
        "station_no": station_no,
    }
    try:
        resp = await client.get(settings.bom_kiwis_base_url, params=params, timeout=15.0)
        resp.raise_for_status()
        data = resp.json()
        # KiWIS returns [[header_row], [data_row], ...]
        if not isinstance(data, list) or len(data) < 2:
            return None
        # First row is the column headers
        headers = data[0]
        ts_id_idx = next((i for i, h in enumerate(headers) if str(h).lower() == "ts_id"), None)
        if ts_id_idx is None:
            return None
        for row in data[1:]:
            if isinstance(row, list) and len(row) > ts_id_idx:
                val = row[ts_id_idx]
                if val:
                    return str(val)
    except Exception as e:
        warnings.append(f"flood:ts_id:{station_no}: {e}")
    return None


async def _get_ts_values(
    client: httpx.AsyncClient,
    ts_id: str,
    hours_back: float,
    warnings: list,
) -> List[Tuple[str, float]]:
    """
    Fetch timeseries values for the last `hours_back` hours.
    Returns list of (timestamp_iso, value_m) sorted oldest-first.
    """
    from_iso = _utc_ago_iso(hours_back)
    params = {
        "service": "kisters",
        "type": "queryServices",
        "request": "getTimeseriesValues",
        "datasource": "0",
        "format": "json",
        "ts_id": ts_id,
        "from": from_iso,
        "returnfields": "Timestamp,Value",
    }
    try:
        resp = await client.get(settings.bom_kiwis_base_url, params=params, timeout=20.0)
        resp.raise_for_status()
        data = resp.json()
        # KiWIS wraps in [{ts_id:..., data:[[ts, val], ...]}, ...]
        if not isinstance(data, list) or not data:
            return []
        entry = data[0]
        if not isinstance(entry, dict):
            return []
        rows = entry.get("data") or entry.get("values") or []
        result: List[Tuple[str, float]] = []
        for row in rows:
            if isinstance(row, (list, tuple)) and len(row) >= 2:
                ts_str = str(row[0])
                val = _parse_float(row[1])
                if val is not None:
                    result.append((ts_str, val))
        return result
    except Exception as e:
        warnings.append(f"flood:ts_values:{ts_id}: {e}")
    return []


# ══════════════════════════════════════════════════════════════
# Trend + severity
# ══════════════════════════════════════════════════════════════

def _compute_trend(
    readings: List[Tuple[str, float]],
) -> str:
    """
    Compare the last reading to the reading ~3-6h ago to determine trend.
    Returns "rising" | "falling" | "steady" | "unknown"
    """
    if len(readings) < 2:
        return "unknown"
    latest = readings[-1][1]
    # Find oldest reading that is at least 3h back
    three_h_ago = (datetime.now(timezone.utc) - timedelta(hours=3)).timestamp()
    older_val: Optional[float] = None
    for ts_str, val in readings:
        try:
            t = ts_str.strip()
            if t.endswith("Z"):
                t = t[:-1] + "+00:00"
            dt = datetime.fromisoformat(t)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            if dt.timestamp() <= three_h_ago:
                older_val = val
        except Exception:
            continue
    if older_val is None:
        older_val = readings[0][1]
    if older_val == 0:
        return "rising" if latest > 0 else "unknown"
    ratio = latest / older_val
    if ratio > (1 + _STEADY_THRESHOLD):
        return "rising"
    if ratio < (1 - _STEADY_THRESHOLD):
        return "falling"
    return "steady"


def _compute_severity(
    readings: List[Tuple[str, float]],
) -> str:
    """
    Relative severity: compare latest height to the reading 24h ago.

    NOTE: These are estimates. The app includes a warning directing
    users to check BOM directly for official flood classifications.
    """
    if not readings:
        return "unknown"
    latest = readings[-1][1]
    baseline = readings[0][1]   # Oldest reading (~24h ago)
    if baseline <= 0:
        return "unknown"
    ratio = latest / baseline
    if ratio >= 2.0:
        return "major"
    if ratio >= 1.5:
        return "moderate"
    if ratio >= 1.2:
        return "minor"
    return "normal"


# ══════════════════════════════════════════════════════════════
# Per-station fetch
# ══════════════════════════════════════════════════════════════

async def _fetch_gauge(
    client: httpx.AsyncClient,
    sem: asyncio.Semaphore,
    station: dict,
    warnings: list,
) -> Optional[FloodGauge]:
    """
    Fetch live height for one station.
    Returns a FloodGauge or None if data unavailable.
    """
    async with sem:
        station_no = station["station_no"]
        ts_id = await _get_ts_id(client, station_no, warnings)
        if not ts_id:
            return FloodGauge(
                station_no=station_no,
                station_name=station["station_name"],
                lat=station["lat"],
                lng=station["lng"],
                data_owner=station["data_owner"],
            )

        readings = await _get_ts_values(client, ts_id, hours_back=24.0, warnings=warnings)
        await asyncio.sleep(_KIWIS_THROTTLE_S)

    if not readings:
        return FloodGauge(
            station_no=station_no,
            station_name=station["station_name"],
            lat=station["lat"],
            lng=station["lng"],
            data_owner=station["data_owner"],
        )

    latest_ts, latest_val = readings[-1]
    trend = _compute_trend(readings)
    severity = _compute_severity(readings)

    return FloodGauge(
        station_no=station_no,
        station_name=station["station_name"],
        lat=station["lat"],
        lng=station["lng"],
        data_owner=station["data_owner"],
        latest_height_m=round(latest_val, 3),
        reading_time_iso=latest_ts,
        trend=trend,
        severity=severity,
    )


# ══════════════════════════════════════════════════════════════
# QLD Flood Cameras
# ══════════════════════════════════════════════════════════════

async def _fetch_qld_flood_cameras(
    client: httpx.AsyncClient,
    bbox: BBox4,
    route_coords: List[Tuple[float, float]],
    warnings: List[str],
) -> List[FloodCamera]:
    """
    Fetch QLD flood camera locations + image URLs from the QLD Traffic
    GeoJSON feed. No auth required. CC-BY 4.0.

    Route coords (lat, lng) are used to compute distance_from_route_km.
    Samples every 10th point for speed.
    """
    try:
        resp = await client.get(_QLD_FLOOD_CAMERAS_URL, timeout=15.0)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        warnings.append(f"flood:qld_flood_cameras fetch failed: {e}")
        return []

    # Sample route for distance calc - every 10th point
    samples = route_coords[::10] if route_coords else []

    cameras: List[FloodCamera] = []
    for feat in (data.get("features") or []):
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

        if not (bbox.minLat <= lat <= bbox.maxLat and bbox.minLng <= lng <= bbox.maxLng):
            continue

        dist_km: Optional[float] = None
        if samples:
            dist_km = min(
                haversine_km((lat, lng), (rlat, rlng))
                for rlat, rlng in samples
            )

        cam_id_raw = str(props.get("camera_id") or props.get("id") or props.get("OBJECTID") or "")
        if not cam_id_raw:
            cam_id_raw = f"{lat:.5f},{lng:.5f}"
        cam_id = base64.urlsafe_b64encode(
            hashlib.sha256(f"qld_fc:{cam_id_raw}".encode()).digest()
        ).decode()[:16]

        name = str(props.get("camera_name") or props.get("name") or props.get("road_name") or "").strip() or None
        road = str(props.get("road_name") or props.get("road") or "").strip() or None
        image_url = str(props.get("image_url") or props.get("url") or props.get("snapshot_url") or "").strip() or None

        cameras.append(FloodCamera(
            id=cam_id,
            source="qld_flood_cameras",
            name=name,
            lat=lat,
            lng=lng,
            image_url=image_url,
            road=road,
            distance_from_route_km=round(dist_km, 2) if dist_km is not None else None,
        ))

    return cameras


# ══════════════════════════════════════════════════════════════
# Main service
# ══════════════════════════════════════════════════════════════

class Flood:
    def __init__(self, *, conn) -> None:
        self.conn = conn

    async def poll(self, *, bbox: BBox4, polyline: Optional[str] = None) -> FloodOverlay:
        algo_version = settings.flood_algo_version
        max_age = settings.flood_cache_seconds

        flood_key = stable_key(
            "flood",
            {"bbox": bbox.model_dump(), "algo_version": algo_version},
        )

        # SQLite cache hit
        cached = get_flood_pack(self.conn, flood_key)
        if cached:
            try:
                pack = FloodOverlay.model_validate(cached)
                if is_fresh(pack.created_at, max_age_s=max_age):
                    return pack
            except Exception:
                pass

        warnings: list[str] = []

        if not settings.flood_enabled:
            pack = FloodOverlay(
                flood_key=flood_key,
                bbox=bbox,
                algo_version=algo_version,
                created_at=utc_now_iso(),
                warnings=["Flood overlay is disabled (FLOOD_ENABLED=false)."],
            )
            put_flood_pack(
                self.conn,
                flood_key=flood_key,
                created_at=pack.created_at,
                algo_version=algo_version,
                pack=pack.model_dump(),
            )
            return pack

        # ── Load / refresh station list ──────────────────────────────
        stations = await self._get_stations(warnings)
        if not stations:
            pack = FloodOverlay(
                flood_key=flood_key,
                bbox=bbox,
                algo_version=algo_version,
                created_at=utc_now_iso(),
                warnings=["Could not load BOM station list."],
            )
            put_flood_pack(
                self.conn,
                flood_key=flood_key,
                created_at=pack.created_at,
                algo_version=algo_version,
                pack=pack.model_dump(),
            )
            return pack

        # ── Spatial filter ───────────────────────────────────────────
        bbox_stations = _stations_in_bbox(stations, bbox)
        if not bbox_stations:
            pack = FloodOverlay(
                flood_key=flood_key,
                bbox=bbox,
                algo_version=algo_version,
                created_at=utc_now_iso(),
                gauges=[],
                warnings=["No BOM gauge stations found within bbox."],
            )
            put_flood_pack(
                self.conn,
                flood_key=flood_key,
                created_at=pack.created_at,
                algo_version=algo_version,
                pack=pack.model_dump(),
            )
            return pack

        # Cap at max stations
        selected = bbox_stations[:_MAX_STATIONS_PER_REQUEST]
        if len(bbox_stations) > _MAX_STATIONS_PER_REQUEST:
            warnings.append(
                f"{len(bbox_stations)} stations in bbox; limited to {_MAX_STATIONS_PER_REQUEST}."
            )

        # ── Decode route polyline once (used for catchment check + camera distances) ──
        route_coords: List[Tuple[float, float]] = decode_polyline6(polyline) if polyline else []

        # ── Fetch live readings + catchments + QLD flood cameras concurrently ──
        sem = asyncio.Semaphore(_MAX_CONCURRENT_KIWIS)
        transport = httpx.AsyncHTTPTransport(retries=1)
        async with httpx.AsyncClient(
            follow_redirects=True,
            transport=transport,
            timeout=httpx.Timeout(20.0),
        ) as client:
            gauge_tasks = [
                _fetch_gauge(client, sem, s, warnings)
                for s in selected
            ]
            catchment_task = _fetch_bom_flood_catchments(client, bbox, warnings)
            # QLD flood cameras: only fetch when bbox overlaps QLD
            _QLD_LAT_MIN, _QLD_LAT_MAX = -29.2, -10.0
            _QLD_LNG_MIN, _QLD_LNG_MAX = 137.9, 153.6
            fetch_qld_cameras = (
                bbox.maxLat >= _QLD_LAT_MIN and bbox.minLat <= _QLD_LAT_MAX
                and bbox.maxLng >= _QLD_LNG_MIN and bbox.minLng <= _QLD_LNG_MAX
            )
            async def _empty_cameras() -> List[FloodCamera]:
                return []

            qld_cameras_task = (
                _fetch_qld_flood_cameras(client, bbox, route_coords, warnings)
                if fetch_qld_cameras
                else _empty_cameras()
            )
            *gauge_results, catchments, qld_flood_cameras = await asyncio.gather(
                *gauge_tasks, catchment_task, qld_cameras_task, return_exceptions=True
            )

        gauges: list[FloodGauge] = []
        for r in gauge_results:
            if isinstance(r, FloodGauge):
                gauges.append(r)
            elif isinstance(r, Exception):
                warnings.append(f"flood:gauge fetch error: {r}")

        if isinstance(catchments, Exception):
            warnings.append(f"flood:catchments fetch error: {catchments}")
            catchments = []

        if isinstance(qld_flood_cameras, Exception):
            warnings.append(f"flood:qld_flood_cameras error: {qld_flood_cameras}")
            qld_flood_cameras = []

        # ── Route-passes-through-warning check ───────────────────────
        route_passes_through_warning = False
        if route_coords and catchments:
            warning_catchments = [c for c in catchments if c.level == "warning"]
            if warning_catchments:
                route_passes_through_warning = _route_intersects_warning_catchments(
                    route_coords, warning_catchments
                )

        # ── Attributions ─────────────────────────────────────────────
        attributions = sorted({g.data_owner for g in gauges if g.data_owner})

        # Always include the severity-estimate disclaimer
        warnings.append(
            "Flood severity ratings are relative estimates only - "
            "check www.bom.gov.au/water for official flood classifications."
        )

        pack = FloodOverlay(
            flood_key=flood_key,
            bbox=bbox,
            algo_version=algo_version,
            created_at=utc_now_iso(),
            gauges=gauges,
            catchments=catchments,
            flood_cameras=list(qld_flood_cameras),
            attributions=attributions,
            warnings=warnings,
            route_passes_through_warning=route_passes_through_warning,
        )

        put_flood_pack(
            self.conn,
            flood_key=flood_key,
            created_at=pack.created_at,
            algo_version=algo_version,
            pack=pack.model_dump(),
        )
        return pack

    async def _get_stations(self, warnings: list) -> list:
        """
        Return the cached station list, refreshing from BOM if stale or missing.
        """
        refresh_secs = settings.flood_station_refresh_hours * 3600
        fetched_at, cached_stations = get_flood_stations(self.conn)

        if cached_stations and fetched_at and is_fresh(fetched_at, max_age_s=refresh_secs):
            return cached_stations

        # Fetch from BOM
        try:
            transport = httpx.AsyncHTTPTransport(retries=1)
            async with httpx.AsyncClient(
                follow_redirects=True,
                transport=transport,
                timeout=httpx.Timeout(90.0),
                headers={"User-Agent": "Mozilla/5.0 (compatible; roam-backend/1.0)"},
            ) as client:
                stations = await _fetch_station_list(client)
            if stations:
                put_flood_stations(self.conn, fetched_at=utc_now_iso(), stations=stations)
                return stations
            warnings.append("BOM station list returned empty - using cached copy if available.")
        except Exception as e:
            warnings.append(f"flood:station_list fetch failed: {e}")

        # Fall back to stale cache
        return cached_stations or []
