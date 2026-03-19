# app/core/geo.py
"""
Shared geospatial helpers used by overlay services.

Centralises polyline6 decoding, haversine distance, route sampling,
bounding-box calculation, min-distance-to-route, interpolated sampling,
corridor filtering, and bbox-overlap checking so each service doesn't
carry its own copy.
"""
from __future__ import annotations

import math
from typing import Any, Callable, Dict, List, Optional, Tuple

# Re-export from the canonical polyline6 module so callers only need one import.
from app.core.polyline6 import decode_polyline6, encode_polyline6  # noqa: F401

# ──────────────────────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────────────────────

EARTH_RADIUS_KM = 6_371.0


# ──────────────────────────────────────────────────────────────
# Distance
# ──────────────────────────────────────────────────────────────

def haversine_km(a: Tuple[float, float], b: Tuple[float, float]) -> float:
    """Great-circle distance in km between two (lat, lng) points."""
    lat1, lon1 = math.radians(a[0]), math.radians(a[1])
    lat2, lon2 = math.radians(b[0]), math.radians(b[1])
    dlat, dlon = lat2 - lat1, lon2 - lon1
    x = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2.0 * EARTH_RADIUS_KM * math.asin(min(1.0, math.sqrt(x)))


def haversine_m(a: Tuple[float, float], b: Tuple[float, float]) -> float:
    """Great-circle distance in metres between two (lat, lng) points."""
    return haversine_km(a, b) * 1000.0


def min_dist_to_route(
    lat: float,
    lng: float,
    samples: List[Tuple[float, float]],
) -> float:
    """Minimum haversine distance (km) from (lat, lng) to any sample point."""
    best = float("inf")
    pt = (lat, lng)
    for s in samples:
        d = haversine_km(pt, s)
        if d < best:
            best = d
        if d < 0.05:
            break
    return best


class RouteGrid:
    """
    Spatial grid index over route samples for O(1) nearest-sample lookups.

    Divides the route bounding box into ~0.1° cells (~11km), storing sample
    indices per cell.  To find the nearest sample to a query point, only
    samples in the 9 neighbouring cells are checked instead of all M samples.
    """

    __slots__ = ("_samples", "_grid", "_res", "_has_km")

    def __init__(
        self,
        samples: list,
        resolution: float = 0.1,
    ) -> None:
        self._samples = samples
        self._res = resolution
        self._has_km = len(samples) > 0 and len(samples[0]) >= 3
        grid: Dict[Tuple[int, int], List[int]] = {}
        for i, s in enumerate(samples):
            key = (int(s[0] / resolution), int(s[1] / resolution))
            grid.setdefault(key, []).append(i)
        self._grid = grid

    def nearest(self, lat: float, lng: float) -> Tuple[float, Optional[float]]:
        """Return (distance_km, km_along_or_None) for nearest sample."""
        r = self._res
        ci, cj = int(lat / r), int(lng / r)
        best_dist = float("inf")
        best_km: Optional[float] = None
        pt = (lat, lng)
        samples = self._samples
        has_km = self._has_km
        for di in (-1, 0, 1):
            for dj in (-1, 0, 1):
                bucket = self._grid.get((ci + di, cj + dj))
                if bucket is None:
                    continue
                for idx in bucket:
                    s = samples[idx]
                    d = haversine_km(pt, (s[0], s[1]))
                    if d < best_dist:
                        best_dist = d
                        best_km = s[2] if has_km else None
                    if d < 0.05:
                        return best_dist, best_km
        if best_dist == float("inf"):
            # Query point is far from any cell - fall back to brute force
            for i, s in enumerate(samples):
                d = haversine_km(pt, (s[0], s[1]))
                if d < best_dist:
                    best_dist = d
                    best_km = s[2] if has_km else None
        return best_dist, best_km

    def dist(self, lat: float, lng: float) -> float:
        """Return distance_km only (compat with min_dist_to_route)."""
        return self.nearest(lat, lng)[0]

    def dist_and_km(self, lat: float, lng: float) -> Tuple[float, float]:
        """Return (distance_km, km_along) - compat with min_dist_to_route_with_km."""
        d, km = self.nearest(lat, lng)
        return d, km or 0.0


# ──────────────────────────────────────────────────────────────
# Route sampling
# ──────────────────────────────────────────────────────────────

def sample_route(
    coords: List[Tuple[float, float]],
    interval_km: float = 5.0,
) -> List[Tuple[float, float]]:
    """Down-sample a coordinate list to roughly one point every *interval_km*."""
    if not coords:
        return []
    samples = [coords[0]]
    accum = 0.0
    for i in range(1, len(coords)):
        accum += haversine_km(coords[i - 1], coords[i])
        if accum >= interval_km:
            samples.append(coords[i])
            accum = 0.0
    if len(coords) > 1 and samples[-1] != coords[-1]:
        samples.append(coords[-1])
    return samples


