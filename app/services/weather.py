from __future__ import annotations

"""
Weather overlay service for Roam.

Data source: Open-Meteo ECMWF IFS 0.25° model (self-hosted).
  Endpoint : {OPEN_METEO_BASE_URL}/v1/forecast?models=ecmwf_ifs025
  Auth     : None required (self-hosted); AGPLv3 engine + CC-BY 4.0 data.
  Returns  : Hourly forecast at route sample points, timed to user ETA.

Algorithm
---------
1. Decode the route polyline6.
2. Compute cumulative distances and per-point ETAs from departure_iso + avg_speed_kmh.
3. Sample every weather_sample_interval_km (default 50 km).
4. Batch all sample coordinates into a single Open-Meteo request (comma-separated).
5. For each sample, pick the forecast hour closest to the ETA at that point.
6. Derive sunrise/sunset, twilight danger, WMO weather description.
7. Cache the result for weather_cache_seconds (default 1 hour).
"""

import asyncio
import logging
import math
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Tuple

import httpx

from app.core.cache_utils import is_fresh, stable_key
from app.core.contracts import WeatherOverlay, WeatherPoint
from app.core.geo import decode_polyline6, haversine_km
from app.core.settings import settings
from app.core.storage import get_weather_pack, put_weather_pack
from app.core.time import utc_now_iso

logger = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════════
# WMO Weather Codes → human descriptions
# ══════════════════════════════════════════════════════════════

_WMO_DESCRIPTIONS = {
    0: "Clear sky",
    1: "Mainly clear",
    2: "Partly cloudy",
    3: "Overcast",
    45: "Fog",
    48: "Depositing rime fog",
    51: "Light drizzle",
    53: "Moderate drizzle",
    55: "Dense drizzle",
    56: "Light freezing drizzle",
    57: "Dense freezing drizzle",
    61: "Slight rain",
    63: "Moderate rain",
    65: "Heavy rain",
    66: "Light freezing rain",
    67: "Heavy freezing rain",
    71: "Slight snow fall",
    73: "Moderate snow fall",
    75: "Heavy snow fall",
    77: "Snow grains",
    80: "Slight rain showers",
    81: "Moderate rain showers",
    82: "Violent rain showers",
    85: "Slight snow showers",
    86: "Heavy snow showers",
    95: "Thunderstorm",
    96: "Thunderstorm with slight hail",
    99: "Thunderstorm with heavy hail",
}

# ══════════════════════════════════════════════════════════════
# Open-Meteo config
# ══════════════════════════════════════════════════════════════

# Hourly vars we request - maps to WeatherPoint fields.
_HOURLY_VARS = ",".join([
    "temperature_2m",
    "apparent_temperature",
    "precipitation_probability",
    "precipitation",
    "weather_code",
    "wind_speed_10m",
    "wind_gusts_10m",
    "wind_direction_10m",
    "cloud_cover",
    "visibility",
])

# Daily vars for sunrise/sunset/UV.
_DAILY_VARS = "sunrise,sunset,uv_index_max"

# Max locations per Open-Meteo request (their batch limit).
_MAX_BATCH = 50


# ══════════════════════════════════════════════════════════════
# Geo / sampling helpers
# ══════════════════════════════════════════════════════════════

def _cumulative_distances(coords: List[Tuple[float, float]]) -> List[float]:
    dists = [0.0]
    for i in range(1, len(coords)):
        d = haversine_km(coords[i - 1], coords[i])
        dists.append(dists[-1] + d)
    return dists


def _sample_with_eta(
    coords: List[Tuple[float, float]],
    cum_dists: List[float],
    interval_km: float,
    departure: datetime,
    avg_speed_kmh: float,
) -> List[Tuple[float, float, float, datetime]]:
    """
    Sample every interval_km; always include start and end.
    Returns [(lat, lng, km_along, eta_datetime), ...].
    """
    total_km = cum_dists[-1]
    if total_km == 0 or not coords:
        return []

    speed_kms = avg_speed_kmh / 3600.0  # km per second

    def _eta(km: float) -> datetime:
        if speed_kms <= 0:
            return departure
        return departure + timedelta(seconds=km / speed_kms)

    samples: List[Tuple[float, float, float, datetime]] = [
        (coords[0][0], coords[0][1], 0.0, _eta(0.0))
    ]
    target_km = interval_km
    i = 0
    while target_km < total_km:
        while i < len(cum_dists) - 1 and cum_dists[i + 1] < target_km:
            i += 1
        if i >= len(coords) - 1:
            break
        seg_len = cum_dists[i + 1] - cum_dists[i]
        frac = (target_km - cum_dists[i]) / seg_len if seg_len > 0 else 0.0
        lat = coords[i][0] + frac * (coords[i + 1][0] - coords[i][0])
        lng = coords[i][1] + frac * (coords[i + 1][1] - coords[i][1])
        samples.append((lat, lng, target_km, _eta(target_km)))
        target_km += interval_km

    last_lat, last_lng = coords[-1]
    if not samples or haversine_km((samples[-1][0], samples[-1][1]), (last_lat, last_lng)) > 0.5:
        samples.append((last_lat, last_lng, total_km, _eta(total_km)))

    return samples


