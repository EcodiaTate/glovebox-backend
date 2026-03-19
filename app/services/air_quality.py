# app/services/air_quality.py
"""
Air quality overlay service for Roam.

Data source: OpenWeatherMap Air Pollution API (free tier, 1M calls/month)
  Endpoint : http://api.openweathermap.org/data/2.5/air_pollution
  Auth     : appid query parameter (OWM_API_KEY env var)
  Returns  : AQI 1-5 scale + individual pollutant concentrations (ug/m3)

Algorithm
---------
1. Decode the route polyline6.
2. Sample every sample_interval_km km along the route (default 50 km).
3. For each sample, query the OWM air pollution endpoint.
4. Limit concurrency with asyncio.Semaphore(5).
5. Map AQI 1-5 to descriptive labels + health advice.
6. Overall route AQI = worst (max) segment AQI.
7. Cache result in aqi_packs for 3600s (1 hour) - air quality changes frequently.
"""
from __future__ import annotations

import asyncio
import logging
from typing import List, Optional, Tuple

import httpx

from app.core.contracts import AirQualityPoint, AirQualityOverlay

from app.core.settings import settings
from app.core.storage import get_aqi_pack, put_aqi_pack
from app.core.time import utc_now_iso
from app.core.geo import decode_polyline6, cumulative_distances, interpolated_samples
from app.core.http_client import http_client
from app.core.cache_utils import is_fresh, stable_key

logger = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════════
# Constants
# ══════════════════════════════════════════════════════════════

_OWM_AIR_URL = "http://api.openweathermap.org/data/2.5/air_pollution"
_MAX_CONCURRENT = 5

_AQI_LABELS = {
    1: "Good",
    2: "Fair",
    3: "Moderate",
    4: "Poor",
    5: "Very Poor",
}

_AQI_ADVICE = {
    1: "Air quality is satisfactory",
    2: "Acceptable; sensitive groups may notice effects",
    3: "Sensitive groups should reduce outdoor exertion",
    4: "Everyone should reduce outdoor exertion",
    5: "Health alert - avoid outdoor activities",
}

# ══════════════════════════════════════════════════════════════
# Cache helpers
# ══════════════════════════════════════════════════════════════


# ══════════════════════════════════════════════════════════════
# OWM fetch helper
# ══════════════════════════════════════════════════════════════

async def _fetch_point_aqi(
    client: httpx.AsyncClient,
    sem: asyncio.Semaphore,
    lat: float,
    lng: float,
    km_along: float,
    api_key: str,
    warnings: list,
) -> Optional[AirQualityPoint]:
    """Fetch air quality for a single lat/lng from OWM."""
    async with sem:
        try:
            resp = await client.get(
                _OWM_AIR_URL,
                params={
                    "lat": round(lat, 6),
                    "lon": round(lng, 6),
                    "appid": api_key,
                },
                timeout=15.0,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            warnings.append(f"aqi:fetch({lat:.4f},{lng:.4f}): {exc}")
            return None

    # Parse OWM response: {"coord":..., "list":[{"main":{"aqi":N}, "components":{...}}]}
    items = data.get("list") or []
    if not items:
        warnings.append(f"aqi:empty response at ({lat:.4f},{lng:.4f})")
        return None

    entry = items[0]
    main = entry.get("main") or {}
    components = entry.get("components") or {}

    aqi = main.get("aqi", 1)
    aqi = max(1, min(5, int(aqi)))

    return AirQualityPoint(
        lat=round(lat, 6),
        lng=round(lng, 6),
        km_along=round(km_along, 2),
        aqi=aqi,
        aqi_label=_AQI_LABELS.get(aqi, "Unknown"),
        pm25=components.get("pm2_5"),
        pm10=components.get("pm10"),
        co=components.get("co"),
        no2=components.get("no2"),
        o3=components.get("o3"),
        so2=components.get("so2"),
    )


# ══════════════════════════════════════════════════════════════
# Service
# ══════════════════════════════════════════════════════════════

class AirQuality:
    """
    Air quality overlay service.

    Powered by OpenWeatherMap Air Pollution API (free tier).
    Results are cached in SQLite for 1 hour (air quality changes frequently).
    """

    def __init__(self, *, conn) -> None:
        self.conn = conn

    async def along_route(
        self,
        *,
        polyline6: str,
        sample_interval_km: float = 50.0,
        cache_seconds: int | None = None,
    ) -> AirQualityOverlay:
        """
        Build an air quality overlay along a route.

        Returns an AirQualityOverlay with per-point AQI readings and
        an overall route AQI (worst segment).
        """
        algo_version = settings.aqi_algo_version
        max_age = cache_seconds if cache_seconds is not None else settings.aqi_cache_seconds

        api_key = settings.owm_api_key
        if not api_key:
            return AirQualityOverlay(
                aqi_key="no-key",
                polyline6=polyline6,
                algo_version=algo_version,
                created_at=utc_now_iso(),
                warnings=["OWM_API_KEY not configured - air quality unavailable."],
            )

        # Cache key
        aqi_key = stable_key("aqi", {
            "polyline6": polyline6,
            "interval_km": round(sample_interval_km, 1),
            "algo_version": algo_version,
        })

        # Check cache
        cached = get_aqi_pack(self.conn, aqi_key)
        if cached:
            created_at = cached.get("created_at", "")
            if is_fresh(created_at, max_age_s=max_age):
                return AirQualityOverlay(**cached)

        # Decode geometry
        coords = decode_polyline6(polyline6)
        if not coords:
            return AirQualityOverlay(
                aqi_key=aqi_key,
                polyline6=polyline6,
                algo_version=algo_version,
                created_at=utc_now_iso(),
                warnings=["Empty route geometry."],
            )

        cum_dists = cumulative_distances(coords)
        samples = interpolated_samples(coords, cum_dists, sample_interval_km)

        if not samples:
            return AirQualityOverlay(
                aqi_key=aqi_key,
                polyline6=polyline6,
                algo_version=algo_version,
                created_at=utc_now_iso(),
                warnings=["No sample points generated from route."],
            )

        warnings: list[str] = []
        points: list[AirQualityPoint] = []

        # Fetch AQI for each sample point
        sem = asyncio.Semaphore(_MAX_CONCURRENT)
        async with http_client(timeout=20.0) as client:
            tasks = [
                _fetch_point_aqi(client, sem, lat, lng, km, api_key, warnings)
                for lat, lng, km in samples
            ]
            results = await asyncio.gather(*tasks, return_exceptions=True)

        for r in results:
            if isinstance(r, AirQualityPoint):
                points.append(r)
            elif isinstance(r, Exception):
                warnings.append(f"aqi:fetch error: {r}")

        # Compute overall AQI (worst segment)
        if points:
            overall_aqi = max(p.aqi for p in points)
        else:
            overall_aqi = 1

        overall_label = _AQI_LABELS.get(overall_aqi, "Unknown")
        health_advice = _AQI_ADVICE.get(overall_aqi, "")

        created_at = utc_now_iso()
        overlay = AirQualityOverlay(
            aqi_key=aqi_key,
            polyline6=polyline6,
            algo_version=algo_version,
            created_at=created_at,
            points=points,
            overall_aqi=overall_aqi,
            overall_label=overall_label,
            health_advice=health_advice,
            warnings=warnings,
        )

        # Persist to cache
        try:
            put_aqi_pack(
                self.conn,
                aqi_key=aqi_key,
                created_at=created_at,
                algo_version=algo_version,
                pack=overlay.model_dump(),
            )
        except Exception as exc:
            logger.warning("[air_quality] Cache write failed: %s", exc)

        return overlay
