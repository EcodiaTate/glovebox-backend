"""
coverage.py
───────────
Mobile coverage overlay service for Roam.

Estimates cellular signal quality at points along a route using the
OpenCelliD bulk cell tower dataset (MCC 505 = Australia, CC BY-SA 4.0).

This is an ESTIMATE based on cell tower locations and nominal range values.
Actual coverage depends on terrain, vegetation, atmospheric conditions, and
device capability. Always download offline maps before entering remote areas.

Signal classification per point per carrier:
  reliable_4g  - LTE tower within its nominal range
  voice_only   - GSM/UMTS tower within range but no LTE nearby
  weak         - Any tower exists but beyond its nominal range (up to 30 km)
  no_coverage  - No tower within 30 km

Algorithm:
  1. Sample every 5 km along the route (coverage changes rapidly in the outback).
  2. For each sample, bucket the lat/lon to a 0.1° grid.
  3. Query cell_towers for all towers in the 3×3 surrounding buckets (~33 km radius).
  4. For each carrier, find the nearest tower and classify.
  5. Walk the per-carrier point list to find contiguous no-coverage / voice-only gaps.
  6. Emit gap warnings for any carrier gap > settings.coverage_no_signal_gap_km.
"""
from __future__ import annotations

import logging
import sqlite3
from typing import Any, Dict, List, Optional, Tuple

from app.core.contracts import CoverageGap, CoverageOverlay, CoveragePoint, CoverageLevel
from app.core.polyline6 import decode_polyline6
from app.core.settings import settings
from app.core.storage import (
    get_coverage_pack,
    put_coverage_pack,
    query_cell_towers_in_buckets,
)
from app.core.time import utc_now_iso
from app.core.geo import haversine_km, cumulative_distances, interpolated_samples
from app.core.cache_utils import is_fresh, stable_key

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────────────────────

_BUCKET_SIZE = 0.1          # degrees per tile (~11 km)
_SEARCH_RADIUS_KM = 30.0    # hard cap: beyond this = no_coverage
_SEARCH_BUCKETS = 3         # query ±3 buckets in each axis (±0.3° ≈ 33 km)

# Radio-type priority order (higher index = better)
_RADIO_RANK: Dict[str, int] = {
    "GSM":  1,
    "UMTS": 2,
    "LTE":  3,
    "NR":   4,   # 5G
}

# Canonical carriers surfaced to clients
_CARRIERS = ("Telstra", "Optus", "Vodafone")

# Score for ranking signal levels (used to pick best_carrier)
_LEVEL_SCORE: Dict[str, int] = {
    "reliable_4g": 3,
    "voice_only":  2,
    "weak":        1,
    "no_coverage": 0,
}


def _bucket(coord: float) -> int:
    return int(coord / _BUCKET_SIZE)


# ──────────────────────────────────────────────────────────────
# Cache key
# ──────────────────────────────────────────────────────────────


# ──────────────────────────────────────────────────────────────
# Tower lookup helpers
# ──────────────────────────────────────────────────────────────

def _classify_tower(dist_km: float, range_m: int, radio: str) -> CoverageLevel:
    """
    Classify coverage quality given a tower at dist_km away.

    reliable_4g  - LTE/NR tower within its nominal range
    voice_only   - GSM/UMTS within range, or LTE/NR just outside (likely fringe 4G)
    weak         - any tower within SEARCH_RADIUS_KM but beyond nominal range
    """
    range_km = range_m / 1000.0 if range_m > 0 else 5.0   # default 5 km if range unknown
    within_range = dist_km <= range_km
    radio_upper = radio.upper()
    is_4g = radio_upper in ("LTE", "NR")

    if within_range and is_4g:
        return "reliable_4g"
    if within_range:
        return "voice_only"
    # Beyond nominal range but within our 30 km hard cap
    if dist_km <= _SEARCH_RADIUS_KM:
        return "weak"
    return "no_coverage"


