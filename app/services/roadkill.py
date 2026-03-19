# app/services/roadkill.py
"""
Roadkill / animal vehicle strike hotspot overlay for Roam.

Data source: NSW BioNet Animal Vehicle Strike MapServer (CC BY 3.0 AU, no auth)
  MapServer: https://mapprod2.environment.nsw.gov.au/arcgis/rest/services/EDP/BionetSpeciesSightings_Roadkill/MapServer/0/query
  Source data: NSW Transport + DPIE, from BioNet, crash records, road maintenance
               logs, iNaturalist AU, and IFAW Wildlife Road Casualty app (2021+).
  Coverage: NSW only

Fields used:
  - species_name, common_name (or vernacular_name)
  - latitude, longitude (or geometry point)
  - sighting_date (or observation_date)
  - confidence (Confirmed, Uncertain)
  - sighting_type ("Road Kill", "Wildlife Rescue", etc.)
  - road, locality, lga_name

Algorithm:
  1. Decode polyline6 → bounding box + buffer.
  2. Query ArcGIS MapServer (layer 0) within bbox.
  3. Cluster nearby observations (within _CLUSTER_RADIUS_KM) into hotspots.
  4. Score each hotspot: observation count → risk level.
  5. Distance-filter to route, sort, cache for 7 days.
  6. Twilight risk flagged for each hotspot (used by weather service too).

Risk thresholds:
  high   ≥ 10 observations in cluster
  medium ≥ 3 observations
  low    ≥ 1 observation
"""
from __future__ import annotations

import base64
import hashlib
import logging
from typing import Any, Dict, List, Optional, Tuple

import httpx
from pydantic import BaseModel

from app.core.cache_utils import stable_key, is_fresh
from app.core.geo import decode_polyline6, haversine_km, sample_route, bbox_from_coords, min_dist_to_route
from app.core.storage import put_roadkill_pack, get_roadkill_pack
from app.core.time import utc_now_iso
from app.core.contracts import RoadkillHotspot, RoadkillOverlay

logger = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════════
# Constants
# ══════════════════════════════════════════════════════════════

_CACHE_TTL_S = 7 * 86_400  # 7 days
_ALGO_VERSION = "roadkill-1.0"
_MAX_HOTSPOTS = 100
_HTTP_TIMEOUT = 25.0
_CLUSTER_RADIUS_KM = 2.0

_ROADKILL_URL = (
    "https://mapprod2.environment.nsw.gov.au/arcgis/rest/services"
    "/EDP/BionetSpeciesSightings_Roadkill/MapServer/0/query"
)

_HIGH_RISK_COUNT = 10
_MEDIUM_RISK_COUNT = 3


# ══════════════════════════════════════════════════════════════
# Internal observation model (not exposed in contracts)
# ══════════════════════════════════════════════════════════════

class RoadkillObservation(BaseModel):
    species_name: Optional[str] = None
    common_name: Optional[str] = None
    lat: float
    lng: float
    sighting_date: Optional[str] = None
    confidence: Optional[str] = None
    road: Optional[str] = None
    locality: Optional[str] = None


# ══════════════════════════════════════════════════════════════
# Clustering
# ══════════════════════════════════════════════════════════════

