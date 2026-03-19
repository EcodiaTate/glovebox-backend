# app/services/emergency_services.py
"""
Emergency services overlay - Geoscience Australia Emergency Management Facilities.

Data source : GA Emergency Management Facilities MapServer (CC-BY 4.0, no auth)
Endpoint    : http://services.ga.gov.au/gis/rest/services/Emergency_Management_Facilities/MapServer
Layers:
  0 - AMBULANCE_STATION
  1 - OTHER_EMERGENCY_MANAGEMENT_FACILITY
  2 - POLICING_FACILITY
  3 - METRO_FIRE_FACILITY
  4 - RURAL_COUNTRY_FIRE_SERVICE_FACILITY
  5 - SES_FACILITY

Design:
- Route polyline6 is decoded and sampled to build a bounding box.
- Layers 0, 2, 3+4, 5 are queried concurrently via asyncio.gather.
- Each facility is distance-checked against route sample points (haversine).
- Results sorted by distance from route and capped at 50 facilities.
- Cached in SQLite for 24 hours (facilities rarely change).
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
from typing import Any, Dict, List, Optional, Tuple

import httpx

from app.core.contracts import EmergencyFacility, EmergencyServicesOverlay
from app.core.settings import settings
from app.core.storage import get_emergency_pack, put_emergency_pack
from app.core.time import utc_now_iso
from app.core.geo import bbox_from_coords, decode_polyline6, filter_by_corridor, sample_route
from app.core.http_client import http_client
from app.core.cache_utils import is_fresh, stable_key

logger = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════════
# Constants
# ══════════════════════════════════════════════════════════════

_LAYER_AMBULANCE = 0
_LAYER_OTHER = 1
_LAYER_POLICE = 2
_LAYER_METRO_FIRE = 3
_LAYER_RURAL_FIRE = 4
_LAYER_SES = 5

_OUT_FIELDS = (
    "FACILITY_NAME,CLASS,FACILITY_LAT,FACILITY_LONG,"
    "FACILITY_STATE,FEATURETYPE,GNAF_FORMATTED_ADDRESS,"
    "ABS_SUBURB,ABS_POSTCODE,FACILITY_OPERATIONALSTATUS"
)

_MAX_FACILITIES = 50

# ══════════════════════════════════════════════════════════════
# Geometry helpers
# ══════════════════════════════════════════════════════════════


# ══════════════════════════════════════════════════════════════
# Cache helpers
# ══════════════════════════════════════════════════════════════


# ══════════════════════════════════════════════════════════════
# Storage functions imported from app.core.storage:
# get_emergency_pack, put_emergency_pack


# ══════════════════════════════════════════════════════════════
# ArcGIS query helpers
# ══════════════════════════════════════════════════════════════

def _layer_to_facility_type(layer: int) -> str:
    """Map GA MapServer layer number to a user-friendly facility type."""
    return {
        _LAYER_AMBULANCE: "ambulance",
        _LAYER_OTHER: "hospital",
        _LAYER_POLICE: "police",
        _LAYER_METRO_FIRE: "fire",
        _LAYER_RURAL_FIRE: "fire",
        _LAYER_SES: "ses",
    }.get(layer, "other")


def _parse_facility(
    attrs: Dict[str, Any],
    layer: int,
) -> Optional[EmergencyFacility]:
    """Parse ArcGIS feature attributes into an EmergencyFacility."""
    try:
        lat_val = attrs.get("FACILITY_LAT")
        lng_val = attrs.get("FACILITY_LONG")
        if lat_val is None or lng_val is None:
            return None
        lat = float(lat_val)
        lng = float(lng_val)
    except (TypeError, ValueError):
        return None

    name = str(attrs.get("FACILITY_NAME") or "Unknown Facility")
    facility_type = _layer_to_facility_type(layer)

    # Refine type from CLASS or FEATURETYPE for layer 1 (OTHER)
    if layer == _LAYER_OTHER:
        cls = str(attrs.get("CLASS") or "").lower()
        ft = str(attrs.get("FEATURETYPE") or "").lower()
        if "hospital" in cls or "hospital" in ft:
            facility_type = "hospital"
        elif "ambulance" in cls or "ambulance" in ft:
            facility_type = "ambulance"
        elif "fire" in cls or "fire" in ft:
            facility_type = "fire"
        elif "police" in cls or "police" in ft:
            facility_type = "police"
        elif "ses" in cls or "ses" in ft:
            facility_type = "ses"

    # Skip non-operational facilities
    status = str(attrs.get("FACILITY_OPERATIONALSTATUS") or "").lower()
    if status and status not in ("operational", "open", ""):
        return None

    # Build a stable ID from layer + name + coordinates
    id_raw = f"ga_emf:{layer}:{name}:{round(lat, 5)}:{round(lng, 5)}"
    h = hashlib.sha256(id_raw.encode()).digest()
    stable_id = base64.urlsafe_b64encode(h).decode("ascii").rstrip("=")[:20]

    address = attrs.get("GNAF_FORMATTED_ADDRESS")
    suburb = attrs.get("ABS_SUBURB")
    postcode = attrs.get("ABS_POSTCODE")
    state = attrs.get("FACILITY_STATE")

    return EmergencyFacility(
        id=stable_id,
        name=name,
        facility_type=facility_type,
        lat=lat,
        lng=lng,
        address=str(address) if address else None,
        suburb=str(suburb) if suburb else None,
        postcode=str(postcode) if postcode else None,
        state=str(state) if state else None,
    )


async def _query_layer(
    client: httpx.AsyncClient,
    layer: int,
    bbox: Tuple[float, float, float, float],
    warnings: List[str],
) -> List[EmergencyFacility]:
    """
    Query a single GA MapServer layer within the given bbox.

    bbox = (min_lat, min_lng, max_lat, max_lng)
    ArcGIS expects geometry as: xmin,ymin,xmax,ymax (lng,lat order).
    """
    min_lat, min_lng, max_lat, max_lng = bbox
    geometry = f"{min_lng},{min_lat},{max_lng},{max_lat}"
    params = {
        "f": "json",
        "where": "1=1",
        "geometry": geometry,
        "geometryType": "esriGeometryEnvelope",
        "inSR": "4326",
        "outFields": _OUT_FIELDS,
        "outSR": "4326",
        "returnGeometry": "false",
        "resultRecordCount": "500",
    }
    url = f"{settings.ga_emergency_base_url}/{layer}/query"
    try:
        resp = await client.get(url, params=params, timeout=20.0)
        resp.raise_for_status()
        data = resp.json()
        features = data.get("features") or []
        results: List[EmergencyFacility] = []
        for feat in features:
            attrs = feat.get("attributes") or {}
            facility = _parse_facility(attrs, layer)
            if facility is not None:
                results.append(facility)
        return results
    except Exception as e:
        warnings.append(f"emergency:layer{layer}: {e}")
        return []


# ══════════════════════════════════════════════════════════════
# Main service
# ══════════════════════════════════════════════════════════════

class EmergencyServices:
    """
    Emergency services overlay - hospitals, ambulance, police, fire, SES
    near a route, sourced from Geoscience Australia (CC-BY 4.0).
    """

    def __init__(self, *, conn) -> None:
        self.conn = conn
        # table created by ensure_schema in storage.py

    async def along_route(
        self,
        *,
        polyline6: str,
        buffer_km: float = 25.0,
        cache_seconds: int | None = None,
    ) -> EmergencyServicesOverlay:
        """
        Find emergency facilities within buffer_km of the route.
        """
        if cache_seconds is None:
            cache_seconds = settings.emergency_cache_seconds
        algo_version = settings.emergency_algo_version

        # Cache key
        emergency_key = stable_key("emergency", {
            "polyline6": polyline6,
            "buffer_km": round(buffer_km, 1),
            "algo_version": algo_version,
        })

        # Check cache
        cached = get_emergency_pack(self.conn, emergency_key)
        if cached:
            created_at = cached.get("created_at", "")
            if is_fresh(created_at, max_age_s=cache_seconds):
                logger.debug("emergency cache hit: %s", emergency_key)
                return EmergencyServicesOverlay(**cached)

        # Decode route
        coords = decode_polyline6(polyline6)
        if not coords:
            overlay = EmergencyServicesOverlay(
                emergency_key=emergency_key,
                polyline6=polyline6,
                algo_version=algo_version,
                created_at=utc_now_iso(),
                warnings=["Empty route geometry."],
            )
            put_emergency_pack(
                self.conn,
                emergency_key=emergency_key,
                created_at=overlay.created_at,
                algo_version=algo_version,
                pack=overlay.model_dump(),
            )
            return overlay

        # Sample route points
        samples = sample_route(coords, interval_km=5.0)
        if not samples:
            samples = coords[:1]

        # Bounding box
        bbox = bbox_from_coords(samples, buffer_km)

        warnings: List[str] = []

        # Query layers concurrently
        async with http_client(timeout=25.0) as client:
            ambulance_task = _query_layer(client, _LAYER_AMBULANCE, bbox, warnings)
            police_task = _query_layer(client, _LAYER_POLICE, bbox, warnings)
            metro_fire_task = _query_layer(client, _LAYER_METRO_FIRE, bbox, warnings)
            rural_fire_task = _query_layer(client, _LAYER_RURAL_FIRE, bbox, warnings)
            ses_task = _query_layer(client, _LAYER_SES, bbox, warnings)

            (
                ambulance_results,
                police_results,
                metro_fire_results,
                rural_fire_results,
                ses_results,
            ) = await asyncio.gather(
                ambulance_task,
                police_task,
                metro_fire_task,
                rural_fire_task,
                ses_task,
                return_exceptions=False,
            )

        # Merge all results
        all_facilities: List[EmergencyFacility] = []
        for batch in (
            ambulance_results,
            police_results,
            metro_fire_results,
            rural_fire_results,
            ses_results,
        ):
            if isinstance(batch, list):
                all_facilities.extend(batch)
            elif isinstance(batch, Exception):
                warnings.append(f"emergency:layer fetch error: {batch}")

        # Compute distance from route and filter within buffer
        filtered = filter_by_corridor(
            all_facilities,
            samples,
            buffer_km,
            set_distance=lambda fac, d: setattr(fac, "distance_from_route_km", d),
        )

        # Sort by distance from route
        filtered.sort(key=lambda f: f.distance_from_route_km or 0.0)

        # Cap at MAX_FACILITIES
        if len(filtered) > _MAX_FACILITIES:
            warnings.append(
                f"{len(filtered)} facilities found; limited to {_MAX_FACILITIES}."
            )
            filtered = filtered[:_MAX_FACILITIES]

        logger.info(
            "emergency: polyline=%d chars, bbox=%.3f,%.3f→%.3f,%.3f, "
            "raw=%d filtered=%d",
            len(polyline6),
            bbox[0], bbox[1], bbox[2], bbox[3],
            len(all_facilities),
            len(filtered),
        )

        created_at = utc_now_iso()
        overlay = EmergencyServicesOverlay(
            emergency_key=emergency_key,
            polyline6=polyline6,
            algo_version=algo_version,
            created_at=created_at,
            facilities=filtered,
            warnings=warnings,
        )

        # Persist to cache
        try:
            put_emergency_pack(
                self.conn,
                emergency_key=emergency_key,
                created_at=created_at,
                algo_version=algo_version,
                pack=overlay.model_dump(),
            )
        except Exception as exc:
            logger.warning("[emergency] Cache write failed: %s", exc)

        return overlay