def _best_coverage_for_carrier(
    sample_lat: float,
    sample_lng: float,
    towers: List[Dict[str, Any]],
    carrier_name: str,
) -> CoverageLevel:
    """
    Among all towers for `carrier_name`, find the one giving the best coverage level.
    Uses a 2-pass approach: first find the nearest LTE/NR tower, then the nearest any tower.
    """
    best_level: CoverageLevel = "no_coverage"
    best_score = -1

    for t in towers:
        if t["carrier_name"] != carrier_name:
            continue
        dist = haversine_km((sample_lat, sample_lng), (t["lat"], t["lon"]))
        if dist > _SEARCH_RADIUS_KM:
            continue
        level = _classify_tower(dist, t["range_m"], t["radio"])
        score = _LEVEL_SCORE[level]
        if score > best_score:
            best_score = score
            best_level = level

    return best_level


# ──────────────────────────────────────────────────────────────
# Gap detection
# ──────────────────────────────────────────────────────────────

def _find_gaps(
    points: List[CoveragePoint],
    carrier_attr: str,      # "telstra", "optus", "vodafone"
    carrier_label: str,     # "Telstra", "Optus", "Vodafone"
    gap_threshold_km: float,
) -> List[CoverageGap]:
    """Walk the point list and find contiguous no-coverage segments > threshold."""
    gaps: List[CoverageGap] = []
    in_gap = False
    gap_start_km = 0.0

    for pt in points:
        level: CoverageLevel = getattr(pt, carrier_attr)
        is_bad = level in ("no_coverage", "voice_only")

        if is_bad and not in_gap:
            in_gap = True
            gap_start_km = pt.km_along
        elif not is_bad and in_gap:
            gap_km = pt.km_along - gap_start_km
            if gap_km >= gap_threshold_km:
                gaps.append(CoverageGap(
                    km_from=round(gap_start_km, 1),
                    km_to=round(pt.km_along, 1),
                    gap_km=round(gap_km, 1),
                    carrier=carrier_label,
                    message=(
                        f"Limited {carrier_label} coverage for ~{gap_km:.0f} km "
                        f"between km {gap_start_km:.0f} and km {pt.km_along:.0f}."
                    ),
                ))
            in_gap = False

    # Close any open gap at end of route
    if in_gap and points:
        gap_km = points[-1].km_along - gap_start_km
        if gap_km >= gap_threshold_km:
            gaps.append(CoverageGap(
                km_from=round(gap_start_km, 1),
                km_to=round(points[-1].km_along, 1),
                gap_km=round(gap_km, 1),
                carrier=carrier_label,
                message=(
                    f"Limited {carrier_label} coverage for ~{gap_km:.0f} km "
                    f"between km {gap_start_km:.0f} and km {points[-1].km_along:.0f}."
                ),
            ))

    return gaps


def _find_all_carrier_gaps(
    points: List[CoveragePoint],
    gap_threshold_km: float,
) -> List[CoverageGap]:
    """Find contiguous segments where ALL three carriers have no/voice coverage."""
    gaps: List[CoverageGap] = []
    in_gap = False
    gap_start_km = 0.0

    for pt in points:
        all_bad = all(
            getattr(pt, attr) in ("no_coverage", "voice_only")
            for attr in ("telstra", "optus", "vodafone")
        )
        if all_bad and not in_gap:
            in_gap = True
            gap_start_km = pt.km_along
        elif not all_bad and in_gap:
            gap_km = pt.km_along - gap_start_km
            if gap_km >= gap_threshold_km:
                gaps.append(CoverageGap(
                    km_from=round(gap_start_km, 1),
                    km_to=round(pt.km_along, 1),
                    gap_km=round(gap_km, 1),
                    carrier="all",
                    message=(
                        f"No mobile coverage for ~{gap_km:.0f} km between "
                        f"km {gap_start_km:.0f} and km {pt.km_along:.0f}. "
                        "Download offline maps before this section."
                    ),
                ))
            in_gap = False

    if in_gap and points:
        gap_km = points[-1].km_along - gap_start_km
        if gap_km >= gap_threshold_km:
            gaps.append(CoverageGap(
                km_from=round(gap_start_km, 1),
                km_to=round(points[-1].km_along, 1),
                gap_km=round(gap_km, 1),
                carrier="all",
                message=(
                    f"No mobile coverage for ~{gap_km:.0f} km between "
                    f"km {gap_start_km:.0f} and km {points[-1].km_along:.0f}. "
                    "Download offline maps before this section."
                ),
            ))

    return gaps


# ──────────────────────────────────────────────────────────────
# Coverage service class
# ──────────────────────────────────────────────────────────────

