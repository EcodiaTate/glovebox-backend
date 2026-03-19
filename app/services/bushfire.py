# app/services/bushfire.py
"""
Bushfire overlay service for Roam.

Data sources:
  1. NSW RFS Major Incidents GeoJSON (free, no auth)
     URL: https://www.rfs.nsw.gov.au/feeds/majorIncidents.json
     Returns a GeoJSON FeatureCollection with Point and Polygon geometries.
     Fields: title, category, guid, pubDate, description (HTML with alert
     level, location, status, fire type, size, responsible agency, council area).

  2. NASA FIRMS (free, API key required)
     URL: https://firms.modaps.eosdis.nasa.gov/api/country/csv/{MAP_KEY}/VIIRS_SNPP_NRT/AUS/{days}
     Returns CSV with columns: country_id, latitude, longitude, bright_ti4,
     scan, track, acq_date, acq_time, satellite, instrument, confidence,
     version, bright_ti5, frp, daynight, type.

Algorithm:
  1. Decode polyline6 to get route bounding box with buffer.
  2. Fetch NSW RFS GeoJSON + NASA FIRMS CSV concurrently.
  3. From NSW RFS: parse each feature, extract alert level from description
     HTML using regex.
  4. From FIRMS: parse CSV, filter hotspots within bbox, group nearby
     hotspots into clusters.
  5. Merge both sources - NSW RFS fires with boundaries take priority.
  6. Compute distance from route for each fire/hotspot.

Cache TTL: 15 minutes (900s) - fires are time-critical.
"""
from __future__ import annotations

import asyncio
import csv
import hashlib
import io
import logging
import re
from typing import Dict, List, Optional, Tuple

import httpx
from app.core.contracts import BushfireIncident, FirmsHotspot, BushfireOverlay
from app.core.settings import settings
from app.core.storage import get_bushfire_pack, put_bushfire_pack
from app.core.time import utc_now_iso
from app.core.geo import bbox_from_coords, decode_polyline6, haversine_km, min_dist_to_route, sample_route
from app.core.http_client import http_client
from app.core.cache_utils import is_fresh, stable_key

logger = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════════
# Constants
# ══════════════════════════════════════════════════════════════

_FIRMS_DAYS = 2                 # Fetch last 2 days of FIRMS data
_CLUSTER_RADIUS_KM = 5.0        # Group FIRMS hotspots within 5 km
# Alert level severity ordering (higher = more severe)
_ALERT_SEVERITY: Dict[str, int] = {
    "Not Applicable": 0,
    "Advice": 1,
    "Watch and Act": 2,
    "Emergency Warning": 3,
}


# ══════════════════════════════════════════════════════════════
# Geo helpers
# ══════════════════════════════════════════════════════════════


# ══════════════════════════════════════════════════════════════
# NSW RFS parsing
# ══════════════════════════════════════════════════════════════

_RE_ALERT_LEVEL = re.compile(r"ALERT LEVEL:\s*(.+?)[\n<]", re.IGNORECASE)
_RE_STATUS = re.compile(r"STATUS:\s*(.+?)[\n<]", re.IGNORECASE)
_RE_TYPE = re.compile(r"TYPE:\s*(.+?)[\n<]", re.IGNORECASE)
_RE_FIRE = re.compile(r"FIRE:\s*(.+?)[\n<]", re.IGNORECASE)
_RE_SIZE = re.compile(r"SIZE:\s*(.+?)\s*ha", re.IGNORECASE)
_RE_COUNCIL = re.compile(r"COUNCIL AREA:\s*(.+?)[\n<]", re.IGNORECASE)
_RE_AGENCY = re.compile(r"RESPONSIBLE AGENCY:\s*(.+?)[\n<]", re.IGNORECASE)


def _extract_field(pattern: re.Pattern, text: str) -> Optional[str]:
    m = pattern.search(text)
    return m.group(1).strip() if m else None