def sample_route_with_km(
    coords: List[Tuple[float, float]],
    interval_km: float = 5.0,
) -> List[Tuple[float, float, float]]:
    """
    Down-sample coords returning (lat, lng, km_along) tuples.

    Used by services that need cumulative distance along the route
    (e.g. coverage, air_quality, rest_areas).
    """
    if not coords:
        return []
    samples: List[Tuple[float, float, float]] = [(coords[0][0], coords[0][1], 0.0)]
    total_km = 0.0
    accum = 0.0
    for i in range(1, len(coords)):
        d = haversine_km(coords[i - 1], coords[i])
        total_km += d
        accum += d
        if accum >= interval_km:
            samples.append((coords[i][0], coords[i][1], round(total_km, 2)))
            accum = 0.0
    if len(coords) > 1 and (samples[-1][0], samples[-1][1]) != coords[-1]:
        samples.append((coords[-1][0], coords[-1][1], round(total_km, 2)))
    return samples


# ──────────────────────────────────────────────────────────────
# Bounding box
# ──────────────────────────────────────────────────────────────

def bbox_from_coords(
    coords: List[Tuple[float, float]],
    buffer_km: float,
) -> Tuple[float, float, float, float]:
    """
    Compute (min_lat, min_lng, max_lat, max_lng) bounding box from coords
    with a buffer in kilometres.
    """
    lats = [c[0] for c in coords]
    lngs = [c[1] for c in coords]
    buf_lat = buffer_km / 111.32
    center_lat = (min(lats) + max(lats)) / 2.0
    cos_v = max(0.2, math.cos(math.radians(center_lat)))
    buf_lng = buffer_km / (111.32 * cos_v)
    return (
        min(lats) - buf_lat,
        min(lngs) - buf_lng,
        max(lats) + buf_lat,
        max(lngs) + buf_lng,
    )


def bbox_overlaps(
    min_lat: float, min_lng: float, max_lat: float, max_lng: float,
    region_lat_min: float, region_lat_max: float,
    region_lng_min: float, region_lng_max: float,
) -> bool:
    """Return True if two axis-aligned bounding boxes overlap."""
    return (
        min_lat <= region_lat_max
        and max_lat >= region_lat_min
        and min_lng <= region_lng_max
        and max_lng >= region_lng_min
    )


# ──────────────────────────────────────────────────────────────
# Interpolated route sampling (used by air_quality, coverage, wildlife)
# ──────────────────────────────────────────────────────────────

def cumulative_distances(coords: List[Tuple[float, float]]) -> List[float]:
    """Compute cumulative haversine distances along a coordinate list."""
    dists = [0.0]
    for i in range(1, len(coords)):
        d = haversine_km(
            (coords[i - 1][0], coords[i - 1][1]),
            (coords[i][0], coords[i][1]),
        )
        dists.append(dists[-1] + d)
    return dists


def interpolated_samples(
    coords: List[Tuple[float, float]],
    cum_dists: List[float],
    interval_km: float,
) -> List[Tuple[float, float, float]]:
    """
    Sample every *interval_km* with linear interpolation between vertices.

    Always includes the start and end points.
    Returns [(lat, lng, km_along), ...].
    """
    total_km = cum_dists[-1]
    if total_km == 0 or not coords:
        return []

    samples: List[Tuple[float, float, float]] = [(coords[0][0], coords[0][1], 0.0)]
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
        samples.append((lat, lng, target_km))
        target_km += interval_km

    last_lat, last_lng = coords[-1]
    if not samples or haversine_km((samples[-1][0], samples[-1][1]), (last_lat, last_lng)) > 0.5:
        samples.append((last_lat, last_lng, total_km))

    return samples


# ──────────────────────────────────────────────────────────────
# min_dist_to_route with km_along (used by rest_areas)
# ──────────────────────────────────────────────────────────────

def min_dist_to_route_with_km(
    lat: float,
    lng: float,
    samples: List[Tuple[float, float, float]],
) -> Tuple[float, float]:
    """
    Minimum haversine distance (km) from (lat, lng) to any sample point,
    also returning the km_along of that nearest sample.

    *samples* must be (lat, lng, km_along) tuples.
    Returns (distance_km, km_along).
    """
    best_dist = float("inf")
    best_km = 0.0
    pt = (lat, lng)
    for s_lat, s_lng, s_km in samples:
        d = haversine_km(pt, (s_lat, s_lng))
        if d < best_dist:
            best_dist = d
            best_km = s_km
        if d < 0.05:
            break
    return best_dist, best_km


# ──────────────────────────────────────────────────────────────
# Corridor filtering (filter items by distance from route)
# ──────────────────────────────────────────────────────────────

def filter_by_corridor(
    items: List[Any],
    route_samples: List[Tuple[float, float]],
    buffer_km: float,
    *,
    lat_fn: Callable[[Any], float] = lambda x: x.lat,
    lng_fn: Callable[[Any], float] = lambda x: x.lng,
    set_distance: Callable[[Any, float], None] | None = None,
) -> List[Any]:
    """
    Filter a list of items to those within *buffer_km* of the route.

    Args:
        items:          Objects to filter.
        route_samples:  Sampled route as [(lat, lng), ...].
        buffer_km:      Maximum distance from route.
        lat_fn/lng_fn:  Accessors for lat/lng on each item.
        set_distance:   Optional callback to stamp distance_from_route_km.

    Returns:
        Filtered list (order preserved).
    """
    filtered: List[Any] = []
    for item in items:
        dist = min_dist_to_route(lat_fn(item), lng_fn(item), route_samples)
        if dist <= buffer_km:
            if set_distance is not None:
                set_distance(item, round(dist, 2))
            filtered.append(item)
    return filtered