class Coverage:
    def __init__(self, *, conn: sqlite3.Connection):
        self.conn = conn

    def _ensure_towers_loaded(self) -> List[str]:
        """
        Trigger a cell tower import if the data is missing or stale.
        Returns a list of warning strings (empty on success).
        """
        from app.scripts.import_celltowers import needs_import, run_import

        warnings: List[str] = []
        if not settings.coverage_enabled:
            return warnings

        try:
            if needs_import(self.conn):
                local_path = settings.opencellid_local_db_path
                if not local_path or not __import__("pathlib").Path(local_path).exists():
                    warnings.append(
                        "Local cell tower CSV not found - coverage data unavailable. "
                        "Run the download_celltowers script to fetch the OpenCelliD data."
                    )
                    return warnings
                logger.info("[coverage] Cell tower data is missing or stale - importing from local file …")
                count = run_import(
                    self.conn,
                    local_path=local_path,
                )
                logger.info("[coverage] Import complete: %d towers loaded", count)
        except Exception as exc:
            msg = f"Cell tower import failed: {exc}"
            logger.warning("[coverage] %s", msg)
            warnings.append(msg)

        return warnings

    async def along_route(
        self,
        *,
        polyline6: str,
        sample_interval_km: float = 5.0,
        carriers: Optional[List[str]] = None,
    ) -> CoverageOverlay:
        """
        Estimate mobile coverage at regular intervals along the route.

        Args:
            polyline6:          Polyline6-encoded route geometry.
            sample_interval_km: Sample spacing in km (default 5 km).
            carriers:           Restrict output to these carriers (default: all three).

        Returns:
            CoverageOverlay with per-point classification, gap list, and summary stats.
        """
        algo_version = settings.coverage_algo_version
        max_age = settings.coverage_cache_seconds
        active_carriers = [c for c in _CARRIERS if (carriers is None or c in carriers)]

        coverage_key = stable_key(
            "coverage",
            {
                "polyline6": polyline6,
                "interval_km": sample_interval_km,
                "carriers": sorted(active_carriers),
                "algo_version": algo_version,
            },
        )

        # Cache hit - only serve from cache when the tower table has data, so a
        # previously-cached all-no-coverage result (generated before the token was
        # configured) isn't served indefinitely.
        towers_present = self.conn.execute("SELECT COUNT(*) FROM cell_towers").fetchone()[0] > 0
        if towers_present:
            cached = get_coverage_pack(self.conn, coverage_key)
            if cached:
                try:
                    pack = CoverageOverlay.model_validate(cached)
                    if is_fresh(pack.created_at, max_age_s=max_age):
                        return pack
                except Exception:
                    pass

        warnings: List[str] = []

        # ── Ensure cell tower data is available ──────────────────
        import_warnings = self._ensure_towers_loaded()
        warnings.extend(import_warnings)

        # If the import step added warnings, re-check whether the tower table now
        # has data (a successful import would have filled it).  If still empty we
        # can't produce meaningful coverage - return an overlay with no points so
        # the UI treats this as data-unavailable rather than "whole route has no
        # signal".
        if import_warnings:
            has_data = self.conn.execute("SELECT COUNT(*) FROM cell_towers").fetchone()[0] > 0
            if not has_data:
                overlay = CoverageOverlay(
                    coverage_key=coverage_key,
                    polyline6=polyline6,
                    algo_version=algo_version,
                    created_at=utc_now_iso(),
                    warnings=warnings,
                )
                _persist(self.conn, overlay, algo_version)
                return overlay

        # ── Decode polyline ──────────────────────────────────────
        coords = decode_polyline6(polyline6)
        if not coords:
            overlay = CoverageOverlay(
                coverage_key=coverage_key,
                polyline6=polyline6,
                algo_version=algo_version,
                created_at=utc_now_iso(),
                warnings=["Empty route polyline."],
            )
            _persist(self.conn, overlay, algo_version)
            return overlay

        cum_dists = cumulative_distances(coords)
        samples = interpolated_samples(coords, cum_dists, sample_interval_km)

        if not samples:
            overlay = CoverageOverlay(
                coverage_key=coverage_key,
                polyline6=polyline6,
                algo_version=algo_version,
                created_at=utc_now_iso(),
                warnings=["No sample points generated."],
            )
            _persist(self.conn, overlay, algo_version)
            return overlay

        # ── Add estimate disclaimer ───────────────────────────────
        warnings.append(
            "Coverage data is an estimate based on cell tower locations (OpenCelliD CC BY-SA 4.0). "
            "Actual coverage varies with terrain, buildings, and atmospheric conditions."
        )

        # ── Build per-point coverage ─────────────────────────────
        points: List[CoveragePoint] = []

        for lat, lng, km_along in samples:
            lat_b = _bucket(lat)
            lng_b = _bucket(lng)

            # Query the ±SEARCH_BUCKETS tile neighbourhood
            lat_buckets = list(range(lat_b - _SEARCH_BUCKETS, lat_b + _SEARCH_BUCKETS + 1))
            lon_buckets = list(range(lng_b - _SEARCH_BUCKETS, lng_b + _SEARCH_BUCKETS + 1))

            towers = query_cell_towers_in_buckets(self.conn, lat_buckets, lon_buckets)

            telstra = _best_coverage_for_carrier(lat, lng, towers, "Telstra")
            optus   = _best_coverage_for_carrier(lat, lng, towers, "Optus")
            vodafone = _best_coverage_for_carrier(lat, lng, towers, "Vodafone")

            # Best overall signal at this point
            scored = [
                ("Telstra",  telstra),
                ("Optus",    optus),
                ("Vodafone", vodafone),
            ]
            best_carrier_at_pt, best_signal_at_pt = max(
                scored, key=lambda x: _LEVEL_SCORE[x[1]]
            )
            if _LEVEL_SCORE[best_signal_at_pt] == 0:
                best_carrier_at_pt = None  # type: ignore[assignment]

            points.append(CoveragePoint(
                lat=round(lat, 6),
                lng=round(lng, 6),
                km_along=round(km_along, 2),
                telstra=telstra,
                optus=optus,
                vodafone=vodafone,
                best_carrier=best_carrier_at_pt,
                best_signal=best_signal_at_pt,
            ))

        # ── Gap detection ────────────────────────────────────────
        gap_threshold = settings.coverage_no_signal_gap_km
        gaps: List[CoverageGap] = []

        # All-carrier gaps first (most important warning)
        gaps.extend(_find_all_carrier_gaps(points, gap_threshold))

        # Per-carrier gaps (only if not already captured in all-carrier gaps)
        for carrier_attr, carrier_label in [
            ("telstra", "Telstra"),
            ("optus", "Optus"),
            ("vodafone", "Vodafone"),
        ]:
            carrier_gaps = _find_gaps(points, carrier_attr, carrier_label, gap_threshold)
            # Only include per-carrier gaps not already covered by an all-carrier gap
            all_carrier_ranges = {(g.km_from, g.km_to) for g in gaps if g.carrier == "all"}
            for g in carrier_gaps:
                if (g.km_from, g.km_to) not in all_carrier_ranges:
                    gaps.append(g)

        # ── Carrier scores (% of route with 4G) ─────────────────
        carrier_scores: Dict[str, float] = {}
        if points:
            n = len(points)
            for attr, label in [("telstra", "Telstra"), ("optus", "Optus"), ("vodafone", "Vodafone")]:
                pct = sum(1 for p in points if getattr(p, attr) == "reliable_4g") / n * 100.0
                carrier_scores[label] = round(pct, 1)

        best_carrier_overall: Optional[str] = None
        if carrier_scores:
            best_carrier_overall = max(carrier_scores, key=lambda c: carrier_scores[c])

        # ── Emit warnings for significant all-carrier gaps ───────
        for g in gaps:
            if g.carrier == "all" and g.gap_km >= gap_threshold:
                warnings.append(g.message)

        overlay = CoverageOverlay(
            coverage_key=coverage_key,
            polyline6=polyline6,
            algo_version=algo_version,
            created_at=utc_now_iso(),
            points=points,
            gaps=gaps,
            best_carrier_overall=best_carrier_overall,
            carrier_scores=carrier_scores,
            warnings=warnings,
        )
        _persist(self.conn, overlay, algo_version)
        return overlay


def _persist(conn: sqlite3.Connection, overlay: CoverageOverlay, algo_version: str) -> None:
    put_coverage_pack(
        conn,
        coverage_key=overlay.coverage_key,
        created_at=overlay.created_at,
        algo_version=algo_version,
        pack=overlay.model_dump(),
    )
