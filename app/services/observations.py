# app/services/observations.py
#
# Crowd-sourced road observations from roamers.
# Observations are aggregated (clustered) when multiple users
# report the same type of issue near the same location.

from __future__ import annotations

import math
import sqlite3
import uuid
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

from app.core.contracts import (
    AggregatedObservation,
    UserObservation,
)
from app.core.storage import put_observation, get_nearby_observations
from app.core.time import utc_now_iso


# Default TTL per observation type (hours)
_DEFAULT_TTL: Dict[str, int] = {
    "road_condition": 72,     # 3 days
    "road_closure": 168,      # 7 days
    "hazard": 24,             # 1 day
    "fuel_price": 48,         # 2 days
    "speed_trap": 8,          # 8 hours
    "weather": 12,            # 12 hours
    "campsite": 168,          # 7 days
    "general": 48,            # 2 days
}

# Cluster radius in km - observations within this distance are merged
_CLUSTER_RADIUS_KM = 1.0

_R_KM = 6371.0


def _haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    rlat1, rlat2 = math.radians(lat1), math.radians(lat2)
    dlat = math.radians(lat2 - lat1)
    dlng = math.radians(lng2 - lng1)
    a = math.sin(dlat / 2) ** 2 + math.cos(rlat1) * math.cos(rlat2) * math.sin(dlng / 2) ** 2
    return 2 * _R_KM * math.asin(math.sqrt(a))


def _compute_expires_at(obs_type: str) -> str:
    ttl_hours = _DEFAULT_TTL.get(obs_type, 48)
    return (datetime.now(timezone.utc) + timedelta(hours=ttl_hours)).isoformat()


def _highest_severity(severities: List[str]) -> str:
    rank = {"info": 0, "caution": 1, "warning": 2, "danger": 3}
    best = max(severities, key=lambda s: rank.get(s, 0))
    return best


# Typical report count thresholds per type for full confidence.
# e.g., 3 independent reporters saying "road closed" = 1.0 confidence.
_CONFIDENCE_THRESHOLD: Dict[str, int] = {
    "road_closure": 2,
    "hazard": 2,
    "road_condition": 3,
    "fuel_price": 1,
    "speed_trap": 2,
    "weather": 3,
    "campsite": 1,
    "general": 3,
}


def _compute_confidence(obs_type: str, reporters: int, age_hours: float) -> float:
    """
    Confidence score 0.0-1.0 based on:
    - Reporter count vs threshold (more reporters = higher base confidence)
    - Temporal decay (older reports lose confidence toward 0)
    """
    threshold = _CONFIDENCE_THRESHOLD.get(obs_type, 3)
    # Base confidence from reporter count (capped at 1.0)
    base = min(reporters / max(threshold, 1), 1.0)

    # Temporal decay: full confidence up to 25% of TTL, then linear decay to 0
    ttl_hours = _DEFAULT_TTL.get(obs_type, 48)
    fresh_window = ttl_hours * 0.25
    if age_hours <= fresh_window:
        decay = 1.0
    elif age_hours >= ttl_hours:
        decay = 0.0
    else:
        decay = 1.0 - (age_hours - fresh_window) / (ttl_hours - fresh_window)

    return round(min(base * decay, 1.0), 2)


class Observations:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def submit(
        self,
        *,
        user_id: str,
        type: str,
        severity: str,
        lat: float,
        lng: float,
        heading_deg: Optional[float],
        message: Optional[str],
        value: Optional[str],
    ) -> UserObservation:
        """Submit a new observation. Returns the created record."""
        obs_id = str(uuid.uuid4())
        now = utc_now_iso()
        expires = _compute_expires_at(type)

        put_observation(
            self.conn,
            id=obs_id,
            user_id=user_id,
            type=type,
            severity=severity,
            lat=lat, lng=lng,
            heading_deg=heading_deg,
            message=message,
            value=value,
            created_at=now,
            expires_at=expires,
        )

        return UserObservation(
            id=obs_id,
            user_id=user_id,
            type=type,
            severity=severity,
            lat=lat, lng=lng,
            heading_deg=heading_deg,
            message=message,
            value=value,
            created_at=now,
            expires_at=expires,
        )

    def nearby(
        self,
        *,
        lat: float,
        lng: float,
        radius_km: float = 50.0,
        types: Optional[List[str]] = None,
        since_iso: Optional[str] = None,
    ) -> List[AggregatedObservation]:
        """
        Query nearby observations, aggregated by spatial clusters.
        Multiple reports of the same type within 1km are merged.
        """
        rows = get_nearby_observations(
            self.conn,
            lat=lat, lng=lng,
            radius_buckets=max(1, int(radius_km / 55)),  # 0.5° bucket ≈ 55km
            since_iso=since_iso,
            types=types,
        )

        # Filter by haversine distance
        nearby = [r for r in rows if _haversine_km(lat, lng, r["lat"], r["lng"]) <= radius_km]

        # Cluster by type + proximity
        return self._cluster(nearby)

    def _cluster(self, observations: List[dict]) -> List[AggregatedObservation]:
        """Group observations of the same type within _CLUSTER_RADIUS_KM."""
        # Group by type first
        by_type: Dict[str, List[dict]] = defaultdict(list)
        for obs in observations:
            by_type[obs["type"]].append(obs)

        results: list[AggregatedObservation] = []

        for obs_type, type_obs in by_type.items():
            clusters: list[list[dict]] = []

            for obs in type_obs:
                added = False
                for cluster in clusters:
                    # Check distance to cluster centroid (first item)
                    if _haversine_km(
                        obs["lat"], obs["lng"],
                        cluster[0]["lat"], cluster[0]["lng"],
                    ) <= _CLUSTER_RADIUS_KM:
                        cluster.append(obs)
                        added = True
                        break
                if not added:
                    clusters.append([obs])

            for cluster in clusters:
                # Compute aggregate
                lats = [o["lat"] for o in cluster]
                lngs = [o["lng"] for o in cluster]
                severities = [o["severity"] for o in cluster]
                user_ids = set(o["user_id"] for o in cluster)
                created_times = sorted(o["created_at"] for o in cluster)

                # Use the most recent message/value
                latest = max(cluster, key=lambda o: o["created_at"])
                num_reporters = len(user_ids)

                # Compute age from most recent report
                try:
                    last_dt = datetime.fromisoformat(
                        created_times[-1].replace("Z", "+00:00")
                    )
                    if last_dt.tzinfo is None:
                        last_dt = last_dt.replace(tzinfo=timezone.utc)
                    age_hours = (datetime.now(timezone.utc) - last_dt).total_seconds() / 3600
                except Exception:
                    age_hours = 24.0  # fallback

                confidence = _compute_confidence(obs_type, num_reporters, age_hours)
                is_recent = age_hours < 0.5  # within 30 minutes

                results.append(AggregatedObservation(
                    type=obs_type,
                    severity=_highest_severity(severities),
                    lat=round(sum(lats) / len(lats), 6),
                    lng=round(sum(lngs) / len(lngs), 6),
                    message=latest.get("message"),
                    value=latest.get("value"),
                    report_count=len(cluster),
                    first_reported_at=created_times[0],
                    last_reported_at=created_times[-1],
                    reporters=num_reporters,
                    confidence=confidence,
                    is_recent=is_recent,
                ))

        results.sort(key=lambda r: r.last_reported_at, reverse=True)
        return results
