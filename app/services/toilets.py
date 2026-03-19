# app/services/toilets.py
"""
Public toilets + dump points overlay service for Roam.

Data source: National Public Toilet Map (Department of Health, CC BY 3.0 AU)
  ArcGIS FeatureServer:
    https://portal.data.nsw.gov.au/arcgis/rest/services/Hosted/National_Public_Toilet_Map/FeatureServer/0/query
  Coverage: National (17,000+ facilities)

Fields used:
  - FacilityID, Name, Address1, Town, State, Postcode
  - Latitude, Longitude
  - ToiletType (Flush, Chemical, etc.)
  - Accessible (Y/N)
  - BabyChange, DrinkingWater, Shower, DumpPoint, KeyRequired, Fee
  - OpeningHours (free text), IsParkingAvailable
  - attribution required: "© Commonwealth of Australia (Department of Health)"

Algorithm:
  1. Decode polyline6 to extract bounding box + buffer.
  2. Query the ArcGIS FeatureServer with esriGeometryEnvelope.
  3. Parse each facility, flag dump_point and shower as bonus fields.
  4. Compute haversine distance from nearest route sample.
  5. Return toilets sorted by distance, capped at _MAX_TOILETS.
  6. Cache for 7 days (toilet locations are stable).

Dump points (caravan/RV waste stations) are annotated separately so
the frontend can filter them independently.
"""
from __future__ import annotations

import base64
import hashlib
import logging
from typing import Any, List, Optional, Tuple

import httpx

from app.core.cache_utils import stable_key, is_fresh
from app.core.geo import decode_polyline6, sample_route, bbox_from_coords, min_dist_to_route
from app.core.storage import put_toilets_pack, get_toilets_pack
from app.core.time import utc_now_iso
from app.core.contracts import PublicToilet, ToiletsOverlay

logger = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════════
# Constants
# ══════════════════════════════════════════════════════════════

_CACHE_TTL_S = 7 * 86_400  # 7 days
_ALGO_VERSION = "toilets-1.0"
_MAX_TOILETS = 200
_HTTP_TIMEOUT = 25.0

_TOILETS_URL = (
    "https://portal.data.nsw.gov.au/arcgis/rest/services"
    "/Hosted/National_Public_Toilet_Map/FeatureServer/0/query"
)

_ATTRIBUTION = "© Commonwealth of Australia (Department of Health)"


# ══════════════════════════════════════════════════════════════
# Parser helpers
# ══════════════════════════════════════════════════════════════

def _truthy(v: Any) -> bool:
    """ArcGIS returns 1/0, "Y"/"N", True/False - normalise to bool."""
    if v is None:
        return False
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    return str(v).strip().lower() in ("y", "yes", "true", "1")


def _parse_float(v: Any) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# ══════════════════════════════════════════════════════════════
# Data fetcher
# ══════════════════════════════════════════════════════════════

