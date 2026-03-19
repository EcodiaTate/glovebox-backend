# app/services/traffic.py
"""
Multi-state Australian traffic overlay service.

Supports:
  - QLD: Official QLD Traffic v2 events API + delta merge + GeoJSON feed fallback
  - NSW: TfNSW Live Traffic Hazards GeoJSON API (7 feed types)
  - VIC: VicRoads Data Exchange (unplanned/planned/closures) + VicEmergency ArcGIS (crashes, flood, fire)
  - SA:  SA GeoHub ArcGIS traffic events (trafficdata.geohub.sa.gov.au)
  - WA:  Main Roads WA ArcGIS road incidents (CC-BY 4.0) + WebEoc real-time incidents/roadworks/closures/conditions
  - NT:  NT Road Report obstructions + road conditions (roadreport.nt.gov.au)

Source selection is automatic based on the query bbox - a Brisbane→Sydney route
will query QLD + NSW feeds; a Melbourne→Adelaide route queries VIC + SA.
A Perth→Broome route queries WA; an Alice Springs→Darwin route queries NT.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

import asyncio
import hashlib
import json
import os
import re
import time
from datetime import datetime, timezone

import httpx

from app.core.contracts import BBox4, TrafficEvent, TrafficOverlay
from app.core.geo_registry import states_for_bbox
from app.core.settings import settings
from app.core.storage import get_traffic_pack, put_traffic_pack
from app.core.time import utc_now_iso
from app.core.cache_utils import is_fresh, stable_key


# ══════════════════════════════════════════════════════════════
# Shared helpers
# ══════════════════════════════════════════════════════════════


def _stable_id(parts: List[str]) -> str:
    h = hashlib.sha256()
    for p in parts:
        h.update((p or "").encode("utf-8"))
        h.update(b"\x1f")
    return h.hexdigest()[:24]


def _bbox_intersects(a: List[float], b: BBox4) -> bool:
    return not (a[2] < b.minLng or a[0] > b.maxLng or a[3] < b.minLat or a[1] > b.maxLat)


def _bbox_from_geom(geom: Optional[Dict[str, Any]]) -> Optional[List[float]]:
    if not geom:
        return None
    coords: List[List[float]] = []

    def walk(x: Any) -> None:
        if isinstance(x, list):
            if len(x) == 2 and all(isinstance(v, (int, float)) for v in x):
                coords.append([float(x[0]), float(x[1])])
            else:
                for v in x:
                    walk(v)

    walk(geom.get("coordinates"))
    if not coords:
        return None
    xs = [c[0] for c in coords]
    ys = [c[1] for c in coords]
    return [min(xs), min(ys), max(xs), max(ys)]


def _parse_iso_to_epoch(s: Optional[str]) -> Optional[float]:
    if not s:
        return None
    try:
        t = str(s).strip()
        if not t:
            return None
        if t.endswith("Z"):
            t = t[:-1] + "+00:00"
        dt = datetime.fromisoformat(t)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:
        return None


def _event_is_expired(end_str: Optional[str]) -> bool:
    """Return True if the event has an end time that is in the past."""
    if not end_str:
        return False
    ts = _parse_iso_to_epoch(end_str)
    if ts is None:
        return False
    return time.time() > ts


def _event_is_too_old(props: Dict[str, Any], include_past_hours: int) -> bool:
    """Drop events that ended more than N hours ago (0 disables)."""
    if include_past_hours <= 0:
        return False
    endish = (
        props.get("end") or props.get("end_time") or props.get("endTime")
        or props.get("expires") or props.get("expiry")
        or props.get("to") or props.get("valid_to")
    )
    ts = _parse_iso_to_epoch(str(endish)) if endish else None
    if ts is None:
        return False
    return (time.time() - ts) > float(include_past_hours * 3600)


def _env(name: str) -> Optional[str]:
    v = os.getenv(name)
    return v.strip() if isinstance(v, str) and v.strip() else None


def _append_query_params(url: str, params: Dict[str, str]) -> str:
    if not params:
        return url
    qs = "&".join(f"{k}={v}" for k, v in params.items())
    return url + ("&" if "?" in url else "?") + qs


# ══════════════════════════════════════════════════════════════
# Classification - structured fields first, text fallback
# ══════════════════════════════════════════════════════════════

# Maps structured event_type / mainCategory values to (TrafficType, TrafficSeverity).
# Keys are lowercase. These are checked BEFORE the text-based fallback.
_STRUCTURED_TYPE_MAP: Dict[str, Tuple[str, str]] = {
    # QLD Traffic v2 structured fields
    "road closure":     ("closure", "major"),
    "road closed":      ("closure", "major"),
    "closure":          ("closure", "major"),
    "roadworks":        ("roadworks", "moderate"),
    "roadwork":         ("roadworks", "moderate"),
    "road work":        ("roadworks", "moderate"),
    "flooding":         ("flooding", "major"),
    "flood":            ("flooding", "major"),
    "congestion":       ("congestion", "minor"),
    "crash":            ("incident", "moderate"),
    "incident":         ("incident", "moderate"),
    "collision":        ("incident", "moderate"),
    "breakdown":        ("incident", "minor"),
    "hazard":           ("hazard", "info"),
    "special event":    ("hazard", "info"),
    # NSW Live Traffic mainCategory values
    "alpine conditions": ("hazard", "info"),
    "fire":             ("hazard", "major"),
    "major event":      ("hazard", "moderate"),
    # VIC Data Exchange event types
    "road closure - emergency": ("closure", "major"),
    "road closure - planned":   ("closure", "moderate"),
    "unplanned":        ("incident", "moderate"),
    "planned":          ("roadworks", "minor"),
    # WA Main Roads ArcGIS IncidentType values
    "bushfire":         ("hazard", "major"),
    "debris/trees/lost loads": ("hazard", "moderate"),
    "detour":           ("closure", "moderate"),
    "pothole/road surface damage": ("hazard", "minor"),
    "break down/tow away": ("incident", "minor"),
    # NT Road Report obstructionType values
    "water over road":  ("flooding", "major"),
    "wandering stock":  ("hazard", "moderate"),
    "changing surface conditions": ("hazard", "info"),
    "maximum gvm 4.5 tonne": ("hazard", "info"),
}

# Expanded text-based keyword classification (Australian terminology)
_TEXT_PATTERNS: List[Tuple[List[str], str, str]] = [
    # Closures - diverse Australian phrasing
    (["road closed", "closure", "closed", "shut", "impassable", "blocked",
      "no access", "cut off", "road closure"], "closure", "major"),
    # Flooding
    (["flood", "flooding", "floodwater", "inundated", "water over road",
      "water across", "submerged"], "flooding", "major"),
    # Roadworks
    (["roadworks", "works", "road work", "maintenance", "resurfacing",
      "line marking", "bridge work", "construction zone"], "roadworks", "moderate"),
    # Congestion
    (["congestion", "heavy traffic", "delays", "slow traffic",
      "queuing traffic"], "congestion", "minor"),
    # Incidents
    (["crash", "incident", "collision", "accident", "rollover", "jackknife",
      "truck rollover", "vehicle fire", "mvp", "multi-vehicle"], "incident", "moderate"),
    # Fire-related road impacts
    (["bushfire", "grass fire", "fire", "smoke", "reduced visibility due to fire"],
     "hazard", "major"),
]


def _classify(headline: str, desc: str, structured_type: Optional[str] = None) -> Tuple[str, str]:
    """
    Classify a traffic event into (type, severity).

    Strategy: check structured type field first, then fall back to text matching.
    """
    # 1) Structured field (from source API)
    if structured_type:
        key = structured_type.strip().lower()
        if key in _STRUCTURED_TYPE_MAP:
            return _STRUCTURED_TYPE_MAP[key]
        # Partial match - check if any structured key is a substring
        for skey, val in _STRUCTURED_TYPE_MAP.items():
            if skey in key or key in skey:
                return val

    # 2) Text-based keyword matching (expanded patterns)
    hay = f"{headline} {desc}".lower()
    for keywords, typ, sev in _TEXT_PATTERNS:
        for kw in keywords:
            if kw in hay:
                return typ, sev

    return "hazard", "info"


# ══════════════════════════════════════════════════════════════
# QLD Traffic - v2 events API with delta merge
# ══════════════════════════════════════════════════════════════

class _QldTrafficCache:
    """In-process merge cache for official QLD Traffic events endpoint."""

    def __init__(self) -> None:
        self.full_at: float = 0.0
        self.delta_at: float = 0.0
        self.features_by_id: Dict[str, Dict[str, Any]] = {}

    def is_full_stale(self, full_refresh_s: int) -> bool:
        if not self.features_by_id:
            return True
        return (time.time() - self.full_at) > float(max(1, full_refresh_s))

    def can_use_cached(self, ttl_s: int) -> bool:
        if not self.features_by_id:
            return False
        return (time.time() - max(self.full_at, self.delta_at)) <= float(max(1, ttl_s))


_QCACHE = _QldTrafficCache()


class _QldTrafficProvider:
    """Handles QLD Traffic v2 events API + GeoJSON feed fallback."""

    def _feature_source_id(self, feature: Dict[str, Any]) -> str:
        props = feature.get("properties") or {}
        if not isinstance(props, dict):
            props = {}
        fid = feature.get("id") or props.get("id") or props.get("event_id") or props.get("eventId")
        return str(fid).strip() if fid is not None else ""

    def _feature_id_for_cache(self, feature: Dict[str, Any], *, source: str) -> str:
        sid = self._feature_source_id(feature)
        if sid:
            return _stable_id([source, sid])
        geom = feature.get("geometry") or {}
        props = feature.get("properties") or {}
        if not isinstance(props, dict):
            props = {}
        return _stable_id([
            source,
            str(geom.get("type")),
            json.dumps(geom.get("coordinates"))[:240],
            json.dumps(props, sort_keys=True, separators=(",", ":"))[:240],
        ])

    def _status_allows(self, feature: Dict[str, Any]) -> bool:
        props = feature.get("properties") or {}
        status = str(props.get("status") or "").strip().lower() if isinstance(props, dict) else ""
        if not status:
            return True
        return status in ("published", "reopened")

    async def _fetch_json(self, client: httpx.AsyncClient, url: str) -> Dict[str, Any]:
        r = await client.get(url, headers={"User-Agent": "roam/traffic"})
        r.raise_for_status()
        data = r.json()
        return data if isinstance(data, dict) else {}

    async def _qld_fetch_full(self, *, client: httpx.AsyncClient, url: str) -> None:
        data = await self._fetch_json(client, url)
        feats = data.get("features") or []
        by_id: Dict[str, Dict[str, Any]] = {}
        for f in feats:
            if not isinstance(f, dict):
                continue
            if not self._status_allows(f):
                continue
            fid = self._feature_id_for_cache(f, source="qldtraffic")
            by_id[fid] = f
        _QCACHE.features_by_id = by_id
        _QCACHE.full_at = time.time()

    async def _qld_fetch_delta_merge(self, *, client: httpx.AsyncClient, url: str) -> None:
        data = await self._fetch_json(client, url)
        feats = data.get("features") or []
        for f in feats:
            if not isinstance(f, dict):
                continue
            fid = self._feature_id_for_cache(f, source="qldtraffic")
            if not self._status_allows(f):
                _QCACHE.features_by_id.pop(fid, None)
            else:
                _QCACHE.features_by_id[fid] = f
        _QCACHE.delta_at = time.time()

    def _feature_to_event(self, feature: Dict[str, Any], *, feed: str) -> Optional[TrafficEvent]:
        if not isinstance(feature, dict):
            return None

        props = feature.get("properties") or {}
        if not isinstance(props, dict):
            props = {}

        include_past_hours = int(getattr(settings, "traffic_include_past_hours", 0) or 0)
        if _event_is_too_old(props, include_past_hours):
            return None

        geom = feature.get("geometry") or None
        bb = _bbox_from_geom(geom) if isinstance(geom, dict) else None

        headline = str(
            props.get("headline") or props.get("title")
            or props.get("event_type") or props.get("type")
            or props.get("description") or f"{feed} event"
        ).strip()

        desc = str(props.get("description") or props.get("information") or props.get("advice") or "").strip()
        url2 = props.get("url") or props.get("link") or None
        last_updated = props.get("last_updated") or props.get("lastUpdated") or props.get("updated") or None

        # Extract structured type for smarter classification
        structured_type = props.get("event_type") or props.get("type") or None
        typ, sev = _classify(headline, desc, structured_type=str(structured_type) if structured_type else None)

        # Extract start/end times
        start_at = props.get("start") or props.get("start_time") or props.get("startTime") or props.get("from") or None
        end_at = (
            props.get("end") or props.get("end_time") or props.get("endTime")
            or props.get("expires") or props.get("expiry") or props.get("to") or None
        )

        # Prune expired events
        if _event_is_expired(str(end_at) if end_at else None):
            return None

        # Stable event identity
        sid = self._feature_source_id(feature)
        if sid:
            ev_id = _stable_id(["qldtraffic", feed, sid])
        else:
            ev_id = _stable_id([
                "qldtraffic", feed, headline[:160],
                json.dumps(geom, sort_keys=True, separators=(",", ":"))[:600] if isinstance(geom, dict) else "",
            ])

        return TrafficEvent(
            id=ev_id,
            source="qldtraffic",
            feed=feed,
            type=typ,       # type: ignore
            severity=sev,   # type: ignore
            headline=headline or f"{feed} event",
            description=(desc or None),
            url=(str(url2) if url2 else None),
            last_updated=(str(last_updated) if last_updated else None),
            start_at=(str(start_at) if start_at else None),
            end_at=(str(end_at) if end_at else None),
            geometry=(geom if isinstance(geom, dict) else None),
            bbox=bb,
            region="qld",
            raw=props,
        )

    async def poll(
        self,
        *,
        client: httpx.AsyncClient,
        bbox: BBox4,
        warnings: List[str],
    ) -> List[TrafficEvent]:
        """Query QLD Traffic v2 events + optional GeoJSON feeds."""
        items: List[TrafficEvent] = []

        events_url = settings.qldtraffic_events_url or None
        delta_url = settings.qldtraffic_events_delta_url or None
        api_key = settings.qldtraffic_api_key or None
        if isinstance(api_key, str) and not api_key.strip():
            api_key = None

        v2_ok = False

        # Preferred: official v2 events (+ delta merge)
        if events_url:
            try:
                url_full = events_url
                if api_key:
                    url_full = _append_query_params(url_full, {"apikey": api_key})

                full_refresh_s = max(1, int(settings.qldtraffic_full_refresh_seconds or 900))
                ttl_s = max(1, int(settings.qldtraffic_cache_seconds or 60))

                if _QCACHE.is_full_stale(full_refresh_s):
                    await self._qld_fetch_full(client=client, url=url_full)
                else:
                    if delta_url and not _QCACHE.can_use_cached(ttl_s):
                        url_delta = delta_url
                        if api_key:
                            url_delta = _append_query_params(url_delta, {"apikey": api_key})
                        await self._qld_fetch_delta_merge(client=client, url=url_delta)

                for f in _QCACHE.features_by_id.values():
                    if not isinstance(f, dict):
                        continue
                    ev = self._feature_to_event(f, feed="events")
                    if not ev:
                        continue
                    if ev.bbox and not _bbox_intersects(ev.bbox, bbox):
                        continue
                    items.append(ev)

                v2_ok = True
            except Exception as e:
                warnings.append(f"traffic:qld_v2 failed: {e}")

        # Fallback: per-feed GeoJSON
        if not v2_ok:
            feeds: List[Tuple[str, str]] = []
            m = [
                ("incidents", settings.qldtraffic_incidents_url),
                ("roadworks", settings.qldtraffic_roadworks_url),
                ("closures", settings.qldtraffic_closures_url),
                ("flooding", settings.qldtraffic_flooding_url),
            ]
            for name, url in m:
                if url:
                    feeds.append((name, str(url)))

            for feed_name, url in feeds:
                try:
                    r = await client.get(url, headers={"User-Agent": "roam/traffic"})
                    r.raise_for_status()
                    data = r.json()
                except Exception as e:
                    warnings.append(f"traffic:qld:{feed_name} fetch failed: {e}")
                    continue

                for f in (data.get("features") or []):
                    if not isinstance(f, dict):
                        continue
                    ev = self._feature_to_event(f, feed=feed_name)
                    if not ev:
                        continue
                    if ev.bbox and not _bbox_intersects(ev.bbox, bbox):
                        continue
                    items.append(ev)

        return items


# ══════════════════════════════════════════════════════════════
# NSW Traffic - TfNSW Live Traffic Hazards GeoJSON API
# ══════════════════════════════════════════════════════════════

class _NswTrafficProvider:
    """
    Fetches from api.transport.nsw.gov.au/v1/live/hazards/{type}
    Types: incidents, fires, floods, alpine, roadworks, majorevent, planned
    Auth: Authorization: apikey {key}
    Returns: GeoJSON FeatureCollection per type
    """

    def _parse_feature(self, feature: Dict[str, Any], *, feed_type: str) -> Optional[TrafficEvent]:
        if not isinstance(feature, dict):
            return None

        props = feature.get("properties") or {}
        if not isinstance(props, dict):
            props = {}

        # NSW uses "isEnded" to mark finished events
        if props.get("isEnded"):
            return None

        geom = feature.get("geometry") or None
        bb = _bbox_from_geom(geom) if isinstance(geom, dict) else None

        headline = str(props.get("headline") or props.get("displayName") or f"NSW {feed_type}").strip()
        desc = str(props.get("otherAdvice") or props.get("advisoryMessage") or "").strip()
        url2 = props.get("webLinkUrl") or None
        last_updated = props.get("lastUpdated") or props.get("created") or None
        start_at = props.get("start") or props.get("created") or None
        end_at = props.get("end") or None

        # Prune expired
        if _event_is_expired(str(end_at) if end_at else None):
            return None

        # Use mainCategory for structured classification, feed_type as fallback
        main_cat = props.get("mainCategory") or feed_type
        typ, sev = _classify(headline, desc, structured_type=str(main_cat))

        # NSW "isMajor" flag overrides severity up
        if props.get("isMajor"):
            sev = "major"

        # Feed-type overrides for clarity
        if feed_type == "fires":
            typ = "hazard"
            if sev not in ("major",):
                sev = "major"
        elif feed_type == "floods":
            typ = "flooding"
            if sev not in ("major",):
                sev = "major"

        # Stable ID: prefer upstream id
        upstream_id = str(feature.get("id") or props.get("id") or "").strip()
        if upstream_id:
            ev_id = _stable_id(["nsw_traffic", feed_type, upstream_id])
        else:
            ev_id = _stable_id(["nsw_traffic", feed_type, headline[:160], str(bb or "")])

        return TrafficEvent(
            id=ev_id,
            source="nsw_traffic",
            feed=feed_type,
            type=typ,       # type: ignore
            severity=sev,   # type: ignore
            headline=headline,
            description=(desc or None),
            url=(str(url2) if url2 else None),
            last_updated=(str(last_updated) if last_updated else None),
            start_at=(str(start_at) if start_at else None),
            end_at=(str(end_at) if end_at else None),
            geometry=(geom if isinstance(geom, dict) else None),
            bbox=bb,
            region="nsw",
            raw=props,
        )

    async def poll(
        self,
        *,
        client: httpx.AsyncClient,
        bbox: BBox4,
        warnings: List[str],
    ) -> List[TrafficEvent]:
        if not settings.nsw_traffic_enabled:
            return []

        api_key = settings.nsw_traffic_api_key
        if not api_key:
            api_key = _env("NSW_TRAFFIC_API_KEY") or ""
        if not api_key.strip():
            warnings.append("traffic:nsw skipped - no API key (set NSW_TRAFFIC_API_KEY)")
            return []

        base_url = settings.nsw_traffic_base_url.rstrip("/")
        feed_types = [f.strip() for f in settings.nsw_traffic_feeds.split(",") if f.strip()]
        headers = {
            "Authorization": f"apikey {api_key}",
            "User-Agent": "roam/traffic",
            "Accept": "application/json",
        }

        items: List[TrafficEvent] = []

        for feed_type in feed_types:
            url = f"{base_url}/{feed_type}"
            try:
                r = await client.get(url, headers=headers)
                r.raise_for_status()
                data = r.json()
            except Exception as e:
                warnings.append(f"traffic:nsw:{feed_type} failed: {e}")
                continue

            features = data.get("features") or []
            for f in features:
                ev = self._parse_feature(f, feed_type=feed_type)
                if not ev:
                    continue
                if ev.bbox and not _bbox_intersects(ev.bbox, bbox):
                    continue
                items.append(ev)

        return items


# ══════════════════════════════════════════════════════════════
# VIC Traffic - VicRoads Data Exchange API
# ══════════════════════════════════════════════════════════════

class _VicTrafficProvider:
    """
    Fetches from data-exchange.vicroads.vic.gov.au
    - Unplanned Disruptions v2
    - Planned Disruptions v1
    - Emergency Road Closures v1
    Auth: KeyID header
    Returns: JSON (not GeoJSON) with records array
    """

    def _parse_record(self, record: Dict[str, Any], *, feed: str) -> Optional[TrafficEvent]:
        if not isinstance(record, dict):
            return None

        # VIC records have parent-child structure. We want the parent.
        headline = str(record.get("headline") or record.get("description") or record.get("event_type") or f"VIC {feed}").strip()
        desc = str(record.get("advice") or record.get("information") or record.get("description") or "").strip()

        lat = record.get("latitude")
        lng = record.get("longitude")
        start_at = record.get("start_date") or record.get("created_date") or None
        end_at = record.get("end_date") or record.get("expected_end_date") or None

        # Prune expired
        if _event_is_expired(str(end_at) if end_at else None):
            return None

        # Build point geometry from lat/lng
        geom: Optional[Dict[str, Any]] = None
        bb: Optional[List[float]] = None
        if lat is not None and lng is not None:
            try:
                flat, flng = float(lat), float(lng)
                geom = {"type": "Point", "coordinates": [flng, flat]}
                bb = [flng, flat, flng, flat]
            except (ValueError, TypeError):
                pass

        # Also check for road geometry if present
        road_geom = record.get("geometry") or None
        if road_geom and isinstance(road_geom, dict) and road_geom.get("type"):
            geom = road_geom
            bb = _bbox_from_geom(geom)

        event_type = record.get("event_type") or record.get("disruption_type") or feed
        typ, sev = _classify(headline, desc, structured_type=str(event_type))

        # Emergency closures are always major
        if feed == "closures":
            typ = "closure"
            sev = "major"

        # Severity override from VIC data
        vic_severity = str(record.get("severity") or "").lower()
        if vic_severity in ("high", "critical"):
            sev = "major"
        elif vic_severity == "medium":
            sev = "moderate"

        upstream_id = str(record.get("id") or "").strip()
        if upstream_id:
            ev_id = _stable_id(["vic_traffic", feed, upstream_id])
        else:
            ev_id = _stable_id(["vic_traffic", feed, headline[:160], str(lat), str(lng)])

        return TrafficEvent(
            id=ev_id,
            source="vic_traffic",
            feed=feed,
            type=typ,       # type: ignore
            severity=sev,   # type: ignore
            headline=headline,
            description=(desc or None),
            url=None,
            last_updated=(str(record.get("last_updated") or record.get("modified_date") or start_at or "")),
            start_at=(str(start_at) if start_at else None),
            end_at=(str(end_at) if end_at else None),
            geometry=geom,
            bbox=bb,
            region="vic",
            raw=record,
        )

    async def _fetch_vic(
        self,
        client: httpx.AsyncClient,
        url: str,
        api_key: str,
    ) -> List[Dict[str, Any]]:
        """Fetch from VicRoads Data Exchange. Returns list of record dicts."""
        headers = {
            "KeyID": api_key,
            "User-Agent": "roam/traffic",
            "Accept": "application/json",
        }

        records: List[Dict[str, Any]] = []
        page = 1
        limit = 200

        # Paginated fetch (VicRoads uses page/limit params)
        while True:
            sep = "&" if "?" in url else "?"
            page_url = f"{url}{sep}page={page}&limit={limit}"
            r = await client.get(page_url, headers=headers)
            r.raise_for_status()
            data = r.json()

            # VicRoads returns either a list or {"value": [...]} or {"records": [...]}
            if isinstance(data, list):
                batch = data
            elif isinstance(data, dict):
                batch = data.get("value") or data.get("records") or data.get("features") or []
            else:
                break

            if not batch:
                break

            records.extend(batch)

            # Stop if we got less than a full page
            if len(batch) < limit:
                break
            page += 1
            # Safety: max 5 pages (1000 records)
            if page > 5:
                break

        return records

    async def poll(
        self,
        *,
        client: httpx.AsyncClient,
        bbox: BBox4,
        warnings: List[str],
    ) -> List[TrafficEvent]:
        if not settings.vic_traffic_enabled:
            return []

        api_key = settings.vic_traffic_api_key
        if not api_key:
            api_key = _env("VIC_TRAFFIC_API_KEY") or ""
        if not api_key.strip():
            warnings.append("traffic:vic skipped - no API key (set VIC_TRAFFIC_API_KEY)")
            return []

        items: List[TrafficEvent] = []
        feeds = [
            ("unplanned", settings.vic_traffic_unplanned_url),
            ("planned", settings.vic_traffic_planned_url),
            ("closures", settings.vic_traffic_closures_url),
        ]

        for feed_name, url in feeds:
            if not url:
                continue
            try:
                records = await self._fetch_vic(client, url, api_key)
            except Exception as e:
                warnings.append(f"traffic:vic:{feed_name} failed: {e}")
                continue

            for rec in records:
                ev = self._parse_record(rec, feed=feed_name)
                if not ev:
                    continue
                if ev.bbox and not _bbox_intersects(ev.bbox, bbox):
                    continue
                items.append(ev)

        return items


# ══════════════════════════════════════════════════════════════
# SA Traffic - SA GeoHub ArcGIS (trafficdata.geohub.sa.gov.au)
# ══════════════════════════════════════════════════════════════

_SA_HTML_TAG_RE = re.compile(r"<[^>]+>")
_SA_STRIP_WS_RE = re.compile(r"\s{2,}")


def _strip_html(html: str) -> str:
    """Strip HTML tags and normalise whitespace."""
    text = _SA_HTML_TAG_RE.sub(" ", html)
    return _SA_STRIP_WS_RE.sub(" ", text).strip()


def _arcgis_geom_to_geojson(ag_geom: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    Convert an ArcGIS geometry dict to a GeoJSON geometry.
    Handles rings (polygon) and paths (polyline).
    Coordinates are expected in WGS84 (outSR=4326).
    """
    if not isinstance(ag_geom, dict):
        return None
    rings = ag_geom.get("rings")
    if rings and isinstance(rings, list) and rings:
        # Polygon - rings is list of coordinate rings [[lng, lat], ...]
        return {"type": "Polygon", "coordinates": rings}
    paths = ag_geom.get("paths")
    if paths and isinstance(paths, list) and paths:
        if len(paths) == 1:
            return {"type": "LineString", "coordinates": paths[0]}
        return {"type": "MultiLineString", "coordinates": paths}
    x = ag_geom.get("x")
    y = ag_geom.get("y")
    if x is not None and y is not None:
        try:
            return {"type": "Point", "coordinates": [float(x), float(y)]}
        except (ValueError, TypeError):
            pass
    return None