def _parse_rfs_feature(
    feature: dict,
    route_coords: List[Tuple[float, float]],
) -> Optional[BushfireIncident]:
    """Parse a single NSW RFS GeoJSON feature into a BushfireIncident."""
    props = feature.get("properties") or {}
    geom = feature.get("geometry") or {}
    geo_type = geom.get("type", "")
    geo_coords = geom.get("coordinates")

    # Determine centroid lat/lng
    lat: Optional[float] = None
    lng: Optional[float] = None

    if geo_type == "Point" and geo_coords and len(geo_coords) >= 2:
        lng, lat = float(geo_coords[0]), float(geo_coords[1])
    elif geo_type == "Polygon" and geo_coords:
        # Use the centroid of the first ring
        ring = geo_coords[0] if geo_coords else []
        if ring:
            lats = [c[1] for c in ring if len(c) >= 2]
            lngs = [c[0] for c in ring if len(c) >= 2]
            if lats and lngs:
                lat = sum(lats) / len(lats)
                lng = sum(lngs) / len(lngs)
    elif geo_type == "MultiPolygon" and geo_coords:
        all_lats: List[float] = []
        all_lngs: List[float] = []
        for poly in geo_coords:
            if poly:
                ring = poly[0]
                for c in ring:
                    if len(c) >= 2:
                        all_lats.append(c[1])
                        all_lngs.append(c[0])
        if all_lats and all_lngs:
            lat = sum(all_lats) / len(all_lats)
            lng = sum(all_lngs) / len(all_lngs)
    elif geo_type == "GeometryCollection":
        # Use the first geometry's coordinates
        geometries = geom.get("geometries") or []
        for sub_geom in geometries:
            sub_type = sub_geom.get("type", "")
            sub_coords = sub_geom.get("coordinates")
            if sub_type == "Point" and sub_coords and len(sub_coords) >= 2:
                lng, lat = float(sub_coords[0]), float(sub_coords[1])
                break
            elif sub_type == "Polygon" and sub_coords:
                ring = sub_coords[0] if sub_coords else []
                if ring:
                    lats = [c[1] for c in ring if len(c) >= 2]
                    lngs = [c[0] for c in ring if len(c) >= 2]
                    if lats and lngs:
                        lat = sum(lats) / len(lats)
                        lng = sum(lngs) / len(lngs)
                        break

    if lat is None or lng is None:
        return None

    # Parse description HTML for fire details
    desc = props.get("description") or ""
    alert_level = _extract_field(_RE_ALERT_LEVEL, desc)
    status = _extract_field(_RE_STATUS, desc)
    fire_type = _extract_field(_RE_TYPE, desc) or _extract_field(_RE_FIRE, desc)
    council_area = _extract_field(_RE_COUNCIL, desc)
    responsible_agency = _extract_field(_RE_AGENCY, desc)

    size_ha: Optional[float] = None
    size_str = _extract_field(_RE_SIZE, desc)
    if size_str:
        try:
            size_ha = float(size_str.replace(",", ""))
        except (TypeError, ValueError):
            pass

    # Compute distance from route
    distance_km = min_dist_to_route(lat, lng, route_coords)

    # Build a stable id from guid or title
    guid = props.get("guid") or props.get("id") or ""
    fire_id = hashlib.sha256(
        (guid or props.get("title", "")).encode()
    ).hexdigest()[:16]

    # Preserve polygon geometry if available
    boundary_geom: Optional[dict] = None
    if geo_type in ("Polygon", "MultiPolygon"):
        boundary_geom = geom
    elif geo_type == "GeometryCollection":
        # Extract polygon geometries from collection
        geometries = geom.get("geometries") or []
        for sub_geom in geometries:
            if sub_geom.get("type") in ("Polygon", "MultiPolygon"):
                boundary_geom = sub_geom
                break

    return BushfireIncident(
        id=fire_id,
        source="nsw_rfs",
        title=props.get("title") or "Unknown Fire",
        alert_level=alert_level,
        status=status,
        fire_type=fire_type,
        size_ha=size_ha,
        lat=round(lat, 6),
        lng=round(lng, 6),
        geometry=boundary_geom,
        distance_from_route_km=round(distance_km, 1),
        pub_date=props.get("pubDate"),
        council_area=council_area,
        responsible_agency=responsible_agency,
    )


# ══════════════════════════════════════════════════════════════
# FIRMS CSV parsing
# ══════════════════════════════════════════════════════════════