async def _fetch_toilets(
    client: httpx.AsyncClient,
    min_lat: float,
    min_lng: float,
    max_lat: float,
    max_lng: float,
    route_samples: List[Tuple[float, float]],
    buffer_km: float,
    warnings: List[str],
) -> List[PublicToilet]:
    geometry = f"{min_lng},{min_lat},{max_lng},{max_lat}"
    params = {
        "where": "1=1",
        "geometry": geometry,
        "geometryType": "esriGeometryEnvelope",
        "inSR": "4326",
        "outFields": (
            "FacilityID,Name,Address1,Town,State,Postcode,"
            "Latitude,Longitude,ToiletType,Accessible,BabyChange,"
            "DrinkingWater,Shower,DumpPoint,KeyRequired,Fee,"
            "OpeningHours,IsParkingAvailable"
        ),
        "outSR": "4326",
        "resultRecordCount": 2000,
        "f": "json",
    }

    try:
        resp = await client.get(_TOILETS_URL, params=params, timeout=_HTTP_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        warnings.append(f"toilets:fetch: {e}")
        return []

    features = data.get("features") or []
    toilets: List[PublicToilet] = []

    for feat in features:
        try:
            attrs = feat.get("attributes") or {}
            geom = feat.get("geometry") or {}

            lat = _parse_float(attrs.get("Latitude")) or _parse_float(geom.get("y"))
            lng = _parse_float(attrs.get("Longitude")) or _parse_float(geom.get("x"))
            if lat is None or lng is None:
                continue

            dist_km = min_dist_to_route(lat, lng, route_samples)
            if dist_km > buffer_km:
                continue

            fid = str(attrs.get("FacilityID") or "")
            raw_id = f"toilet::{round(lat, 5)}::{round(lng, 5)}::{fid}"
            tid = base64.urlsafe_b64encode(
                hashlib.sha256(raw_id.encode()).digest()
            ).decode().rstrip("=")[:20]

            name = str(attrs.get("Name") or "").strip() or None
            address = str(attrs.get("Address1") or "").strip() or None
            suburb = str(attrs.get("Town") or "").strip() or None
            state = str(attrs.get("State") or "").strip() or None
            toilet_type = str(attrs.get("ToiletType") or "").strip() or None
            opening_hours = str(attrs.get("OpeningHours") or "").strip() or None

            toilets.append(PublicToilet(
                id=tid,
                name=name,
                lat=lat,
                lng=lng,
                address=address,
                suburb=suburb,
                state=state,
                toilet_type=toilet_type,
                is_accessible=_truthy(attrs.get("Accessible")),
                has_baby_change=_truthy(attrs.get("BabyChange")),
                has_drinking_water=_truthy(attrs.get("DrinkingWater")),
                has_shower=_truthy(attrs.get("Shower")),
                is_dump_point=_truthy(attrs.get("DumpPoint")),
                key_required=_truthy(attrs.get("KeyRequired")),
                is_fee=_truthy(attrs.get("Fee")),
                opening_hours=opening_hours,
                has_parking=_truthy(attrs.get("IsParkingAvailable")),
                distance_from_route_km=round(dist_km, 2),
            ))
        except Exception as e:
            warnings.append(f"toilets:parse: {e}")

    logger.info("toilets: bbox query returned %d features → %d parsed", len(features), len(toilets))
    return toilets


# ══════════════════════════════════════════════════════════════
# Main service class
# ══════════════════════════════════════════════════════════════

class Toilets:
    """
    Public toilets + dump points overlay service.

    Queries the National Public Toilet Map (national, CC BY 3.0 AU)
    for facilities near a route polyline. Results cached 7 days.
    """

    def __init__(self, *, conn) -> None:
        self.conn = conn

    async def along_route(
        self,
        *,
        polyline6: str,
        buffer_km: float = 15.0,
        cache_seconds: int = _CACHE_TTL_S,
    ) -> ToiletsOverlay:
        toilets_key = stable_key("toilets", {
            "polyline6": polyline6,
            "buffer_km": round(buffer_km, 1),
            "algo_version": _ALGO_VERSION,
        })

        cached = get_toilets_pack(self.conn, toilets_key)
        if cached:
            created_at = cached.get("created_at", "")
            if is_fresh(created_at, max_age_s=cache_seconds):
                logger.debug("toilets cache hit: %s", toilets_key)
                return ToiletsOverlay.model_validate(cached)

        coords = decode_polyline6(polyline6)
        if not coords:
            overlay = ToiletsOverlay(
                toilets_key=toilets_key,
                polyline6=polyline6,
                algo_version=_ALGO_VERSION,
                created_at=utc_now_iso(),
                warnings=["Failed to decode route polyline."],
            )
            put_toilets_pack(self.conn, toilets_key=toilets_key, created_at=overlay.created_at, algo_version=_ALGO_VERSION, pack=overlay.model_dump())
            return overlay

        route_samples = sample_route(coords, interval_km=2.0)
        min_lat, min_lng, max_lat, max_lng = bbox_from_coords(coords, buffer_km)
        warnings: List[str] = []

        async with httpx.AsyncClient(follow_redirects=True, timeout=httpx.Timeout(_HTTP_TIMEOUT)) as client:
            all_toilets = await _fetch_toilets(
                client, min_lat, min_lng, max_lat, max_lng,
                route_samples, buffer_km, warnings,
            )

        # Sort by distance; split dump points into separate list
        all_toilets.sort(key=lambda t: t.distance_from_route_km or 0.0)

        dump_points = [t for t in all_toilets if t.is_dump_point]
        toilets = [t for t in all_toilets if not t.is_dump_point]

        if len(toilets) > _MAX_TOILETS:
            warnings.append(f"{len(toilets)} toilets found; limited to {_MAX_TOILETS}.")
            toilets = toilets[:_MAX_TOILETS]

        logger.info(
            "toilets: polyline=%d chars, bbox=%.3f,%.3f→%.3f,%.3f, toilets=%d, dump_points=%d",
            len(polyline6), min_lat, min_lng, max_lat, max_lng,
            len(toilets), len(dump_points),
        )

        created_at = utc_now_iso()
        overlay = ToiletsOverlay(
            toilets_key=toilets_key,
            polyline6=polyline6,
            algo_version=_ALGO_VERSION,
            created_at=created_at,
            toilets=toilets,
            dump_points=dump_points,
            warnings=warnings,
        )
        put_toilets_pack(self.conn, toilets_key=toilets_key, created_at=created_at, algo_version=_ALGO_VERSION, pack=overlay.model_dump())
        return overlay
