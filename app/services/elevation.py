from __future__ import annotations

import logging
import math
import os
from typing import List, Optional, Tuple

import httpx

from app.core.contracts import (
    ElevationProfile,
    ElevationRequest,
    ElevationSample,
    GradeSegment,
)
from app.core.errors import service_unavailable
from app.core.polyline6 import decode_polyline6
from app.core.time import utc_now_iso

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────
# Geometry helpers
# ──────────────────────────────────────────────────────────────

def _haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Distance in metres between two (lat, lng) points."""
    R = 6_371_000.0
    rlat1, rlat2 = math.radians(lat1), math.radians(lat2)
    dlat = math.radians(lat2 - lat1)
    dlng = math.radians(lng2 - lng1)
    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(rlat1) * math.cos(rlat2) * math.sin(dlng / 2) ** 2
    )
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _interpolate(
    lat1: float, lng1: float, lat2: float, lng2: float, frac: float
) -> Tuple[float, float]:
    """Linear interpolation between two points. frac in [0, 1]."""
    return (
        lat1 + (lat2 - lat1) * frac,
        lng1 + (lng2 - lng1) * frac,
    )


def _sample_polyline(
    pts: List[Tuple[float, float]], interval_m: float
) -> List[Tuple[float, float, float]]:
    """
    Walk along a polyline at fixed intervals, returning
    [(lat, lng, km_along), ...].

    Always includes the first and last point.
    """
    if not pts:
        return []

    samples: List[Tuple[float, float, float]] = []
    samples.append((pts[0][0], pts[0][1], 0.0))

    cumulative_m = 0.0
    next_sample_m = interval_m

    for i in range(1, len(pts)):
        seg_m = _haversine_m(pts[i - 1][0], pts[i - 1][1], pts[i][0], pts[i][1])

        if seg_m < 1e-3:
            continue

        seg_start_m = cumulative_m

        while next_sample_m <= cumulative_m + seg_m:
            frac = (next_sample_m - seg_start_m) / seg_m if seg_m > 0 else 0
            frac = max(0.0, min(1.0, frac))  # clamp
            lat, lng = _interpolate(
                pts[i - 1][0], pts[i - 1][1], pts[i][0], pts[i][1], frac
            )
            samples.append((lat, lng, next_sample_m / 1000.0))
            next_sample_m += interval_m
            seg_start_m = next_sample_m - interval_m

        cumulative_m += seg_m

    # Always include the final point
    last = pts[-1]
    if len(samples) == 0 or (
        abs(samples[-1][0] - last[0]) > 1e-7
        or abs(samples[-1][1] - last[1]) > 1e-7
    ):
        samples.append((last[0], last[1], cumulative_m / 1000.0))

    return samples


# ──────────────────────────────────────────────────────────────
# API endpoints
# ──────────────────────────────────────────────────────────────

# OpenTopography SRTM30M (1 arc-second ~30m resolution, global)
# Requires free API key: https://portal.opentopography.org/requestService?service=api
# Env var: OPENTOPOGRAPHY_API_KEY
_OT_URL = "https://portal.opentopography.org/API/globaldem"
_OT_BATCH_SIZE = 100   # OT returns a raster; we send a bbox + point count

# Open-Elevation fallback (no key required, less reliable)
_OPEN_ELEV_URL = "https://api.open-elevation.com/api/v1/lookup"
_OPEN_ELEV_BATCH_SIZE = 200


class Elevation:
    """
    Fetches elevation profiles for route geometry.

    Primary: OpenTopography SRTM30M API (requires OPENTOPOGRAPHY_API_KEY).
    Fallback: Open-Elevation public API (no key, less reliable).

    If neither succeeds, returns zeroed elevations rather than raising -
    callers can still build fuel/grade summaries; they just won't be accurate.
    """

    def __init__(self, *, timeout_s: float = 30.0, api_key: Optional[str] = None):
        self.client = httpx.Client(timeout=timeout_s)
        self._api_key: Optional[str] = api_key or os.getenv("OPENTOPOGRAPHY_API_KEY") or ""

    def profile(self, req: ElevationRequest) -> ElevationProfile:
        """Build a full elevation profile from a polyline6 geometry."""
        pts = decode_polyline6(req.geometry)
        if len(pts) < 2:
            service_unavailable("elevation_bad_geometry", "Need at least 2 points")

        # Sample points along the route at the requested interval
        sample_coords = _sample_polyline(pts, float(req.sample_interval_m))
        if not sample_coords:
            service_unavailable("elevation_no_samples", "Failed to sample route")

        # Fetch elevation values for all sample points
        latlngs = [(s[0], s[1]) for s in sample_coords]
        elevations = self._fetch_elevations(latlngs)

        # Build samples
        samples: list[ElevationSample] = []
        for i, (lat, lng, km_along) in enumerate(sample_coords):
            samples.append(
                ElevationSample(
                    km_along=round(km_along, 2),
                    elevation_m=round(elevations[i], 1),
                    lat=round(lat, 6),
                    lng=round(lng, 6),
                )
            )

        # Compute stats
        elev_values = [s.elevation_m for s in samples]
        total_ascent = 0.0
        total_descent = 0.0
        for i in range(1, len(elev_values)):
            diff = elev_values[i] - elev_values[i - 1]
            if diff > 0:
                total_ascent += diff
            else:
                total_descent += abs(diff)

        return ElevationProfile(
            route_key=req.route_key,
            samples=samples,
            min_elevation_m=round(min(elev_values), 1),
            max_elevation_m=round(max(elev_values), 1),
            total_ascent_m=round(total_ascent, 1),
            total_descent_m=round(total_descent, 1),
            created_at=utc_now_iso(),
        )

    def _fetch_elevations(
        self, latlngs: List[Tuple[float, float]]
    ) -> List[float]:
        """
        Fetch elevation for a list of (lat, lng) pairs.

        Tries OpenTopography SRTM30M first (if key present), falls back to
        Open-Elevation. Returns 0.0 for any point that can't be resolved
        rather than raising - callers degrade gracefully.
        """
        if self._api_key:
            try:
                return self._fetch_opentopography(latlngs)
            except Exception as e:
                logger.warning("elevation: OpenTopography failed (%s), falling back to Open-Elevation", e)

        try:
            return self._fetch_open_elevation(latlngs)
        except Exception as e:
            logger.warning("elevation: Open-Elevation also failed (%s), returning zeros", e)
            return [0.0] * len(latlngs)

    def _fetch_opentopography(self, latlngs: List[Tuple[float, float]]) -> List[float]:
        """
        OpenTopography globaldem API - SRTM30M (1 arc-second, ~30m).

        The API returns a GeoTIFF raster for a bbox; we use the point-query
        form by sending the exact coordinates and parsing the JSON response.
        Processes in batches of _OT_BATCH_SIZE.
        """
        all_elevations: List[float] = []

        for batch_start in range(0, len(latlngs), _OT_BATCH_SIZE):
            batch = latlngs[batch_start : batch_start + _OT_BATCH_SIZE]
            lats = [p[0] for p in batch]
            lngs = [p[1] for p in batch]

            # Build the point list as comma-separated pairs
            points = "|".join(f"{round(lat, 6)},{round(lng, 6)}" for lat, lng in batch)

            params = {
                "demtype": "SRTM30",
                "south": min(lats),
                "north": max(lats),
                "west": min(lngs),
                "east": max(lngs),
                "outputFormat": "JSON",
                "API_Key": self._api_key,
                "locations": points,
            }

            resp = self.client.get(_OT_URL, params=params)
            resp.raise_for_status()
            data = resp.json()

            results = data.get("results") or data.get("data") or []
            if not results or len(results) != len(batch):
                raise ValueError(
                    f"OpenTopography returned {len(results)} results for {len(batch)} points"
                )

            for r in results:
                elev = r.get("elevation") if isinstance(r, dict) else r
                all_elevations.append(float(elev) if elev is not None else 0.0)

        return all_elevations

    def _fetch_open_elevation(self, latlngs: List[Tuple[float, float]]) -> List[float]:
        """Open-Elevation public API - no key required, batched POST with retry."""
        import time as _time

        all_elevations: List[float] = []

        for batch_start in range(0, len(latlngs), _OPEN_ELEV_BATCH_SIZE):
            batch = latlngs[batch_start : batch_start + _OPEN_ELEV_BATCH_SIZE]
            locations = [
                {"latitude": round(lat, 6), "longitude": round(lng, 6)}
                for lat, lng in batch
            ]

            # Retry up to 3 times with exponential backoff - the public
            # Open-Elevation API frequently returns 504s under load.
            last_err: Optional[Exception] = None
            for attempt in range(3):
                try:
                    resp = self.client.post(_OPEN_ELEV_URL, json={"locations": locations})
                    resp.raise_for_status()

                    data = resp.json()
                    results = data.get("results", [])

                    if len(results) != len(batch):
                        raise ValueError(
                            f"Open-Elevation returned {len(results)} results for {len(batch)} points"
                        )

                    for r in results:
                        elev = r.get("elevation")
                        all_elevations.append(float(elev) if elev is not None else 0.0)
                    last_err = None
                    break
                except Exception as e:
                    last_err = e
                    if attempt < 2:
                        wait = 1.5 * (2 ** attempt)  # 1.5s, 3s
                        logger.info("elevation: Open-Elevation attempt %d failed (%s), retrying in %.1fs", attempt + 1, e, wait)
                        _time.sleep(wait)

            if last_err is not None:
                raise last_err

        return all_elevations

    def close(self) -> None:
        self.client.close()


# ──────────────────────────────────────────────────────────────
# Grade segment computation (for fuel analysis)
# ──────────────────────────────────────────────────────────────

# Fuel penalty lookup by grade percentage
_GRADE_FUEL_FACTORS = [
    # (min_grade_pct, max_grade_pct, factor)
    (-100.0, -5.0, 0.85),    # steep downhill
    (-5.0,   -2.0, 0.90),    # moderate downhill
    (-2.0,    2.0, 1.00),    # flat
    ( 2.0,    5.0, 1.15),    # moderate uphill
    ( 5.0,  100.0, 1.35),    # steep uphill
]


def _fuel_factor_for_grade(grade_pct: float) -> float:
    for lo, hi, factor in _GRADE_FUEL_FACTORS:
        if lo <= grade_pct < hi:
            return factor
    return 1.0


def compute_grade_segments(
    profile: ElevationProfile,
    segment_length_km: float = 5.0,
) -> List[GradeSegment]:
    """
    Divide an elevation profile into fixed-length segments and compute
    average grade and fuel penalty for each.

    Used by the frontend to adjust fuel range calculations per-segment.
    """
    samples = profile.samples
    if len(samples) < 2:
        return []

    total_km = samples[-1].km_along
    segments: List[GradeSegment] = []
    seg_start_km = 0.0

    while seg_start_km < total_km:
        seg_end_km = min(seg_start_km + segment_length_km, total_km)

        # Find samples within this segment
        start_elev = _interp_elevation(samples, seg_start_km)
        end_elev = _interp_elevation(samples, seg_end_km)

        dist_km = seg_end_km - seg_start_km
        elev_change = end_elev - start_elev

        if dist_km > 0.01:
            # grade = rise / run (convert km to m for run to match elev in m)
            grade_pct = (elev_change / (dist_km * 1000.0)) * 100.0
        else:
            grade_pct = 0.0

        segments.append(
            GradeSegment(
                from_km=round(seg_start_km, 2),
                to_km=round(seg_end_km, 2),
                avg_grade_pct=round(grade_pct, 2),
                elevation_change_m=round(elev_change, 1),
                fuel_penalty_factor=_fuel_factor_for_grade(grade_pct),
            )
        )

        seg_start_km = seg_end_km

    return segments


def _interp_elevation(samples: List[ElevationSample], km: float) -> float:
    """Linearly interpolate elevation at a given km_along value."""
    if not samples:
        return 0.0
    if km <= samples[0].km_along:
        return samples[0].elevation_m
    if km >= samples[-1].km_along:
        return samples[-1].elevation_m

    for i in range(1, len(samples)):
        if samples[i].km_along >= km:
            prev = samples[i - 1]
            curr = samples[i]
            span = curr.km_along - prev.km_along
            if span < 1e-6:
                return curr.elevation_m
            frac = (km - prev.km_along) / span
            return prev.elevation_m + (curr.elevation_m - prev.elevation_m) * frac

    return samples[-1].elevation_m