def _parse_firms_csv(
    csv_text: str,
    bbox: Tuple[float, float, float, float],
    route_coords: List[Tuple[float, float]],
) -> Tuple[List[FirmsHotspot], List[str]]:
    """
    Parse NASA FIRMS CSV, filter to bbox, compute distances.
    Returns (hotspots, warnings).
    """
    min_lat, min_lng, max_lat, max_lng = bbox
    hotspots: List[FirmsHotspot] = []
    warnings: List[str] = []

    try:
        reader = csv.DictReader(io.StringIO(csv_text))
        for row in reader:
            try:
                lat = float(row.get("latitude", ""))
                lng = float(row.get("longitude", ""))
            except (TypeError, ValueError):
                continue

            # Filter to bbox
            if lat < min_lat or lat > max_lat or lng < min_lng or lng > max_lng:
                continue

            brightness: Optional[float] = None
            try:
                brightness = float(row.get("bright_ti4", ""))
            except (TypeError, ValueError):
                pass

            frp: Optional[float] = None
            try:
                frp = float(row.get("frp", ""))
            except (TypeError, ValueError):
                pass

            confidence = row.get("confidence")
            if confidence:
                confidence = confidence.strip().lower()

            distance_km = min_dist_to_route(lat, lng, route_coords)

            hotspots.append(FirmsHotspot(
                lat=round(lat, 6),
                lng=round(lng, 6),
                brightness=brightness,
                confidence=confidence,
                acq_date=row.get("acq_date"),
                acq_time=row.get("acq_time"),
                frp=frp,
                distance_from_route_km=round(distance_km, 1),
            ))
    except Exception as e:
        warnings.append(f"bushfire:firms:csv_parse: {e}")

    return hotspots, warnings


def _cluster_hotspots(
    hotspots: List[FirmsHotspot],
    radius_km: float = _CLUSTER_RADIUS_KM,
) -> List[FirmsHotspot]:
    """
    Group nearby hotspots into clusters, keeping the hotspot with the
    highest FRP as the representative. This reduces noise from individual
    VIIRS pixels.
    """
    if not hotspots:
        return []

    # Sort by FRP descending so the brightest hotspot seeds each cluster
    sorted_hs = sorted(
        hotspots,
        key=lambda h: h.frp if h.frp is not None else 0.0,
        reverse=True,
    )

    clustered: List[FirmsHotspot] = []
    used: List[bool] = [False] * len(sorted_hs)

    for i, seed in enumerate(sorted_hs):
        if used[i]:
            continue
        used[i] = True
        # Mark all hotspots within radius as used (absorbed into this cluster)
        for j in range(i + 1, len(sorted_hs)):
            if used[j]:
                continue
            dist = haversine_km((seed.lat, seed.lng), (sorted_hs[j].lat, sorted_hs[j].lng))
            if dist <= radius_km:
                used[j] = True
        clustered.append(seed)

    return clustered


# ══════════════════════════════════════════════════════════════
# Data fetching
# ══════════════════════════════════════════════════════════════

async def _fetch_rfs(
    client: httpx.AsyncClient,
    warnings: List[str],
) -> Optional[dict]:
    """Fetch NSW RFS Major Incidents GeoJSON."""
    try:
        resp = await client.get(settings.nsw_rfs_url, timeout=15.0)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        warnings.append(f"bushfire:nsw_rfs: {e}")
        return None


async def _fetch_firms(
    client: httpx.AsyncClient,
    warnings: List[str],
) -> Optional[str]:
    """Fetch NASA FIRMS CSV for Australia."""
    api_key = settings.firms_map_key
    if not api_key:
        warnings.append("bushfire:firms: FIRMS_MAP_KEY not configured - skipping satellite hotspot data.")
        return None

    url = (
        f"https://firms.modaps.eosdis.nasa.gov/api/country/csv/"
        f"{api_key}/VIIRS_SNPP_NRT/AUS/{_FIRMS_DAYS}"
    )
    try:
        resp = await client.get(url, timeout=30.0)
        resp.raise_for_status()
        return resp.text
    except Exception as e:
        warnings.append(f"bushfire:firms: {e}")
        return None


# ══════════════════════════════════════════════════════════════
# Alert level helpers
# ══════════════════════════════════════════════════════════════

def _max_alert(incidents: List[BushfireIncident]) -> Optional[str]:
    """Return the highest alert level across all incidents."""
    best: Optional[str] = None
    best_score = -1
    for inc in incidents:
        if inc.alert_level:
            score = _ALERT_SEVERITY.get(inc.alert_level, 0)
            if score > best_score:
                best_score = score
                best = inc.alert_level
    return best


# ══════════════════════════════════════════════════════════════
# Main service
# ══════════════════════════════════════════════════════════════