def _cluster_observations(
    observations: List[RoadkillObservation],
    cluster_radius_km: float,
    route_samples: List[Tuple[float, float]],
    buffer_km: float,
) -> List[RoadkillHotspot]:
    """
    Simple greedy spatial clustering.
    Each observation not yet assigned joins the nearest existing cluster
    (if within cluster_radius_km) or starts a new one.
    """
    clusters: List[Dict[str, Any]] = []

    for obs in observations:
        assigned = False
        for cl in clusters:
            d = haversine_km((obs.lat, obs.lng), (cl["lat"], cl["lng"]))
            if d <= cluster_radius_km:
                cl["observations"].append(obs)
                # Update centroid (running mean)
                n = len(cl["observations"])
                cl["lat"] = cl["lat"] + (obs.lat - cl["lat"]) / n
                cl["lng"] = cl["lng"] + (obs.lng - cl["lng"]) / n
                assigned = True
                break
        if not assigned:
            clusters.append({
                "lat": obs.lat,
                "lng": obs.lng,
                "observations": [obs],
            })

    hotspots: List[RoadkillHotspot] = []
    for cl in clusters:
        lat, lng = cl["lat"], cl["lng"]
        dist_km = min_dist_to_route(lat, lng, route_samples)
        if dist_km > buffer_km:
            continue

        obs_list: List[RoadkillObservation] = cl["observations"]
        count = len(obs_list)

        if count >= _HIGH_RISK_COUNT:
            risk_level = "high"
        elif count >= _MEDIUM_RISK_COUNT:
            risk_level = "medium"
        else:
            risk_level = "low"

        # Dominant species: top 3 by frequency
        species_counts: Dict[str, int] = {}
        for o in obs_list:
            name = o.common_name or o.species_name
            if name:
                species_counts[name] = species_counts.get(name, 0) + 1
        dominant = sorted(species_counts, key=lambda k: species_counts[k], reverse=True)[:3]

        # Most recent sighting date
        dates = [o.sighting_date for o in obs_list if o.sighting_date]
        latest = max(dates) if dates else None

        # Road + locality from most-represented observation
        road = next((o.road for o in obs_list if o.road), None)
        locality = next((o.locality for o in obs_list if o.locality), None)

        raw_id = f"roadkill::{round(lat, 5)}::{round(lng, 5)}"
        hid = base64.urlsafe_b64encode(
            hashlib.sha256(raw_id.encode()).digest()
        ).decode().rstrip("=")[:20]

        hotspots.append(RoadkillHotspot(
            id=hid,
            lat=round(lat, 6),
            lng=round(lng, 6),
            observation_count=count,
            risk_level=risk_level,
            species=dominant,
            road=road,
            locality=locality,
            distance_from_route_km=round(dist_km, 2),
            latest_sighting=latest,
        ))

    return hotspots


# ══════════════════════════════════════════════════════════════
# Data fetcher
# ══════════════════════════════════════════════════════════════

def _parse_float(v: Any) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