class _SaTrafficProvider:
    """
    Fetches SA traffic events from the SA GeoHub ArcGIS endpoint.
    Replaces the dead data.sa.gov.au GeoJSON feed (404 as of Feb 2026).

    Endpoint: trafficdata.geohub.sa.gov.au/MapServer/5/query
    Key response fields: LOCATION_ID, PLOT_TYPE, PLOT_DETAILS (HTML),
      START_DATE (epoch ms), END_DATE (epoch ms), geometry (rings/paths).
    """

    def _parse_feature(self, feature: Dict[str, Any]) -> Optional[TrafficEvent]:
        if not isinstance(feature, dict):
            return None

        attrs = feature.get("attributes") or {}
        if not isinstance(attrs, dict):
            attrs = {}

        ag_geom = feature.get("geometry") or None
        geom = _arcgis_geom_to_geojson(ag_geom) if isinstance(ag_geom, dict) else None
        bb = _bbox_from_geom(geom) if geom else None

        plot_type = str(attrs.get("PLOT_TYPE") or "").strip()
        plot_details_raw = str(attrs.get("PLOT_DETAILS") or "").strip()
        plot_details = _strip_html(plot_details_raw) if plot_details_raw else ""
        location_id = str(attrs.get("LOCATION_ID") or "").strip()

        # Build headline: prefer PLOT_DETAILS text; fall back to PLOT_TYPE
        if plot_details:
            # First non-empty sentence/line is the headline
            first_line = plot_details.split(".")[0].strip()
            headline = first_line if len(first_line) >= 8 else plot_details[:160]
        elif plot_type:
            headline = f"SA traffic: {plot_type}"
        else:
            headline = "SA traffic event"

        desc = plot_details if plot_details and plot_details != headline else None

        # Expiry from END_DATE (epoch ms, None means ongoing)
        end_epoch_ms = attrs.get("END_DATE")
        end_at: Optional[str] = None
        if end_epoch_ms is not None:
            try:
                end_epoch = float(end_epoch_ms) / 1000.0
                if end_epoch > 0:
                    end_at = datetime.fromtimestamp(end_epoch, tz=timezone.utc).isoformat()
            except (ValueError, TypeError):
                pass
        if end_at and _event_is_expired(end_at):
            return None

        # Start date
        start_at: Optional[str] = None
        start_epoch_ms = attrs.get("START_DATE")
        if start_epoch_ms is not None:
            try:
                start_epoch = float(start_epoch_ms) / 1000.0
                if start_epoch > 0:
                    start_at = datetime.fromtimestamp(start_epoch, tz=timezone.utc).isoformat()
            except (ValueError, TypeError):
                pass

        typ, sev = _classify(headline, desc or "", structured_type=plot_type or None)

        if location_id:
            ev_id = _stable_id(["sa_geohub", location_id])
        else:
            ev_id = _stable_id(["sa_geohub", headline[:160], str(bb)])

        return TrafficEvent(
            id=ev_id,
            source="sa_traffic",
            feed="geohub",
            type=typ,       # type: ignore
            severity=sev,   # type: ignore
            headline=headline,
            description=desc,
            url="https://traffic.sa.gov.au/",
            last_updated=start_at,
            start_at=start_at,
            end_at=end_at,
            geometry=geom,
            bbox=bb,
            region="sa",
            raw=attrs,
        )

    async def poll(
        self,
        *,
        client: httpx.AsyncClient,
        bbox: BBox4,
        warnings: List[str],
    ) -> List[TrafficEvent]:
        if not settings.sa_traffic_enabled:
            return []

        base_url = settings.sa_traffic_geohub_url
        if not base_url:
            return []

        # Build time-bounded query - events active right now
        now_str = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        where = (
            f"1=1 and start_date <= to_date('{now_str}','YYYY-MM-DD HH24:MI:SS')"
            f" and (end_date >= to_date('{now_str}','YYYY-MM-DD HH24:MI:SS') or end_date is null)"
        )
        params = {
            "f": "json",
            "where": where,
            "returnGeometry": "true",
            "spatialRel": "esriSpatialRelIntersects",
            "outFields": "*",
            "outSR": "4326",
            "resultOffset": "0",
            "resultRecordCount": "2000",
        }

        items: List[TrafficEvent] = []
        try:
            r = await client.get(base_url, params=params, headers={"User-Agent": "roam/traffic"})
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            warnings.append(f"traffic:sa failed: {e}")
            return items

        for f in (data.get("features") or []):
            ev = self._parse_feature(f)
            if not ev:
                continue
            if ev.bbox and not _bbox_intersects(ev.bbox, bbox):
                continue
            items.append(ev)

        return items