class Bushfire:
    """
    Bushfire overlay service.

    Combines NSW RFS major incidents and NASA FIRMS satellite hotspots
    to provide fire proximity awareness along a driving route.
    Results are cached in SQLite for 15 minutes.
    """

    def __init__(self, *, conn) -> None:
        self.conn = conn

    async def along_route(
        self,
        *,
        polyline6: str,
        buffer_km: float = 50.0,
        cache_seconds: Optional[int] = None,
    ) -> BushfireOverlay:
        """
        Build a bushfire overlay along a route.

        Args:
            polyline6:      Polyline6-encoded route geometry.
            buffer_km:      Buffer distance in km around the route bbox (default 50).
            cache_seconds:  Override cache TTL (default 900s / 15 min).

        Returns:
            BushfireOverlay with incidents, hotspots, and warnings.
        """
        algo_version = settings.bushfire_algo_version
        max_age = cache_seconds if cache_seconds is not None else settings.bushfire_cache_seconds

        # Cache key
        bushfire_key = stable_key("bushfire", {
            "polyline6": polyline6,
            "buffer_km": round(buffer_km, 1),
            "algo_version": algo_version,
        })

        # Check cache
        cached = get_bushfire_pack(self.conn, bushfire_key)
        if cached:
            created_at = cached.get("created_at", "")
            if is_fresh(created_at, max_age_s=max_age):
                try:
                    return BushfireOverlay.model_validate(cached)
                except Exception:
                    pass

        # Decode geometry
        coords = decode_polyline6(polyline6)
        if not coords:
            overlay = BushfireOverlay(
                bushfire_key=bushfire_key,
                polyline6=polyline6,
                algo_version=algo_version,
                created_at=utc_now_iso(),
                warnings=["Empty route geometry."],
            )
            put_bushfire_pack(
                self.conn,
                bushfire_key=bushfire_key,
                created_at=overlay.created_at,
                algo_version=algo_version,
                pack=overlay.model_dump(),
            )
            return overlay

        # Compute bbox with buffer
        bbox = bbox_from_coords(coords, buffer_km)
        route_sampled = sample_route(coords)

        warnings: List[str] = []

        # ── Fetch NSW RFS + FIRMS concurrently ──────────────────
        async with http_client(timeout=30.0) as client:
            rfs_task = _fetch_rfs(client, warnings)
            firms_task = _fetch_firms(client, warnings)
            rfs_data, firms_csv = await asyncio.gather(
                rfs_task, firms_task, return_exceptions=True,
            )

        # Handle exceptions from gather
        if isinstance(rfs_data, Exception):
            warnings.append(f"bushfire:nsw_rfs: {rfs_data}")
            rfs_data = None
        if isinstance(firms_csv, Exception):
            warnings.append(f"bushfire:firms: {firms_csv}")
            firms_csv = None

        # ── Parse NSW RFS incidents ─────────────────────────────
        incidents: List[BushfireIncident] = []
        if rfs_data and isinstance(rfs_data, dict):
            features = rfs_data.get("features") or []
            for feat in features:
                try:
                    inc = _parse_rfs_feature(feat, route_sampled)
                    if inc is not None:
                        # Filter to bbox
                        min_lat, min_lng, max_lat, max_lng = bbox
                        if (min_lat <= inc.lat <= max_lat
                                and min_lng <= inc.lng <= max_lng):
                            incidents.append(inc)
                except Exception as e:
                    warnings.append(f"bushfire:nsw_rfs:parse: {e}")

        # ── Parse FIRMS hotspots ────────────────────────────────
        hotspots: List[FirmsHotspot] = []
        if firms_csv and isinstance(firms_csv, str):
            parsed, parse_warnings = _parse_firms_csv(firms_csv, bbox, route_sampled)
            warnings.extend(parse_warnings)
            # Cluster nearby hotspots
            hotspots = _cluster_hotspots(parsed)

        # ── Count fires near route ──────────────────────────────
        fires_near = sum(
            1 for inc in incidents
            if inc.distance_from_route_km is not None
            and inc.distance_from_route_km <= buffer_km
        )
        hotspots_near = sum(
            1 for hs in hotspots
            if hs.distance_from_route_km is not None
            and hs.distance_from_route_km <= buffer_km
        )

        max_alert = _max_alert(incidents)

        # ── Sort by distance from route (closest first) ─────────
        incidents.sort(
            key=lambda i: i.distance_from_route_km if i.distance_from_route_km is not None else float("inf"),
        )
        hotspots.sort(
            key=lambda h: h.distance_from_route_km if h.distance_from_route_km is not None else float("inf"),
        )

        created_at = utc_now_iso()
        overlay = BushfireOverlay(
            bushfire_key=bushfire_key,
            polyline6=polyline6,
            algo_version=algo_version,
            created_at=created_at,
            incidents=incidents,
            hotspots=hotspots,
            fires_near_route=fires_near + hotspots_near,
            max_alert_level=max_alert,
            warnings=warnings,
        )

        # Persist to cache
        put_bushfire_pack(
            self.conn,
            bushfire_key=bushfire_key,
            created_at=created_at,
            algo_version=algo_version,
            pack=overlay.model_dump(),
        )

        return overlay