# ══════════════════════════════════════════════════════════════
# Open-Meteo API fetch
# ══════════════════════════════════════════════════════════════

async def _fetch_batch(
    client: httpx.AsyncClient,
    base_url: str,
    lats: List[float],
    lngs: List[float],
    warnings: List[str],
    api_key: str = "",
) -> Optional[dict]:
    """Fetch forecast for a batch of lat/lng from Open-Meteo."""
    lat_str = ",".join(f"{x:.4f}" for x in lats)
    lng_str = ",".join(f"{x:.4f}" for x in lngs)

    params = {
        "latitude": lat_str,
        "longitude": lng_str,
        "hourly": _HOURLY_VARS,
        "daily": _DAILY_VARS,
        "models": "ecmwf_ifs025",
        "timezone": "auto",
        "forecast_days": 10,
    }
    if api_key:
        params["apikey"] = api_key

    try:
        resp = await client.get(f"{base_url}/v1/forecast", params=params, timeout=30.0)
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:
        warnings.append(f"weather:fetch error: {exc}")
        return None


def _parse_iso_local(s: str) -> Optional[datetime]:
    """Parse an ISO datetime string from Open-Meteo (may lack timezone)."""
    try:
        s = s.strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def _closest_hour_index(times: List[str], target: datetime) -> int:
    """Find the hourly time slot closest to target datetime."""
    best_idx = 0
    best_diff = float("inf")
    for i, t_str in enumerate(times):
        dt = _parse_iso_local(t_str)
        if dt is None:
            continue
        diff = abs((dt - target).total_seconds())
        if diff < best_diff:
            best_diff = diff
            best_idx = i
    return best_idx


def _is_between(check: datetime, start_str: Optional[str], end_str: Optional[str]) -> bool:
    """Check if datetime falls between two ISO strings."""
    if not start_str or not end_str:
        return False
    start = _parse_iso_local(start_str)
    end = _parse_iso_local(end_str)
    if not start or not end:
        return False
    return start <= check <= end


def _is_twilight(
    check: datetime,
    sunrise_str: Optional[str],
    sunset_str: Optional[str],
    margin_minutes: int = 30,
) -> bool:
    """Dawn/dusk ±margin_minutes = wildlife risk window."""
    if not sunrise_str or not sunset_str:
        return False
    sunrise = _parse_iso_local(sunrise_str)
    sunset = _parse_iso_local(sunset_str)
    if not sunrise or not sunset:
        return False
    margin = timedelta(minutes=margin_minutes)
    dawn_start = sunrise - margin
    dawn_end = sunrise + margin
    dusk_start = sunset - margin
    dusk_end = sunset + margin
    return (dawn_start <= check <= dawn_end) or (dusk_start <= check <= dusk_end)


def _extract_point(
    location_data: dict,
    eta: datetime,
    km_along: float,
    lat: float,
    lng: float,
) -> Optional[WeatherPoint]:
    """Extract a WeatherPoint from a single location's Open-Meteo response."""
    hourly = location_data.get("hourly") or {}
    daily = location_data.get("daily") or {}
    times = hourly.get("time") or []

    if not times:
        return None

    idx = _closest_hour_index(times, eta)

    def _h(key: str, default=0):
        arr = hourly.get(key) or []
        return arr[idx] if idx < len(arr) and arr[idx] is not None else default

    # Daily: find the day matching ETA
    daily_times = daily.get("time") or []
    eta_date_str = eta.strftime("%Y-%m-%d")
    day_idx = 0
    for di, dt_str in enumerate(daily_times):
        if dt_str == eta_date_str:
            day_idx = di
            break

    def _d(key: str, default=None):
        arr = daily.get(key) or []
        return arr[day_idx] if day_idx < len(arr) and arr[day_idx] is not None else default

    sunrise_str = _d("sunrise")
    sunset_str = _d("sunset")
    uv_index = _d("uv_index_max", 0.0)

    weather_code = int(_h("weather_code", 0))
    visibility_raw = _h("visibility", None)

    return WeatherPoint(
        lat=round(lat, 6),
        lng=round(lng, 6),
        km_along=round(km_along, 2),
        eta_iso=eta.isoformat(),
        temperature_c=round(float(_h("temperature_2m", 20.0)), 1),
        apparent_temperature_c=round(float(_h("apparent_temperature", 20.0)), 1),
        precipitation_probability_pct=int(_h("precipitation_probability", 0)),
        precipitation_mm=round(float(_h("precipitation", 0.0)), 1),
        weather_code=weather_code,
        weather_description=_WMO_DESCRIPTIONS.get(weather_code, f"WMO code {weather_code}"),
        wind_speed_kmh=round(float(_h("wind_speed_10m", 0.0)), 1),
        wind_gust_kmh=round(float(_h("wind_gusts_10m", 0.0)), 1) if _h("wind_gusts_10m", None) is not None else None,
        wind_direction_deg=int(_h("wind_direction_10m", 0)),
        uv_index=round(float(uv_index), 1),
        cloud_cover_pct=int(_h("cloud_cover", 0)),
        visibility_m=round(float(visibility_raw), 0) if visibility_raw is not None else None,
        sunrise_iso=sunrise_str,
        sunset_iso=sunset_str,
        civil_twilight_begin_iso=None,  # Open-Meteo doesn't provide civil twilight directly
        civil_twilight_end_iso=None,
        is_daylight=_is_between(eta, sunrise_str, sunset_str),
        is_twilight_danger=_is_twilight(eta, sunrise_str, sunset_str),
    )