# ══════════════════════════════════════════════════════════════
# WA Traffic - Main Roads WA ArcGIS road incidents (CC-BY 4.0)
# ══════════════════════════════════════════════════════════════

class _WaTrafficProvider:
    """
    Fetches road incidents from Main Roads WA via ArcGIS FeatureServer.

    Endpoint returns GeoJSON FeatureCollection with properties including:
      ClosureTyp, IncidentType, RoadName, Suburb, ClosureStartDate,
      ClosureEndDate, Comments

    IncidentType values observed:
      Break Down/Tow Away, Bushfire, Debris/Trees/Lost Loads, Detour,
      Flooding, Pothole/Road Surface Damage, Special Event

    License: CC-BY 4.0 (data.wa.gov.au)
    """

    # Map WA ClosureTyp to our traffic types
    _CLOSURE_TYPE_MAP: Dict[str, Tuple[str, str]] = {
        "full closure":        ("closure", "major"),
        "partial closure":     ("closure", "moderate"),
        "lane closure":        ("closure", "moderate"),
        "road closed":         ("closure", "major"),
        "detour":              ("closure", "moderate"),
        "temporary closure":   ("closure", "moderate"),
    }

    # Map WA IncidentType to our traffic types
    _INCIDENT_TYPE_MAP: Dict[str, Tuple[str, str]] = {
        "break down/tow away":          ("incident", "minor"),
        "bushfire":                      ("hazard", "major"),
        "debris/trees/lost loads":       ("hazard", "moderate"),
        "detour":                        ("closure", "moderate"),
        "flooding":                      ("flooding", "major"),
        "pothole/road surface damage":   ("hazard", "minor"),
        "special event":                 ("hazard", "info"),
    }

    def _parse_feature(self, feature: Dict[str, Any]) -> Optional[TrafficEvent]:
        if not isinstance(feature, dict):
            return None

        props = feature.get("properties") or {}
        if not isinstance(props, dict):
            props = {}

        geom = feature.get("geometry") or None
        bb = _bbox_from_geom(geom) if isinstance(geom, dict) else None

        # Core fields
        closure_type = str(props.get("ClosureTyp") or props.get("ClosureType") or "").strip()
        incident_type = str(props.get("IncidentType") or "").strip()
        road_name = str(props.get("RoadName") or props.get("Road") or "").strip()
        suburb = str(props.get("Suburb") or props.get("Location") or "").strip()
        comments = str(props.get("Comments") or props.get("Description") or "").strip()

        # Timestamps - ArcGIS epoch milliseconds or ISO strings
        start_raw = props.get("ClosureStartDate") or props.get("StartDate") or None
        end_raw = props.get("ClosureEndDate") or props.get("EndDate") or None

        start_at = self._parse_arcgis_date(start_raw)
        end_at = self._parse_arcgis_date(end_raw)

        # Prune expired
        if end_at and _event_is_expired(end_at):
            return None

        # Build headline
        parts: List[str] = []
        if incident_type:
            parts.append(incident_type)
        elif closure_type:
            parts.append(closure_type)
        if road_name:
            parts.append(road_name)
        if suburb:
            parts.append(suburb)
        headline = " - ".join(parts) if parts else "WA road incident"

        # Classify: try structured IncidentType first, then ClosureTyp, then text
        typ, sev = "hazard", "info"
        it_lower = incident_type.lower()
        ct_lower = closure_type.lower()

        if it_lower in self._INCIDENT_TYPE_MAP:
            typ, sev = self._INCIDENT_TYPE_MAP[it_lower]
        elif ct_lower in self._CLOSURE_TYPE_MAP:
            typ, sev = self._CLOSURE_TYPE_MAP[ct_lower]
        else:
            typ, sev = _classify(headline, comments, structured_type=incident_type or closure_type or None)

        # Stable ID from OBJECTID or fallback
        object_id = str(props.get("OBJECTID") or props.get("ObjectId") or props.get("FID") or "").strip()
        if object_id:
            ev_id = _stable_id(["wa_traffic", object_id])
        else:
            ev_id = _stable_id(["wa_traffic", headline[:160], str(bb or "")])

        return TrafficEvent(
            id=ev_id,
            source="wa_mainroads",
            feed="arcgis_incidents",
            type=typ,       # type: ignore
            severity=sev,   # type: ignore
            headline=headline,
            description=(comments or None),
            url=None,
            last_updated=start_at,
            start_at=start_at,
            end_at=end_at,
            geometry=(geom if isinstance(geom, dict) else None),
            bbox=bb,
            region="wa",
            raw=props,
        )

    @staticmethod
    def _parse_arcgis_date(val: Any) -> Optional[str]:
        """Parse ArcGIS date - either epoch millis (int) or ISO string."""
        if val is None:
            return None
        if isinstance(val, (int, float)) and val > 1_000_000_000:
            # Epoch milliseconds
            try:
                dt = datetime.fromtimestamp(val / 1000, tz=timezone.utc)
                return dt.isoformat()
            except Exception:
                return None
        s = str(val).strip()
        if not s:
            return None
        return s

    async def poll(
        self,
        *,
        client: httpx.AsyncClient,
        bbox: BBox4,
        warnings: List[str],
    ) -> List[TrafficEvent]:
        if not settings.wa_traffic_enabled:
            return []

        url = settings.wa_traffic_arcgis_url
        if not url:
            return []

        items: List[TrafficEvent] = []
        try:
            r = await client.get(url, headers={"User-Agent": "roam/traffic"})
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            warnings.append(f"traffic:wa failed: {e}")
            return items

        features = data.get("features") or []
        for f in features:
            ev = self._parse_feature(f)
            if not ev:
                continue
            if ev.bbox and not _bbox_intersects(ev.bbox, bbox):
                continue
            items.append(ev)

        return items


