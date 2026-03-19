# app/services/peer_sync.py
#
# Builds a delta payload for peer-to-peer overlay data exchange.
# When two roamers meet (online or via BLE), one can request
# overlay updates the other has that are newer than their own cache.

from __future__ import annotations

import sqlite3
from typing import Dict, List, Optional

import orjson

from app.core.contracts import (
    AggregatedObservation,
    FuelStation,
    HazardEvent,
    PeerSyncDelta,
    TrafficEvent,
)
from app.core.storage import get_nearby_observations
from app.core.time import utc_now_iso


class PeerSync:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def build_delta(
        self,
        *,
        lat: float,
        lng: float,
        radius_km: float = 200.0,
        overlay_timestamps: Dict[str, str],
    ) -> PeerSyncDelta:
        """
        Build a delta of overlay data newer than the caller's timestamps.
        Includes: user observations, traffic events, hazard events, fuel updates.
        """
        now = utc_now_iso()

        # 1. User observations (always included - these are the core peer value)
        obs_since = overlay_timestamps.get("observations")
        obs_rows = get_nearby_observations(
            self.conn,
            lat=lat, lng=lng,
            radius_buckets=max(1, int(radius_km / 55)),
            since_iso=obs_since,
        )
        aggregated_obs = self._aggregate_observations(obs_rows)

        # 2. Traffic events from cached packs
        traffic_since = overlay_timestamps.get("traffic")
        traffic_events = self._get_cached_traffic(lat, lng, radius_km, traffic_since)

        # 3. Hazard events from cached packs
        hazards_since = overlay_timestamps.get("hazards")
        hazard_events = self._get_cached_hazards(lat, lng, radius_km, hazards_since)

        # 4. Fuel updates from cached packs
        fuel_since = overlay_timestamps.get("fuel")
        fuel_updates = self._get_cached_fuel(lat, lng, radius_km, fuel_since)

        return PeerSyncDelta(
            observations=aggregated_obs,
            traffic_events=traffic_events,
            hazard_events=hazard_events,
            fuel_updates=fuel_updates,
            generated_at=now,
        )

    def _aggregate_observations(self, rows: List[dict]) -> List[AggregatedObservation]:
        """Simple aggregation - group by type+proximity would be ideal but
        for peer sync we send individual observations as single-report aggregates."""
        results = []
        for r in rows:
            results.append(AggregatedObservation(
                type=r["type"],
                severity=r["severity"],
                lat=r["lat"],
                lng=r["lng"],
                message=r.get("message"),
                value=r.get("value"),
                report_count=1,
                first_reported_at=r["created_at"],
                last_reported_at=r["created_at"],
                reporters=1,
            ))
        return results

    def _get_cached_traffic(
        self, lat: float, lng: float, radius_km: float,
        since_iso: Optional[str],
    ) -> List[TrafficEvent]:
        """Pull traffic events from the most recent cached traffic pack that covers this area."""
        rows = self.conn.execute(
            "SELECT pack_json, created_at FROM traffic_packs ORDER BY created_at DESC LIMIT 5;"
        ).fetchall()

        events: list[TrafficEvent] = []
        for row in rows:
            if since_iso and row[1] <= since_iso:
                continue
            try:
                pack = orjson.loads(row[0])
                for item in pack.get("items", []):
                    events.append(TrafficEvent(**item))
            except Exception:
                continue
        return events[:100]  # cap at 100

    def _get_cached_hazards(
        self, lat: float, lng: float, radius_km: float,
        since_iso: Optional[str],
    ) -> List[HazardEvent]:
        """Pull hazard events from recent cached hazard packs."""
        rows = self.conn.execute(
            "SELECT pack_json, created_at FROM hazard_packs ORDER BY created_at DESC LIMIT 5;"
        ).fetchall()

        events: list[HazardEvent] = []
        for row in rows:
            if since_iso and row[1] <= since_iso:
                continue
            try:
                pack = orjson.loads(row[0])
                for item in pack.get("items", []):
                    events.append(HazardEvent(**item))
            except Exception:
                continue
        return events[:100]

    def _get_cached_fuel(
        self, lat: float, lng: float, radius_km: float,
        since_iso: Optional[str],
    ) -> List[FuelStation]:
        """Pull fuel station updates from recent cached fuel packs."""
        rows = self.conn.execute(
            "SELECT pack_json, created_at FROM fuel_packs ORDER BY created_at DESC LIMIT 5;"
        ).fetchall()

        stations: list[FuelStation] = []
        for row in rows:
            if since_iso and row[1] <= since_iso:
                continue
            try:
                pack = orjson.loads(row[0])
                for item in pack.get("stations", []):
                    stations.append(FuelStation(**item))
            except Exception:
                continue
        return stations[:200]