async def _fetch_observations(
    client: httpx.AsyncClient,
    min_lat: float,
    min_lng: float,
    max_lat: float,
    max_lng: float,
    warnings: List[str],
) -> List[RoadkillObservation]:
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
        resp = await client.get(_ROADKILL_URL, params=params, timeout=_HTTP_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        warnings.append(f"roadkill:fetch: {e}")
        return []

    features = data.get("features") or []
    observations: List[RoadkillObservation] = []

    for feat in features:
        try:
            attrs = feat.get("attributes") or {}
            geom = feat.get("geometry") or {}

            lat = (
                _parse_float(attrs.get("latitude") or attrs.get("Latitude") or attrs.get("lat"))
                or _parse_float(geom.get("y"))
            )
            lng = (
                _parse_float(attrs.get("longitude") or attrs.get("Longitude") or attrs.get("lon") or attrs.get("long"))
                or _parse_float(geom.get("x"))
            )
            if lat is None or lng is None:
                continue

            species_name = str(
                attrs.get("species_name") or attrs.get("ScientificName") or attrs.get("scientific_name") or ""
            ).strip() or None
            common_name = str(
                attrs.get("common_name") or attrs.get("CommonName") or attrs.get("vernacular_name") or ""
            ).strip() or None

            sighting_date = str(attrs.get("sighting_date") or attrs.get("observation_date") or "").strip() or None
            confidence = str(attrs.get("confidence") or attrs.get("Confidence") or "").strip() or None
            road = str(attrs.get("road") or attrs.get("Road") or attrs.get("road_name") or "").strip() or None
            locality = str(
                attrs.get("locality") or attrs.get("lga_name") or attrs.get("LGA") or ""
            ).strip() or None

            observations.append(RoadkillObservation(
                species_name=species_name,
                common_name=common_name,
                lat=lat,
                lng=lng,
                sighting_date=sighting_date,
                confidence=confidence,
                road=road,
                locality=locality,
            ))
        except Exception as e:
            warnings.append(f"roadkill:parse: {e}")

    logger.info("roadkill: NSW returned %d features → %d observations", len(features), len(observations))
    return observations


# ══════════════════════════════════════════════════════════════
# Main service class
# ══════════════════════════════════════════════════════════════

class Roadkill:
    """
    Roadkill / animal vehicle strike hotspot overlay service.

    Queries NSW BioNet Animal Vehicle Strike data and clusters nearby
    observations into hotspots scored by severity.
    """

    def __init__(self, *, conn) -> None:
        self.conn = conn

    async def along_route(
        self,
        *,
        polyline6: str,
        buffer_km: float = 15.0,
        cache_seconds: int = _CACHE_TTL_S,
    ) -> RoadkillOverlay:
        roadkill_key = stable_key("roadkill", {
            "polyline6": polyline6,
            "buffer_km": round(buffer_km, 1),
            "algo_version": _ALGO_VERSION,
        })

        cached = get_roadkill_pack(self.conn, roadkill_key)
        if cached:
            created_at = cached.get("created_at", "")
            if is_fresh(created_at, max_age_s=cache_seconds):
                logger.debug("roadkill cache hit: %s", roadkill_key)
                return RoadkillOverlay.model_validate(cached)

        coords = decode_polyline6(polyline6)
        if not coords:
            overlay = RoadkillOverlay(
                roadkill_key=roadkill_key,
                polyline6=polyline6,
                algo_version=_ALGO_VERSION,
                created_at=utc_now_iso(),
                warnings=["Failed to decode route polyline."],
            )
            put_roadkill_pack(self.conn, roadkill_key=roadkill_key, created_at=overlay.created_at, algo_version=_ALGO_VERSION, pack=overlay.model_dump())
            return overlay

        route_samples = sample_route(coords, interval_km=3.0)
        min_lat, min_lng, max_lat, max_lng = bbox_from_coords(coords, buffer_km)
        warnings: List[str] = []

        # Only query if route overlaps NSW (lat -28.15 to -37.5, lng 141 to 153.6)
        if max_lat < -37.5 or min_lat > -28.15 or max_lng < 141.0 or min_lng > 153.6:
            overlay = RoadkillOverlay(
                roadkill_key=roadkill_key,
                polyline6=polyline6,
                algo_version=_ALGO_VERSION,
                created_at=utc_now_iso(),
                warnings=["Route does not pass through NSW - roadkill data not available outside NSW."],
            )
            put_roadkill_pack(self.conn, roadkill_key=roadkill_key, created_at=overlay.created_at, algo_version=_ALGO_VERSION, pack=overlay.model_dump())
            return overlay

        async with httpx.AsyncClient(follow_redirects=True, timeout=httpx.Timeout(_HTTP_TIMEOUT)) as client:
            observations = await _fetch_observations(
                client, min_lat, min_lng, max_lat, max_lng, warnings,
            )

        hotspots = _cluster_observations(observations, _CLUSTER_RADIUS_KM, route_samples, buffer_km)
        hotspots.sort(key=lambda h: (
            {"high": 0, "medium": 1, "low": 2}.get(h.risk_level, 3),
            h.distance_from_route_km or 0.0,
        ))

        if len(hotspots) > _MAX_HOTSPOTS:
            warnings.append(f"{len(hotspots)} hotspots found; limited to {_MAX_HOTSPOTS}.")
            hotspots = hotspots[:_MAX_HOTSPOTS]

        logger.info(
            "roadkill: polyline=%d chars, observations=%d → hotspots=%d",
            len(polyline6), len(observations), len(hotspots),
        )

        created_at = utc_now_iso()
        overlay = RoadkillOverlay(
            roadkill_key=roadkill_key,
            polyline6=polyline6,
            algo_version=_ALGO_VERSION,
            created_at=created_at,
            hotspots=hotspots,
            total_observations=len(observations),
            warnings=warnings,
        )
        put_roadkill_pack(self.conn, roadkill_key=roadkill_key, created_at=created_at, algo_version=_ALGO_VERSION, pack=overlay.model_dump())
        return overlay