# ══════════════════════════════════════════════════════════════
# VIC Emergency Incidents - ArcGIS FeatureServer supplement
# ══════════════════════════════════════════════════════════════
# Complements the VicRoads Data Exchange feed by adding fire, flood,
# and vehicle incidents from VicEmergency (CFA/SES/VicPol joint feed).
# No auth required. CC-BY 4.0.
#
# Useful incident types for Roam:
#   VEHICLE - road crashes attended by emergency services
#   FLOOD   - flood events affecting roads
#   GRASS   - grass fires near roads

_VIC_EMERGENCY_URL = (
    "https://services1.arcgis.com/vHnIGBHHqDR6y0CR/arcgis/rest/services"
    "/Vic_Emergency_Incidents/FeatureServer/0/query"
)
# Filter to incident types relevant to road travel
_VIC_EMERGENCY_TYPES = ("VEHICLE", "FLOOD", "GRASS", "BUSHFIRE")
_VIC_EMERGENCY_WHERE = (
    "incidentType IN ('VEHICLE','FLOOD','GRASS','BUSHFIRE')"
    " AND incidentStatus<>'Closed'"
)


class _VicEmergencyProvider:
    """
    Fetches active emergency incidents from VicEmergency ArcGIS FeatureServer
    and converts them to TrafficEvent items.

    Covers vehicle crashes and flood/fire events that affect road travel.
    Works without any API key (public ArcGIS service).
    """

    def _parse_feature(self, feature: Dict[str, Any]) -> Optional[TrafficEvent]:
        if not isinstance(feature, dict):
            return None
        props = feature.get("properties") or {}
        if not isinstance(props, dict):
            props = {}

        incident_type = str(props.get("incidentType") or "").strip().upper()
        status = str(props.get("incidentStatus") or "").strip()
        location = str(props.get("incidentLocation") or "").strip()
        last_updated = str(props.get("lastUpdateDateTime") or "").strip() or None

        geom = feature.get("geometry") or None
        geojson_geom: Optional[Dict[str, Any]] = geom if isinstance(geom, dict) else None
        bb = _bbox_from_geom(geojson_geom) if geojson_geom else None

        # Derive point for dedup key
        lat: Optional[float] = None
        lng: Optional[float] = None
        if geojson_geom:
            raw_coords = geojson_geom.get("coordinates")
            gtype = geojson_geom.get("type")
            if gtype == "Point" and isinstance(raw_coords, list) and len(raw_coords) >= 2:
                try:
                    lng, lat = float(raw_coords[0]), float(raw_coords[1])
                except (TypeError, ValueError):
                    pass

        # Classify by incident type
        if incident_type == "VEHICLE":
            typ, sev = "incident", "major"
        elif incident_type in ("FLOOD",):
            typ, sev = "closure", "major"
        elif incident_type in ("GRASS", "BUSHFIRE"):
            typ, sev = "incident", "moderate"
        else:
            typ, sev = "incident", "minor"

        headline = f"{incident_type.title()} - {location}" if location else f"VIC {incident_type.title()} incident"
        description = f"Status: {status}" if status else None

        upstream_id = str(props.get("objectid") or props.get("OBJECTID") or "").strip()
        ev_id = _stable_id(["vic_emergency", incident_type, upstream_id or location[:80]])

        return TrafficEvent(
            id=ev_id,
            source="vic_emergency",
            feed=incident_type.lower(),
            type=typ,      # type: ignore
            severity=sev,  # type: ignore
            headline=headline,
            description=description,
            url=None,
            last_updated=last_updated,
            start_at=last_updated,
            end_at=None,
            geometry=geojson_geom,
            bbox=bb,
            region="vic",
            raw=props,
        )

    async def poll(
        self,
        *,
        client: httpx.AsyncClient,
        bbox: BBox4,
        warnings: List[str],
    ) -> List[TrafficEvent]:
        bbox_param = (
            f"&geometry={bbox.minLng},{bbox.minLat},{bbox.maxLng},{bbox.maxLat}"
            "&geometryType=esriGeometryEnvelope"
            "&spatialRel=esriSpatialRelIntersects"
            "&inSR=4326"
        )
        _where_enc = _VIC_EMERGENCY_WHERE.replace(" ", "%20").replace("'", "%27")
        url = (
            f"{_VIC_EMERGENCY_URL}"
            f"?where={_where_enc}"
            f"&outFields=incidentType,incidentLocation,incidentStatus,lastUpdateDateTime,objectid"
            f"&f=geojson"
            f"{bbox_param}"
        )
        try:
            r = await client.get(url, headers={"User-Agent": "roam/traffic"})
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            warnings.append(f"traffic:vic_emergency failed: {e}")
            return []

        items: List[TrafficEvent] = []
        for feat in (data.get("features") or []):
            ev = self._parse_feature(feat)
            if ev:
                items.append(ev)
        return items