# ══════════════════════════════════════════════════════════════
# Service
# ══════════════════════════════════════════════════════════════

class Weather:
    """
    Weather overlay service.

    Powered by Open-Meteo BOM ACCESS-G model (self-hosted or public).
    Results are cached in SQLite for 1 hour (weather changes frequently).
    """

    def __init__(self, *, conn):
        self.conn = conn

    async def forecast_along_route(
        self,
        *,
        polyline6: str,
        departure_iso: str,
        avg_speed_kmh: float = 90.0,
        sample_interval_km: float | None = None,
    ) -> WeatherOverlay:
        """Build weather overlay with per-point forecasts timed to user ETA."""
        algo_version = settings.weather_algo_version
        max_age = settings.weather_cache_seconds
        interval = sample_interval_km or settings.weather_sample_interval_km
        base_url = settings.open_meteo_base_url
        api_key = settings.open_meteo_api_key

        # Cache key
        weather_key = stable_key("weather", {
            "polyline6": polyline6,
            "departure_iso": departure_iso,
            "speed": round(avg_speed_kmh, 1),
            "interval_km": round(interval, 1),
            "algo_version": algo_version,
        })

        # Check cache
        cached = get_weather_pack(self.conn, weather_key)
        if cached:
            created_at = cached.get("created_at", "")
            if is_fresh(created_at, max_age_s=max_age):
                return WeatherOverlay(**cached)

        # Parse departure
        try:
            dep = departure_iso.strip()
            if dep.endswith("Z"):
                dep = dep[:-1] + "+00:00"
            departure = datetime.fromisoformat(dep)
            if departure.tzinfo is None:
                departure = departure.replace(tzinfo=timezone.utc)
        except Exception:
            departure = datetime.now(timezone.utc)

        # Decode geometry
        coords = decode_polyline6(polyline6)
        if not coords:
            return WeatherOverlay(
                weather_key=weather_key,
                polyline6=polyline6,
                departure_iso=departure_iso,
                algo_version=algo_version,
                created_at=utc_now_iso(),
                points=[],
                warnings=["Empty route geometry."],
            )

        cum_dists = _cumulative_distances(coords)
        samples = _sample_with_eta(coords, cum_dists, interval, departure, avg_speed_kmh)

        if not samples:
            return WeatherOverlay(
                weather_key=weather_key,
                polyline6=polyline6,
                departure_iso=departure_iso,
                algo_version=algo_version,
                created_at=utc_now_iso(),
                points=[],
                warnings=["No sample points generated from route."],
            )

        warnings: List[str] = []
        points: List[WeatherPoint] = []

        # Batch into groups of _MAX_BATCH and fetch
        transport = httpx.AsyncHTTPTransport(retries=2)
        async with httpx.AsyncClient(
            follow_redirects=True,
            transport=transport,
            timeout=httpx.Timeout(45.0),
        ) as client:
            for batch_start in range(0, len(samples), _MAX_BATCH):
                batch = samples[batch_start : batch_start + _MAX_BATCH]
                lats = [s[0] for s in batch]
                lngs = [s[1] for s in batch]

                data = await _fetch_batch(client, base_url, lats, lngs, warnings, api_key=api_key)
                if data is None:
                    continue

                # Single location → data is the location object directly.
                # Multiple locations → data is a list.
                if len(batch) == 1:
                    location_list = [data]
                else:
                    # Open-Meteo returns a list when multiple coords are given.
                    # Each item has its own hourly/daily.
                    location_list = data if isinstance(data, list) else [data]

                for i, loc_data in enumerate(location_list):
                    if i >= len(batch):
                        break
                    lat, lng, km, eta = batch[i]
                    pt = _extract_point(loc_data, eta, km, lat, lng)
                    if pt:
                        points.append(pt)

        created_at = utc_now_iso()
        overlay = WeatherOverlay(
            weather_key=weather_key,
            polyline6=polyline6,
            departure_iso=departure_iso,
            algo_version=algo_version,
            created_at=created_at,
            points=points,
            warnings=warnings,
        )

        # Persist to cache
        try:
            put_weather_pack(
                self.conn,
                weather_key=weather_key,
                created_at=created_at,
                algo_version=algo_version,
                pack=overlay.model_dump(),
            )
        except Exception as exc:
            logger.warning("[weather] Cache write failed: %s", exc)

        return overlay