class _VicCompositeProvider:
    """Combines VicRoads Data Exchange and VicEmergency ArcGIS feeds."""

    def __init__(self) -> None:
        self._data_exchange = _VicTrafficProvider()
        self._emergency = _VicEmergencyProvider()

    async def poll(
        self,
        *,
        client: httpx.AsyncClient,
        bbox: BBox4,
        warnings: List[str],
    ) -> List[TrafficEvent]:
        de_task = self._data_exchange.poll(client=client, bbox=bbox, warnings=warnings)
        em_task = self._emergency.poll(client=client, bbox=bbox, warnings=warnings)
        de_items, em_items = await asyncio.gather(de_task, em_task)
        return list(de_items) + list(em_items)


# ══════════════════════════════════════════════════════════════
# WA Incidents - WebEoc real-time road incidents / roadworks / closures
# ══════════════════════════════════════════════════════════════

_WA_INCIDENTS_BASE = (
    "https://services2.arcgis.com/cHGEnmsJ165IBJRM/arcgis/rest/services"
    "/WebEocFeatureLayerViewPRD/FeatureServer"
)
# Layer 1 – Road Incidents (points); publishExt='Yes' filter
_WA_LAYER1_URL = (
    f"{_WA_INCIDENTS_BASE}/1/query"
    "?where=publishExt%3D%27Yes%27&outFields=*&f=geojson"
)
# Layer 2 – Roadworks (points)
_WA_LAYER2_URL = f"{_WA_INCIDENTS_BASE}/2/query?where=1%3D1&outFields=*&f=geojson"
# Layer 4 – Road Closures (lines)
_WA_LAYER4_URL = f"{_WA_INCIDENTS_BASE}/4/query?where=1%3D1&outFields=*&f=geojson"
# Layer 11 – Roads Opened With Conditions (gravel/flood-damaged roads, outback)
_WA_LAYER11_URL = f"{_WA_INCIDENTS_BASE}/11/query?where=1%3D1&outFields=*&f=geojson"

_WA_INCIDENTS_TTL = 300  # 5 minutes – live feed


class _WaIncidentsProvider:
    """
    Real-time WA road incidents, roadworks, and closures from the
    Department of Transport WebEoc ArcGIS FeatureServer.

    Queries 4 layers concurrently:
      Layer 1  – Road Incidents (points, publishExt='Yes')
      Layer 2  – Roadworks (points)
      Layer 4  – Road Closures (lines)
      Layer 11 – Roads Opened With Conditions (outback gravel/flood-damaged roads)

    Key fields: IncidentType, ClosureType, TrafficCondition, TrafficImpact,
      Location, Road, EntryDate, OBJECTID
    """

    @staticmethod
    def _bbox_param(bbox: BBox4) -> str:
        return (
            f"&geometry={bbox.minLng},{bbox.minLat},{bbox.maxLng},{bbox.maxLat}"
            "&geometryType=esriGeometryEnvelope"
            "&spatialRel=esriSpatialRelIntersects"
            "&inSR=4326"
        )

    @staticmethod
    def _midpoint(coords: List[List[float]]) -> List[float]:
        """Return the midpoint of a list of [lng, lat] coordinate pairs."""
        if not coords:
            return [0.0, 0.0]
        lng = sum(c[0] for c in coords) / len(coords)
        lat = sum(c[1] for c in coords) / len(coords)
        return [lng, lat]

    def _parse_feature(
        self,
        feature: Dict[str, Any],
        *,
        layer: int,
    ) -> Optional[TrafficEvent]:
        if not isinstance(feature, dict):
            return None

        props = feature.get("properties") or {}
        if not isinstance(props, dict):
            props = {}

        geom = feature.get("geometry") or None
        geojson_geom: Optional[Dict[str, Any]] = geom if isinstance(geom, dict) else None

        # Derive point geometry for bbox / midpoint
        point_coords: Optional[List[float]] = None
        if isinstance(geojson_geom, dict):
            gtype = geojson_geom.get("type")
            raw_coords = geojson_geom.get("coordinates")
            if gtype == "Point" and isinstance(raw_coords, list) and len(raw_coords) >= 2:
                try:
                    point_coords = [float(raw_coords[0]), float(raw_coords[1])]
                except (ValueError, TypeError):
                    pass
            elif gtype == "LineString" and isinstance(raw_coords, list) and raw_coords:
                try:
                    pairs = [[float(c[0]), float(c[1])] for c in raw_coords if len(c) >= 2]
                    if pairs:
                        point_coords = self._midpoint(pairs)
                except (ValueError, TypeError, IndexError):
                    pass

        bb = _bbox_from_geom(geojson_geom) if geojson_geom else None

        # Key fields
        incident_type = str(props.get("IncidentType") or "").strip()
        closure_type = str(props.get("ClosureType") or "").strip()
        traffic_impact = str(props.get("TrafficImpact") or "").strip()
        road = str(props.get("Road") or "").strip()
        location = str(props.get("Location") or "").strip()
        entry_date = props.get("EntryDate") or None

        # Build description: "{Road}: {Location} - {IncidentType or ClosureType}"
        type_label = incident_type or closure_type or ""
        _road_loc = ": ".join(p for p in [road, location] if p)
        description = (
            f"{_road_loc} - {type_label}" if _road_loc and type_label
            else _road_loc or type_label or None
        )

        # Headline
        headline_parts: List[str] = []
        if type_label:
            headline_parts.append(type_label)
        if road:
            headline_parts.append(road)
        if location:
            headline_parts.append(location)
        headline = " - ".join(headline_parts) if headline_parts else "WA road incident"

        # --- Classify by mapping rules ---
        it_lower = incident_type.lower()
        ct_lower = closure_type.lower()
        ti_lower = traffic_impact.lower()

        typ: str
        sev: str

        if layer == 4 or "clos" in it_lower or "clos" in ct_lower:
            typ, sev = "closure", "major"
        elif layer == 11:
            # Roads Opened With Conditions - passable but damaged/restricted
            typ, sev = "hazard", "moderate"
        elif layer == 2 or "roadwork" in it_lower:
            typ, sev = "roadwork", "minor"
        elif "crash" in it_lower or "accident" in it_lower:
            typ, sev = "incident", "major"
        elif "flood" in it_lower or "water" in it_lower:
            typ, sev = "closure", "major"
        elif "fire" in it_lower:
            typ, sev = "incident", "major"
        else:
            typ, sev = "incident", "minor"

        # TrafficImpact "delay" → minor unless already major
        if "delay" in ti_lower and sev != "major":
            sev = "minor"

        # Stable ID: OBJECTID or hash of Road+Location+EntryDate
        object_id = props.get("OBJECTID")
        if object_id is not None:
            ev_id = _stable_id(["wa_incident", str(object_id)])
        else:
            ev_id = _stable_id([
                "wa_incident",
                road[:80],
                location[:80],
                str(entry_date or ""),
            ])

        # Parse entry date
        last_updated: Optional[str] = None
        if entry_date is not None:
            if isinstance(entry_date, (int, float)) and entry_date > 1_000_000_000:
                try:
                    dt = datetime.fromtimestamp(float(entry_date) / 1000, tz=timezone.utc)
                    last_updated = dt.isoformat()
                except Exception:
                    pass
            else:
                last_updated = str(entry_date).strip() or None

        return TrafficEvent(
            id=ev_id,
            source="wa_webeoc",
            feed=f"layer{layer}",
            type=typ,       # type: ignore
            severity=sev,   # type: ignore
            headline=headline,
            description=description,
            url=None,
            last_updated=last_updated,
            start_at=last_updated,
            end_at=None,
            geometry=geojson_geom,
            bbox=bb,
            region="wa",
            raw=props,
        )

    async def _fetch_layer(
        self,
        client: httpx.AsyncClient,
        base_url: str,
        layer: int,
        bbox: BBox4,
        warnings: List[str],
    ) -> List[TrafficEvent]:
        url = base_url + self._bbox_param(bbox)
        try:
            r = await client.get(url, headers={"User-Agent": "roam/traffic"})
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            warnings.append(f"traffic:wa_incidents:layer{layer} failed: {e}")
            return []

        items: List[TrafficEvent] = []
        for f in (data.get("features") or []):
            ev = self._parse_feature(f, layer=layer)
            if ev:
                items.append(ev)
        return items

    async def poll(
        self,
        *,
        client: httpx.AsyncClient,
        bbox: BBox4,
        warnings: List[str],
    ) -> List[TrafficEvent]:
        layer_tasks = [
            self._fetch_layer(client, _WA_LAYER1_URL, 1, bbox, warnings),
            self._fetch_layer(client, _WA_LAYER2_URL, 2, bbox, warnings),
            self._fetch_layer(client, _WA_LAYER4_URL, 4, bbox, warnings),
            self._fetch_layer(client, _WA_LAYER11_URL, 11, bbox, warnings),
        ]
        results = await asyncio.gather(*layer_tasks)
        items: List[TrafficEvent] = []
        for batch in results:
            items.extend(batch)
        return items


class _WaCompositeProvider:
    """Combines _WaTrafficProvider (Main Roads legacy) and _WaIncidentsProvider (WebEoc)."""

    def __init__(self) -> None:
        self._legacy = _WaTrafficProvider()
        self._incidents = _WaIncidentsProvider()

    async def poll(
        self,
        *,
        client: httpx.AsyncClient,
        bbox: BBox4,
        warnings: List[str],
    ) -> List[TrafficEvent]:
        legacy_task = self._legacy.poll(client=client, bbox=bbox, warnings=warnings)
        incidents_task = self._incidents.poll(client=client, bbox=bbox, warnings=warnings)
        legacy_items, incident_items = await asyncio.gather(legacy_task, incidents_task)
        return list(legacy_items) + list(incident_items)


# ══════════════════════════════════════════════════════════════
# NT Traffic - NT Road Report obstructions + road conditions
# ══════════════════════════════════════════════════════════════

class _NtTrafficProvider:
    """
    Fetches from roadreport.nt.gov.au/api/Obstruction/GetAll.

    Returns a JSON array (not wrapped) of obstruction objects with:
      roadName, obstructionType, restrictionType, startPoint, endPoint,
      comment, locationComment, dateFrom, dateTo, dateActive

    obstructionType values observed:
      Flooding, Water Over Road, Wandering Stock, Changing Surface Conditions,
      Maximum GVM 4.5 Tonne

    restrictionType values observed:
      Road Closed, Impassable, With Caution, Weight And Or Vehicle Type Restriction

    This also serves as the outback road conditions overlay  -
    covers Tanami Road, Larapinta Drive, Plenty Highway, Stuart Highway, etc.
    """

    # Map restrictionType to (type, severity)
    _RESTRICTION_MAP: Dict[str, Tuple[str, str]] = {
        "road closed":                              ("closure", "major"),
        "impassable":                               ("closure", "major"),
        "with caution":                             ("hazard", "moderate"),
        "weight and or vehicle type restriction":   ("hazard", "info"),
    }

    # Map obstructionType to (type, severity) as fallback
    _OBSTRUCTION_MAP: Dict[str, Tuple[str, str]] = {
        "flooding":                     ("flooding", "major"),
        "water over road":              ("flooding", "major"),
        "wandering stock":              ("hazard", "moderate"),
        "changing surface conditions":  ("hazard", "info"),
        "maximum gvm 4.5 tonne":        ("hazard", "info"),
    }

    def _parse_obstruction(self, item: Dict[str, Any]) -> Optional[TrafficEvent]:
        if not isinstance(item, dict):
            return None

        road_name = str(item.get("roadName") or "").strip()
        obstruction_type = str(item.get("obstructionType") or "").strip()
        restriction_type = str(item.get("restrictionType") or "").strip()
        comment = str(item.get("comment") or "").strip()
        location_comment = str(item.get("locationComment") or "").strip()
        date_from = item.get("dateFrom") or None
        date_to = item.get("dateTo") or None
        date_active = item.get("dateActive") or None

        # Prune expired
        end_str = str(date_to) if date_to else None
        if end_str and _event_is_expired(end_str):
            return None

        # Build geometry from startPoint → endPoint as LineString
        start_pt = item.get("startPoint") or {}
        end_pt = item.get("endPoint") or {}
        geom: Optional[Dict[str, Any]] = None
        bb: Optional[List[float]] = None

        start_lat = start_pt.get("latitude") if isinstance(start_pt, dict) else None
        start_lng = start_pt.get("longitude") if isinstance(start_pt, dict) else None
        end_lat = end_pt.get("latitude") if isinstance(end_pt, dict) else None
        end_lng = end_pt.get("longitude") if isinstance(end_pt, dict) else None

        coords: List[List[float]] = []
        if start_lat is not None and start_lng is not None:
            try:
                coords.append([float(start_lng), float(start_lat)])
            except (ValueError, TypeError):
                pass
        if end_lat is not None and end_lng is not None:
            try:
                coords.append([float(end_lng), float(end_lat)])
            except (ValueError, TypeError):
                pass

        if len(coords) == 2 and coords[0] != coords[1]:
            geom = {"type": "LineString", "coordinates": coords}
            lngs = [c[0] for c in coords]
            lats = [c[1] for c in coords]
            bb = [min(lngs), min(lats), max(lngs), max(lats)]
        elif len(coords) >= 1:
            geom = {"type": "Point", "coordinates": coords[0]}
            bb = [coords[0][0], coords[0][1], coords[0][0], coords[0][1]]

        # Build headline
        parts: List[str] = []
        if restriction_type:
            parts.append(restriction_type)
        elif obstruction_type:
            parts.append(obstruction_type)
        if road_name:
            parts.append(road_name)
        headline = " - ".join(parts) if parts else "NT road obstruction"

        # Build description
        desc_parts: List[str] = []
        if obstruction_type and obstruction_type != restriction_type:
            desc_parts.append(obstruction_type)
        if location_comment:
            desc_parts.append(location_comment)
        if comment:
            desc_parts.append(comment)
        desc = ". ".join(desc_parts) if desc_parts else None

        # Classify: restrictionType takes priority (more specific to road impact)
        typ, sev = "hazard", "info"
        rt_lower = restriction_type.lower()
        ot_lower = obstruction_type.lower()

        if rt_lower in self._RESTRICTION_MAP:
            typ, sev = self._RESTRICTION_MAP[rt_lower]
        elif ot_lower in self._OBSTRUCTION_MAP:
            typ, sev = self._OBSTRUCTION_MAP[ot_lower]
        else:
            typ, sev = _classify(headline, desc or "", structured_type=restriction_type or obstruction_type or None)

        # Stable ID from road name + type + location
        ev_id = _stable_id([
            "nt_roadreport",
            road_name[:80],
            obstruction_type[:40],
            restriction_type[:40],
            location_comment[:80],
        ])

        # Parse dates
        start_at: Optional[str] = None
        end_at: Optional[str] = None
        last_updated: Optional[str] = None
        if date_from:
            start_at = str(date_from)
        if date_to:
            end_at = str(date_to)
        if date_active:
            last_updated = str(date_active)
        elif date_from:
            last_updated = str(date_from)

        return TrafficEvent(
            id=ev_id,
            source="nt_roadreport",
            feed="obstructions",
            type=typ,       # type: ignore
            severity=sev,   # type: ignore
            headline=headline,
            description=desc,
            url="https://roadreport.nt.gov.au/",
            last_updated=last_updated,
            start_at=start_at,
            end_at=end_at,
            geometry=geom,
            bbox=bb,
            region="nt",
            raw=item,
        )

    async def poll(
        self,
        *,
        client: httpx.AsyncClient,
        bbox: BBox4,
        warnings: List[str],
    ) -> List[TrafficEvent]:
        if not settings.nt_traffic_enabled:
            return []

        url = settings.nt_road_report_url
        if not url:
            return []

        items: List[TrafficEvent] = []
        try:
            r = await client.get(url, headers={
                "User-Agent": "roam/traffic",
                "Accept": "application/json",
            })
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            warnings.append(f"traffic:nt failed: {e}")
            return items

        # Response is a bare JSON array
        obstructions: list = []
        if isinstance(data, list):
            obstructions = data
        elif isinstance(data, dict):
            obstructions = data.get("obstructions") or data.get("items") or data.get("results") or []

        for item in obstructions:
            ev = self._parse_obstruction(item)
            if not ev:
                continue
            if ev.bbox and not _bbox_intersects(ev.bbox, bbox):
                continue
            items.append(ev)

        return items


# ══════════════════════════════════════════════════════════════
# Main Traffic service - state-aware orchestrator
# ══════════════════════════════════════════════════════════════

# Provider singletons
_QLD = _QldTrafficProvider()
_NSW = _NswTrafficProvider()
_VIC = _VicCompositeProvider()
_SA = _SaTrafficProvider()
_WA = _WaCompositeProvider()
_NT = _NtTrafficProvider()

# Map state codes → provider instances
_STATE_PROVIDERS: Dict[str, Any] = {
    "qld": _QLD,
    # "nsw": _NSW,  # DISABLED: api.transport.nsw.gov.au returns 404 on all feeds (Mar 2026)
    "vic": _VIC,
    "sa":  _SA,
    "wa":  _WA,
    "nt":  _NT,
    # TAS - no traffic JSON API found. Hazards only via BOM + TheList ArcGIS.
}


class Traffic:
    def __init__(self, *, conn):
        self.conn = conn

    async def poll(
        self,
        *,
        bbox: BBox4,
        cache_seconds: int | None = None,
        timeout_s: float | None = None,
    ) -> TrafficOverlay:
        algo_version = settings.traffic_algo_version
        max_age = int(cache_seconds or settings.overlays_cache_seconds)
        timeout = float(timeout_s or settings.overlays_timeout_s)

        # Determine which states the bbox overlaps
        active_states = states_for_bbox(bbox)
        # Filter to states we actually have providers for
        query_states = [s for s in active_states if s in _STATE_PROVIDERS]
        # ACT is covered by NSW
        if "act" in active_states and "nsw" not in query_states:
            query_states.append("nsw")

        traffic_key = stable_key(
            "traffic",
            {
                "bbox": bbox.model_dump(),
                "algo_version": algo_version,
                "states": query_states,
            },
        )

        # SQLite cache hit
        cached = get_traffic_pack(self.conn, traffic_key)
        if cached:
            try:
                pack = TrafficOverlay.model_validate(cached)
                if is_fresh(pack.created_at, max_age_s=max_age):
                    return pack
            except Exception:
                pass

        warnings: List[str] = []
        items: List[TrafficEvent] = []

        if not query_states:
            # No states in bbox - probably offshore or outside Australia
            pack = TrafficOverlay(
                traffic_key=traffic_key,
                bbox=bbox,
                provider="no_states",
                algo_version=algo_version,
                created_at=utc_now_iso(),
                items=[],
                warnings=["No Australian states overlap this bbox."],
            )
            put_traffic_pack(
                self.conn,
                traffic_key=traffic_key,
                created_at=pack.created_at,
                algo_version=algo_version,
                pack=pack.model_dump(),
            )
            return pack

        transport = httpx.AsyncHTTPTransport(retries=1)
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True, transport=transport) as client:
            async def _poll_state(state: str) -> List[TrafficEvent]:
                provider = _STATE_PROVIDERS.get(state)
                if not provider:
                    return []
                try:
                    return await provider.poll(client=client, bbox=bbox, warnings=warnings)
                except Exception as e:
                    warnings.append(f"traffic:{state} failed: {e}")
                    return []

            results = await asyncio.gather(*[_poll_state(s) for s in query_states])
            for state_items in results:
                items.extend(state_items)

        # Dedup by stable ID
        dedup: Dict[str, TrafficEvent] = {}
        for it in items:
            dedup[it.id] = it

        provider_str = "+".join(f"{s}" for s in query_states)
        if not dedup:
            provider_str += ":empty"

        pack = TrafficOverlay(
            traffic_key=traffic_key,
            bbox=bbox,
            provider=provider_str,
            algo_version=algo_version,
            created_at=utc_now_iso(),
            items=list(dedup.values()),
            warnings=warnings,
        )

        put_traffic_pack(
            self.conn,
            traffic_key=traffic_key,
            created_at=pack.created_at,
            algo_version=algo_version,
            pack=pack.model_dump(),
        )
        return pack
