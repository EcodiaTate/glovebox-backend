# app/services/hazards.py
"""
Multi-state Australian hazard overlay service.

Sources (auto-selected by bbox → state detection):
  - BOM RSS warnings: Per-state XML feeds (QLD, NSW, VIC, SA, WA, NT, TAS)
  - QLD: Disaster CAP + Emergency Alerts (CAP-AU XML)
  - NSW: RFS major incidents (JSON)
  - VIC: Emergency Victoria incidents (JSON)
  - SA:  CFS current incidents (JSON)
  - WA:  DFES incidents + warnings (api.emergency.wa.gov.au/v1/)
  - NT:  Emergency Announcements (roadreport.nt.gov.au/api/Announcement/GetEmergencyAnnouncements)
  - TAS: TheList ArcGIS Emergency Management (public, no auth)
  - National: DEA satellite fire hotspots (all states, CC-BY 4.0)
  - National: RADAR roadworks/closures (federal, all states, ArcGIS FeatureServer)
  - QLD Parks: Park closure/alert RSS (parks.qld.gov.au)
  - NSW NPWS: National Parks park closure/alert RSS (nationalparks.nsw.gov.au)
  - VIC Parks: Park closure/alert RSS (parks.vic.gov.au) - disabled until URL confirmed
  - SA Parks: Park closure/alert RSS (parks.sa.gov.au) - disabled until URL confirmed
  - NT Parks: Park closure/alert RSS (nt.gov.au) - disabled until URL confirmed
  - TAS Parks: Park closure/alert RSS (parks.tas.gov.au) - disabled until URL confirmed

Key features:
  - National coverage (all states)
  - Composite CAP severity scoring (severity × urgency × certainty)
  - Active expiry pruning (expired events are filtered out)
  - Region tagging on every event
  - Satellite fire detection supplements all state fire APIs
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

import asyncio
import email.utils
import hashlib
import json
import logging
import math
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

import httpx

from app.core.contracts import BBox4, HazardEvent, HazardOverlay
from app.core.geo_registry import states_for_bbox
from app.core.settings import settings
from app.core.storage import get_hazards_pack, put_hazards_pack
from app.core.time import utc_now_iso
from app.core.cache_utils import is_fresh, stable_key


# ══════════════════════════════════════════════════════════════
# Shared helpers
# ══════════════════════════════════════════════════════════════

BBox = Tuple[float, float, float, float]  # minLng,minLat,maxLng,maxLat


def _stable_id(parts: List[str]) -> str:
    h = hashlib.sha256()
    for p in parts:
        h.update((p or "").encode("utf-8"))
        h.update(b"\x1f")
    return h.hexdigest()[:24]


def _bbox_intersects(a: BBox, b: BBox4) -> bool:
    return not (a[2] < b.minLng or a[0] > b.maxLng or a[3] < b.minLat or a[1] > b.maxLat)


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


def _is_expired(end_str: Optional[str]) -> bool:
    """Return True if the event has an end/expires time in the past."""
    if not end_str:
        return False
    ts = _parse_iso_to_epoch(end_str)
    if ts is None:
        return False
    return time.time() > ts


def _safe_float(x: Any) -> Optional[float]:
    try:
        f = float(x)
        if math.isfinite(f):
            return f
    except Exception:
        return None
    return None


# ══════════════════════════════════════════════════════════════
# Kind + severity classification
# ══════════════════════════════════════════════════════════════

def _kind_from_text(title: str, event: Optional[str] = None) -> str:
    t = (event or title or "").lower()
    if "flood" in t:
        return "flood"
    if "cyclone" in t or "tropical" in t:
        return "cyclone"
    if "storm" in t or "thunder" in t:
        return "storm"
    if "fire" in t or "bushfire" in t or "grass fire" in t:
        return "fire"
    if "wind" in t or "gale" in t or "damaging wind" in t:
        return "wind"
    if "heat" in t or "heatwave" in t:
        return "heat"
    if "marine" in t or "coastal" in t or "surf" in t:
        return "marine"
    return "weather_warning"


def _severity_from_text(title: str, desc: str) -> str:
    hay = f"{title} {desc}".lower()
    if "emergency warning" in hay or "evacuate" in hay or "dangerous" in hay or "life threatening" in hay:
        return "high"
    if "warning" in hay or "severe" in hay:
        return "medium"
    if "watch" in hay or "advice" in hay or "minor" in hay:
        return "low"
    return "unknown"


# ══════════════════════════════════════════════════════════════
# CAP composite severity scoring
# ══════════════════════════════════════════════════════════════

_CAP_SEVERITY_SCORES: Dict[str, float] = {
    "extreme": 1.0,
    "severe": 0.8,
    "moderate": 0.5,
    "minor": 0.25,
    "unknown": 0.3,
}

_CAP_URGENCY_SCORES: Dict[str, float] = {
    "immediate": 1.0,
    "expected": 0.75,
    "future": 0.4,
    "past": 0.1,
    "unknown": 0.3,
}

_CAP_CERTAINTY_SCORES: Dict[str, float] = {
    "observed": 1.0,
    "likely": 0.8,
    "possible": 0.5,
    "unlikely": 0.2,
    "unknown": 0.3,
}


def _severity_from_cap(sev: Optional[str]) -> str:
    """Map CAP severity to our HazardSeverity."""
    s = (sev or "").strip().lower()
    if s in ("extreme", "severe"):
        return "high"
    if s == "moderate":
        return "medium"
    if s == "minor":
        return "low"
    return "unknown"


def _compute_effective_priority(
    severity: Optional[str],
    urgency: Optional[str],
    certainty: Optional[str],
) -> float:
    """
    Composite CAP priority score: 0.0 (least urgent) to 1.0 (most urgent).

    Weights: severity 40%, urgency 35%, certainty 25%.

    A "Severe + Immediate + Observed" flood scores much higher than a
    "Severe + Future + Possible" storm - this dramatically improves ranking
    when there are 10+ warnings in a region.
    """
    sev_s = _CAP_SEVERITY_SCORES.get((severity or "").strip().lower(), 0.3)
    urg_s = _CAP_URGENCY_SCORES.get((urgency or "").strip().lower(), 0.3)
    cer_s = _CAP_CERTAINTY_SCORES.get((certainty or "").strip().lower(), 0.3)
    return round(sev_s * 0.40 + urg_s * 0.35 + cer_s * 0.25, 3)


def _normalise_cap_urgency(val: Optional[str]) -> str:
    v = (val or "").strip().lower()
    if v in ("immediate", "expected", "future", "past"):
        return v
    return "unknown"


def _normalise_cap_certainty(val: Optional[str]) -> str:
    v = (val or "").strip().lower()
    if v in ("observed", "likely", "possible", "unlikely"):
        return v
    return "unknown"


# ══════════════════════════════════════════════════════════════
# Geometry helpers
# ══════════════════════════════════════════════════════════════

def _bbox_of_coords(coords: List[Tuple[float, float]]) -> Optional[BBox]:
    if not coords:
        return None
    lngs = [c[0] for c in coords]
    lats = [c[1] for c in coords]
    return (min(lngs), min(lats), max(lngs), max(lats))


def _geom_bbox(geom: Dict[str, Any]) -> Optional[BBox]:
    def collect_points(g: Dict[str, Any], out: List[Tuple[float, float]]) -> None:
        t = g.get("type")
        coords = g.get("coordinates")
        if t == "Point" and isinstance(coords, (list, tuple)) and len(coords) >= 2:
            out.append((float(coords[0]), float(coords[1])))
            return
        if t in ("LineString", "MultiPoint") and isinstance(coords, list):
            for p in coords:
                if isinstance(p, (list, tuple)) and len(p) >= 2:
                    out.append((float(p[0]), float(p[1])))
            return
        if t in ("MultiLineString", "Polygon") and isinstance(coords, list):
            for ring in coords:
                if not isinstance(ring, list):
                    continue
                for p in ring:
                    if isinstance(p, (list, tuple)) and len(p) >= 2:
                        out.append((float(p[0]), float(p[1])))
            return
        if t == "MultiPolygon" and isinstance(coords, list):
            for poly in coords:
                if not isinstance(poly, list):
                    continue
                for ring in poly:
                    if not isinstance(ring, list):
                        continue
                    for p in ring:
                        if isinstance(p, (list, tuple)) and len(p) >= 2:
                            out.append((float(p[0]), float(p[1])))
            return
        if t == "GeometryCollection":
            geoms = g.get("geometries")
            if isinstance(geoms, list):
                for sg in geoms:
                    if isinstance(sg, dict):
                        collect_points(sg, out)

    pts: List[Tuple[float, float]] = []
    try:
        collect_points(geom, pts)
    except Exception:
        return None
    return _bbox_of_coords(pts)


# ══════════════════════════════════════════════════════════════
# XML helpers
# ══════════════════════════════════════════════════════════════

def _localname(tag: str) -> str:
    if "}" in tag:
        return tag.split("}", 1)[1]
    return tag


def _find_first_text(el: ET.Element, name: str) -> Optional[str]:
    for child in el.iter():
        if _localname(child.tag) == name:
            txt = (child.text or "").strip()
            return txt if txt else None
    return None


def _parse_cap_polygon(poly_text: str) -> Optional[List[List[float]]]:
    """CAP-AU polygon: "lat,lon lat,lon ..." → GeoJSON ring [[lon,lat],...]"""
    pts: List[List[float]] = []
    raw = (poly_text or "").strip()
    if not raw:
        return None
    parts = [p for p in raw.replace("\n", " ").split(" ") if p.strip()]
    for part in parts:
        if "," not in part:
            continue
        a, b = part.split(",", 1)
        lat = _safe_float(a)
        lon = _safe_float(b)
        if lat is None or lon is None:
            continue
        pts.append([float(lon), float(lat)])
    if len(pts) < 3:
        return None
    if pts[0] != pts[-1]:
        pts.append(pts[0])
    return pts


def _cap_info_to_geometry(info: ET.Element) -> Optional[Dict[str, Any]]:
    polygons: List[List[List[float]]] = []
    points: List[List[float]] = []

    for area in info.findall(".//*"):
        if _localname(area.tag) != "area":
            continue
        for poly in area.findall(".//*"):
            if _localname(poly.tag) == "polygon":
                ring = _parse_cap_polygon((poly.text or "").strip())
                if ring:
                    polygons.append(ring)
        for cir in area.findall(".//*"):
            if _localname(cir.tag) == "circle":
                txt = (cir.text or "").strip()
                if not txt:
                    continue
                bits = [b for b in txt.replace(",", " ").split() if b.strip()]
                if len(bits) >= 2:
                    lat = _safe_float(bits[0])
                    lon = _safe_float(bits[1])
                    if lat is not None and lon is not None:
                        points.append([float(lon), float(lat)])

    if polygons:
        if len(polygons) == 1:
            return {"type": "Polygon", "coordinates": [polygons[0]]}
        return {"type": "MultiPolygon", "coordinates": [[[ring]] for ring in polygons]}

    if points:
        return {"type": "Point", "coordinates": points[0]}

    return None


# ══════════════════════════════════════════════════════════════
# BOM RSS parser (per-state weather warnings)
# ══════════════════════════════════════════════════════════════

def _parse_rss(xml_text: str, source: str, region: str) -> List[HazardEvent]:
    out: List[HazardEvent] = []
    try:
        root = ET.fromstring(xml_text)
    except Exception:
        return out

    for item in root.findall(".//item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip() or None
        desc = (item.findtext("description") or "").strip() or None
        pub = (item.findtext("pubDate") or "").strip() or None

        if not title and not desc:
            continue

        sev = _severity_from_text(title, desc or "")
        kind = _kind_from_text(title)
        hid = _stable_id([source, region, title[:160], (link or "")[:160], (pub or "")[:80]])

        out.append(
            HazardEvent(
                id=hid,
                source=source,
                kind=kind,           # type: ignore
                severity=sev,        # type: ignore
                urgency="unknown",   # type: ignore
                certainty="unknown", # type: ignore
                effective_priority=_compute_effective_priority(None, None, None),
                title=title or "Warning",
                description=desc,
                url=link,
                issued_at=pub,
                start_at=None,
                end_at=None,
                geometry=None,
                bbox=None,
                region=region,
                raw={},
            )
        )
    return out


# ══════════════════════════════════════════════════════════════
# CAP-AU parser (emergency alerting protocol)
# ══════════════════════════════════════════════════════════════

def _parse_cap(xml_text: str, source: str, region: str) -> List[HazardEvent]:
    out: List[HazardEvent] = []
    try:
        root = ET.fromstring(xml_text)
    except Exception:
        return out

    alert_identifier = _find_first_text(root, "identifier") or ""
    alert_sent = _find_first_text(root, "sent") or None

    # Check for alert-level status - skip "Cancel" alerts
    alert_status = (_find_first_text(root, "status") or "").strip().lower()
    if alert_status == "cancel":
        return out

    alert_msgtype = (_find_first_text(root, "msgType") or "").strip().lower()

    for info in [el for el in root.iter() if _localname(el.tag) == "info"]:
        event_name = _find_first_text(info, "event") or None
        headline = _find_first_text(info, "headline") or None
        description = _find_first_text(info, "description") or None
        instruction = _find_first_text(info, "instruction") or None

        # CAP-AU three dimensions
        severity_cap = _find_first_text(info, "severity") or None
        urgency_cap = _find_first_text(info, "urgency") or None
        certainty_cap = _find_first_text(info, "certainty") or None

        onset = _find_first_text(info, "onset") or None
        effective = _find_first_text(info, "effective") or None
        expires = _find_first_text(info, "expires") or None

        # Prune expired events
        if _is_expired(expires):
            continue

        web = None
        for ch in info.iter():
            if _localname(ch.tag) == "web":
                t = (ch.text or "").strip()
                if t:
                    web = t
                    break

        title = (headline or event_name or "Warning").strip()
        desc = description or instruction

        sev = _severity_from_cap(severity_cap)
        kind = _kind_from_text(title, event=event_name)
        urg = _normalise_cap_urgency(urgency_cap)
        cer = _normalise_cap_certainty(certainty_cap)
        eff_pri = _compute_effective_priority(severity_cap, urgency_cap, certainty_cap)

        geom = _cap_info_to_geometry(info)
        bb = _geom_bbox(geom) if geom else None
        bbox_out = [bb[0], bb[1], bb[2], bb[3]] if bb else None

        hid = _stable_id([
            source, region, alert_identifier,
            title[:160],
            (effective or onset or "")[:80],
            (expires or "")[:80],
        ])

        out.append(
            HazardEvent(
                id=hid,
                source=source,
                kind=kind,                   # type: ignore
                severity=sev,                # type: ignore
                urgency=urg,                 # type: ignore
                certainty=cer,               # type: ignore
                effective_priority=eff_pri,
                title=title,
                description=desc,
                url=web,
                issued_at=alert_sent,
                start_at=effective or onset or alert_sent,
                end_at=expires,
                geometry=geom,
                bbox=bbox_out,
                region=region,
                raw={},
            )
        )

    return out


# ══════════════════════════════════════════════════════════════
# NSW: RFS major incidents (JSON)
# ══════════════════════════════════════════════════════════════

def _parse_nsw_rfs_json(json_text: str) -> List[HazardEvent]:
    """Parse NSW Rural Fire Service major incidents JSON feed."""
    out: List[HazardEvent] = []
    try:
        data = json.loads(json_text)
    except Exception:
        return out

    # RFS returns either a dict with "features" or a list
    features = []
    if isinstance(data, dict):
        features = data.get("features") or data.get("incidents") or []
    elif isinstance(data, list):
        features = data

    for f in features:
        if not isinstance(f, dict):
            continue

        props = f.get("properties") or f
        if not isinstance(props, dict):
            props = f

        title = str(props.get("title") or props.get("description") or "NSW Fire Incident").strip()
        desc = str(props.get("description") or props.get("alert_level") or "").strip()
        pub_date = props.get("pubDate") or props.get("updated") or props.get("guid") or None
        category = str(props.get("category") or props.get("type") or "").lower()

        # Determine severity from fire alert level
        alert_level = str(props.get("alert_level") or props.get("alertLevel") or "").lower()
        if "emergency" in alert_level or "emergency warning" in title.lower():
            sev = "high"
        elif "watch" in alert_level and "act" in alert_level:
            sev = "high"
        elif "advice" in alert_level:
            sev = "medium"
        else:
            sev = _severity_from_text(title, desc)

        # Geometry
        geom = f.get("geometry") or None
        bb = _geom_bbox(geom) if geom and isinstance(geom, dict) else None
        bbox_out = [bb[0], bb[1], bb[2], bb[3]] if bb else None

        # If no geometry, try lat/lng from properties
        if not geom:
            lat = _safe_float(props.get("latitude") or props.get("lat"))
            lng = _safe_float(props.get("longitude") or props.get("lng") or props.get("lon"))
            if lat is not None and lng is not None:
                geom = {"type": "Point", "coordinates": [lng, lat]}
                bbox_out = [lng, lat, lng, lat]

        hid = _stable_id(["nsw_rfs", title[:160], str(pub_date or ""), str(bbox_out or "")])

        out.append(
            HazardEvent(
                id=hid,
                source="nsw_rfs",
                kind="fire",             # type: ignore
                severity=sev,            # type: ignore
                urgency="immediate" if sev == "high" else "expected",  # type: ignore
                certainty="observed",    # type: ignore - fires are observed events
                effective_priority=_compute_effective_priority(
                    "severe" if sev == "high" else "moderate",
                    "immediate" if sev == "high" else "expected",
                    "observed",
                ),
                title=title,
                description=(desc or None),
                url=str(props.get("link") or props.get("url") or ""),
                issued_at=str(pub_date) if pub_date else None,
                start_at=str(pub_date) if pub_date else None,
                end_at=None,
                geometry=geom,
                bbox=bbox_out,
                region="nsw",
                raw=props,
            )
        )

    return out


# ══════════════════════════════════════════════════════════════
# VIC: Emergency Victoria incidents (JSON)
# ══════════════════════════════════════════════════════════════

def _parse_vic_emergency_json(json_text: str) -> List[HazardEvent]:
    """
    Parse VIC Emergency incidents JSON.

    Real format is a flat array of objects like:
    {
      "incidentNo": 258693,
      "lastUpdateDateTime": "20/01/2026 14:53:00",
      "originDateTime": "20/01/2026 14:53:00",
      "incidentType": "BUSHFIRE",
      "incidentLocation": "6KM SE OF BURNT BRIDGE",
      "incidentStatus": "Under Control",
      "incidentSize": "0.16 HA.",
      "name": "MT TERRIBLE - DANES SPUR TK",
      "territory": "DELWP",
      "resourceCount": 0,
      "latitude": -37.394737,
      "longitude": 146.09224,
      ...
    }
    """
    out: List[HazardEvent] = []
    try:
        data = json.loads(json_text)
    except Exception:
        return out

    # Response is either a flat array or {"results": [...]}
    incidents: list = []
    if isinstance(data, list):
        incidents = data
    elif isinstance(data, dict):
        incidents = data.get("results") or data.get("incidents") or data.get("features") or []

    for inc in incidents:
        if not isinstance(inc, dict):
            continue

        incident_no = inc.get("incidentNo") or ""
        inc_type = str(inc.get("incidentType") or "").strip()
        inc_status = str(inc.get("incidentStatus") or inc.get("originStatus") or "").strip()
        inc_location = str(inc.get("incidentLocation") or "").strip()
        inc_name = str(inc.get("name") or "").strip()
        inc_size = str(inc.get("incidentSize") or inc.get("incidentSizeFmt") or "").strip()
        municipality = str(inc.get("municipality") or "").strip()
        category1 = str(inc.get("category1") or "").strip()
        category2 = str(inc.get("category2") or "").strip()
        agency = str(inc.get("agency") or inc.get("territory") or "").strip()
        resource_count = inc.get("resourceCount") or 0

        # Build a readable headline
        if inc_name and inc_location:
            title = f"{inc_name} - {inc_location}"
        elif inc_name:
            title = inc_name
        elif inc_location:
            title = f"{inc_type} - {inc_location}" if inc_type else inc_location
        else:
            title = inc_type or "VIC Emergency Incident"

        # Build description with useful details
        desc_parts: List[str] = []
        if inc_type:
            desc_parts.append(inc_type)
        if inc_size and inc_size != "0.00 HA.":
            desc_parts.append(f"Size: {inc_size}")
        if municipality:
            desc_parts.append(f"Municipality: {municipality}")
        if agency:
            desc_parts.append(f"Agency: {agency}")
        if resource_count:
            desc_parts.append(f"Resources: {resource_count}")
        desc = ". ".join(desc_parts) if desc_parts else None

        # Kind from category1/category2/incidentType
        cat_text = f"{category1} {category2} {inc_type}".lower()
        kind = _kind_from_text(cat_text)

        # Severity from status
        status_lower = inc_status.lower()
        origin_status = str(inc.get("originStatus") or "").lower()
        if status_lower in ("emergency warning", "emergency"):
            sev = "high"
        elif status_lower in ("watch and act", "watch_and_act"):
            sev = "high"
        elif status_lower in ("advice", "going"):
            sev = "medium"
        elif status_lower in ("under control", "controlled", "safe"):
            sev = "low"
        elif origin_status == "controlled":
            sev = "low"
        else:
            sev = _severity_from_text(title, desc or "")

        # Geometry from latitude/longitude
        geom: Optional[Dict[str, Any]] = None
        bbox_out: Optional[List[float]] = None
        lat = _safe_float(inc.get("latitude"))
        lng = _safe_float(inc.get("longitude"))
        if lat is not None and lng is not None:
            geom = {"type": "Point", "coordinates": [lng, lat]}
            bbox_out = [lng, lat, lng, lat]

        # Timestamps - VIC uses "DD/MM/YYYY HH:MM:SS" format
        updated_raw = inc.get("lastUpdateDateTime") or inc.get("lastUpdatedDtStr") or None
        origin_raw = inc.get("originDateTime") or inc.get("originDateTimeStr") or None
        # Also try epoch millis
        updated_epoch = inc.get("lastUpdatedDt")
        issued_at: Optional[str] = None
        if isinstance(updated_epoch, (int, float)) and updated_epoch > 0:
            try:
                issued_at = datetime.fromtimestamp(updated_epoch / 1000, tz=timezone.utc).isoformat()
            except Exception:
                issued_at = str(updated_raw) if updated_raw else None
        elif updated_raw:
            issued_at = str(updated_raw)

        hid = _stable_id(["vic_emergency", str(incident_no), str(issued_at or origin_raw or "")])

        out.append(
            HazardEvent(
                id=hid,
                source="vic_emergency",
                kind=kind,               # type: ignore
                severity=sev,            # type: ignore
                urgency="immediate" if sev == "high" else ("expected" if sev == "medium" else "past"),  # type: ignore
                certainty="observed",    # type: ignore
                effective_priority=_compute_effective_priority(
                    "severe" if sev == "high" else ("moderate" if sev == "medium" else "minor"),
                    "immediate" if sev == "high" else ("expected" if sev == "medium" else "past"),
                    "observed",
                ),
                title=title,
                description=desc,
                url=None,
                issued_at=issued_at,
                start_at=str(origin_raw) if origin_raw else None,
                end_at=None,
                geometry=geom,
                bbox=bbox_out,
                region="vic",
                raw=inc,
            )
        )

    return out


# ══════════════════════════════════════════════════════════════
# SA: CFS incidents (JSON)
# ══════════════════════════════════════════════════════════════

def _parse_sa_cfs_json(json_text: str) -> List[HazardEvent]:
    """
    Parse SA CFS current incidents JSON.

    Real format is a flat JSON array of objects like:
    {
      "IncidentNo": "1689944",
      "Date": "17/02/2026",
      "Time": "16:25",
      "Location_name": "MCLAREN VALE, KAYS RD/CHAFFEYS RD",
      "Type": "Vehicle Accident",
      "Status": "GOING",
      "Level": 1,
      "FBD": "MOUNT LOFTY RANGES",
      "Resources": 3,
      "Aircraft": 0,
      "Location": "-35.187365,138.554013"
    }
    """
    out: List[HazardEvent] = []
    try:
        data = json.loads(json_text)
    except Exception:
        return out

    # Response is a flat JSON array
    incidents: list = []
    if isinstance(data, list):
        incidents = data
    elif isinstance(data, dict):
        incidents = data.get("incidents") or data.get("results") or data.get("features") or []

    for inc in incidents:
        if not isinstance(inc, dict):
            continue

        incident_no = str(inc.get("IncidentNo") or "").strip()
        inc_type = str(inc.get("Type") or "").strip()
        status = str(inc.get("Status") or "").strip()
        status_lower = status.lower()
        location_name = str(inc.get("Location_name") or "").strip()
        level = inc.get("Level") or 1
        fbd = str(inc.get("FBD") or "").strip()
        resources = inc.get("Resources") or 0
        aircraft = inc.get("Aircraft") or 0
        date_str = str(inc.get("Date") or "").strip()
        time_str = str(inc.get("Time") or "").strip()
        message_link = str(inc.get("Message_link") or "").strip()

        # Build readable headline
        if location_name and inc_type:
            title = f"{inc_type} - {location_name}"
        elif location_name:
            title = location_name
        elif inc_type:
            title = f"SA CFS: {inc_type}"
        else:
            title = "SA CFS Incident"

        # Build description
        desc_parts: List[str] = []
        if status:
            desc_parts.append(f"Status: {status}")
        if fbd:
            desc_parts.append(f"District: {fbd}")
        if resources:
            desc_parts.append(f"Resources: {resources}")
        if aircraft:
            desc_parts.append(f"Aircraft: {aircraft}")
        desc = ". ".join(desc_parts) if desc_parts else None

        # Kind from Type field
        kind = _kind_from_text(inc_type)
        # Vehicle accidents and assist agency aren't natural hazards but are
        # useful for route safety awareness
        if inc_type.lower() in ("vehicle accident", "assist agency", "rescue"):
            kind = "weather_warning"  # generic - not a natural hazard kind

        # Severity from Status + Level
        # Level 3 = Emergency Warning, Level 2 = Watch and Act, Level 1 = Advice
        try:
            level_int = int(level)
        except (ValueError, TypeError):
            level_int = 1

        if level_int >= 3 or "emergency" in status_lower:
            sev = "high"
        elif level_int == 2 or status_lower == "going":
            sev = "medium"
        elif status_lower in ("contained", "controlled"):
            sev = "low"
        elif status_lower == "complete":
            sev = "low"
        else:
            sev = _severity_from_text(title, desc or "")

        # Geometry from Location field: "lat,lng" as comma-separated string
        geom: Optional[Dict[str, Any]] = None
        bbox_out: Optional[List[float]] = None
        location_str = str(inc.get("Location") or "").strip()
        if location_str and "," in location_str:
            parts = location_str.split(",", 1)
            lat = _safe_float(parts[0].strip())
            lng = _safe_float(parts[1].strip())
            if lat is not None and lng is not None:
                geom = {"type": "Point", "coordinates": [lng, lat]}
                bbox_out = [lng, lat, lng, lat]

        # Build ISO timestamp from Date (DD/MM/YYYY) + Time (HH:MM)
        issued_at: Optional[str] = None
        if date_str and time_str:
            try:
                dt = datetime.strptime(f"{date_str} {time_str}", "%d/%m/%Y %H:%M")
                # SA is UTC+9:30 (ACST) / UTC+10:30 (ACDT)
                issued_at = dt.isoformat()
            except Exception:
                issued_at = f"{date_str} {time_str}"
        elif date_str:
            issued_at = date_str

        # Extract update URL from Message_link HTML if present
        url: Optional[str] = None
        if message_link and "href=" in message_link:
            href_match = re.search(r"href=['\"]([^'\"]+\.html)['\"]", message_link)
            if href_match:
                url = href_match.group(1)

        # Map CFS level to CAP equivalents for composite scoring
        if level_int >= 3:
            cap_sev, cap_urg = "extreme", "immediate"
        elif level_int == 2:
            cap_sev, cap_urg = "severe", "expected"
        elif status_lower == "going":
            cap_sev, cap_urg = "moderate", "expected"
        else:
            cap_sev, cap_urg = "minor", "past"

        hid = _stable_id(["sa_cfs", incident_no or title[:160]])

        out.append(
            HazardEvent(
                id=hid,
                source="sa_cfs",
                kind=kind,           # type: ignore
                severity=sev,        # type: ignore
                urgency=_normalise_cap_urgency(cap_urg),   # type: ignore
                certainty="observed",                       # type: ignore
                effective_priority=_compute_effective_priority(cap_sev, cap_urg, "observed"),
                title=title,
                description=desc,
                url=url,
                issued_at=issued_at,
                start_at=issued_at,
                end_at=None,
                geometry=geom,
                bbox=bbox_out,
                region="sa",
                raw=inc,
            )
        )

    return out


# ══════════════════════════════════════════════════════════════
# WA: DFES emergency incidents (JSON)
# ══════════════════════════════════════════════════════════════

def _parse_wa_dfes_incidents(json_text: str) -> List[HazardEvent]:
    """
    Parse WA DFES incidents from api.emergency.wa.gov.au/v1/incidents.

    Response is {"incidents": [{...}, ...]} where each incident has:
      id, name, incident-type, incident-status, incident-icon, entitySubType,
      suburbs, lga, dfes-regions, start-date-time, updated-date-time,
      location: {latitude, value, longitude},
      geo-source: {features: [{geometry: {...}}]}
    """
    out: List[HazardEvent] = []
    try:
        data = json.loads(json_text)
    except Exception:
        return out

    incidents = data.get("incidents") or []
    if not isinstance(incidents, list):
        return out

    for inc in incidents:
        if not isinstance(inc, dict):
            continue

        inc_id = str(inc.get("id") or "").strip()
        name = str(inc.get("name") or "").strip()
        inc_type = str(inc.get("incident-type") or "").strip()
        inc_status = str(inc.get("incident-status") or "").strip()
        entity_sub = str(inc.get("entitySubType") or "").strip()
        suburbs = inc.get("suburbs") or []
        lga = inc.get("lga") or []
        dfes_regions = inc.get("dfes-regions") or []

        # Build title
        suburb_str = ", ".join(suburbs) if isinstance(suburbs, list) else ""
        if name and suburb_str:
            title = f"{name} - {suburb_str}"
        elif name:
            title = name
        else:
            title = inc_type or "WA Incident"

        # Build description
        desc_parts: List[str] = []
        if inc_type:
            desc_parts.append(inc_type)
        if inc_status:
            desc_parts.append(f"Status: {inc_status}")
        loc_obj = inc.get("location") or {}
        if isinstance(loc_obj, dict) and loc_obj.get("value"):
            desc_parts.append(str(loc_obj["value"]))
        cross = inc.get("nearest-cross-street") or {}
        if isinstance(cross, dict) and cross.get("value"):
            desc_parts.append(f"Near: {cross['value']}")
        if lga and isinstance(lga, list):
            desc_parts.append(", ".join(lga))
        desc = ". ".join(desc_parts) if desc_parts else None

        # Kind from incident-icon or name
        icon = str(inc.get("incident-icon") or "").lower()
        name_lower = name.lower()
        entity_lower = entity_sub.lower()
        if "fire" in icon or "fire" in name_lower or "bushfire" in entity_lower:
            kind = "fire"
        elif "flood" in icon or "flood" in name_lower or "flood" in entity_lower:
            kind = "flood"
        elif "storm" in icon or "storm" in name_lower:
            kind = "storm"
        elif "cyclone" in name_lower:
            kind = "cyclone"
        else:
            kind = "weather_warning"

        # Severity from status
        status_lower = inc_status.lower()
        if status_lower in ("emergency warning", "emergency"):
            sev = "high"
        elif status_lower in ("responding", "on scene"):
            sev = "medium"
        elif status_lower in ("monitoring", "controlled", "safe"):
            sev = "low"
        else:
            sev = _severity_from_text(name, desc or "")

        # Geometry from geo-source → features[0].geometry
        geom: Optional[Dict[str, Any]] = None
        bbox_out: Optional[List[float]] = None
        geo_source = inc.get("geo-source") or {}
        features = geo_source.get("features") or []
        if features and isinstance(features, list):
            first_geom = (features[0] or {}).get("geometry")
            if isinstance(first_geom, dict) and first_geom.get("type"):
                geom = first_geom
                bb = _geom_bbox(geom)
                if bb:
                    bbox_out = [bb[0], bb[1], bb[2], bb[3]]

        # Fallback: location object has lat/lng
        if not geom and isinstance(loc_obj, dict):
            lat = _safe_float(loc_obj.get("latitude"))
            lng = _safe_float(loc_obj.get("longitude"))
            if lat is not None and lng is not None:
                geom = {"type": "Point", "coordinates": [lng, lat]}
                bbox_out = [lng, lat, lng, lat]

        # Timestamps - ISO format with timezone offset
        start_dt = inc.get("start-date-time") or None
        updated_dt = inc.get("updated-date-time") or inc.get("issued-date-time") or None

        hid = _stable_id(["wa_dfes_inc", inc_id or title[:160]])

        out.append(
            HazardEvent(
                id=hid,
                source="wa_dfes",
                kind=kind,                   # type: ignore
                severity=sev,                # type: ignore
                urgency="immediate" if sev == "high" else ("expected" if sev == "medium" else "past"),  # type: ignore
                certainty="observed",        # type: ignore
                effective_priority=_compute_effective_priority(
                    "severe" if sev == "high" else ("moderate" if sev == "medium" else "minor"),
                    "immediate" if sev == "high" else ("expected" if sev == "medium" else "past"),
                    "observed",
                ),
                title=title,
                description=desc,
                url=f"https://www.emergency.wa.gov.au/",
                issued_at=str(updated_dt) if updated_dt else None,
                start_at=str(start_dt) if start_dt else None,
                end_at=None,
                geometry=geom,
                bbox=bbox_out,
                region="wa",
                raw=inc,
            )
        )

    return out


def _parse_wa_dfes_warnings(json_text: str) -> List[HazardEvent]:
    """
    Parse WA DFES warnings from api.emergency.wa.gov.au/v1/warnings.

    Response is {"warnings": [{...}, ...]} where each warning has:
      id, headline, warning-type, action-statement, cap-category,
      sorting-priority, suburbs, lga, published-date-time, updatedAt,
      geo-source: {features: [{geometry: {...}}]}
    """
    out: List[HazardEvent] = []
    try:
        data = json.loads(json_text)
    except Exception:
        return out

    warnings = data.get("warnings") or []
    if not isinstance(warnings, list):
        return out

    for w in warnings:
        if not isinstance(w, dict):
            continue

        w_id = str(w.get("id") or "").strip()
        headline = str(w.get("headline") or "").strip()
        warning_type = str(w.get("warning-type") or "").strip()
        action = str(w.get("action-statement") or "").strip()
        cap_cat = str(w.get("cap-category") or "").strip()
        sorting_priority = w.get("sorting-priority") or 99
        suburbs = w.get("suburbs") or []
        lga = w.get("lga") or []

        # Build title from warning-type + headline
        if warning_type and headline:
            title = f"{warning_type} - {headline}"
        elif warning_type:
            title = warning_type
        elif headline:
            title = headline
        else:
            title = "WA DFES Warning"

        # Description from action-statement + suburbs
        desc_parts: List[str] = []
        if action:
            desc_parts.append(action)
        if suburbs and isinstance(suburbs, list):
            desc_parts.append(f"Areas: {', '.join(suburbs)}")
        if lga and isinstance(lga, list):
            desc_parts.append(f"LGA: {', '.join(lga)}")
        desc = ". ".join(desc_parts) if desc_parts else None

        # Kind from warning-type
        wt_lower = warning_type.lower()
        if "flood" in wt_lower:
            kind = "flood"
        elif "bushfire" in wt_lower or "fire" in wt_lower:
            kind = "fire"
        elif "cyclone" in wt_lower or "tropical" in wt_lower:
            kind = "cyclone"
        elif "storm" in wt_lower or "thunder" in wt_lower:
            kind = "storm"
        elif "wind" in wt_lower:
            kind = "wind"
        elif "heat" in wt_lower:
            kind = "heat"
        else:
            kind = _kind_from_text(title)

        # Severity from warning-type keywords
        # WA DFES uses: Emergency Warning > Watch and Act > Advice
        if "emergency warning" in wt_lower:
            sev = "high"
            cap_sev, cap_urg = "extreme", "immediate"
        elif "watch and act" in wt_lower:
            sev = "high"
            cap_sev, cap_urg = "severe", "immediate"
        elif "warning" in wt_lower and "advice" not in wt_lower:
            sev = "medium"
            cap_sev, cap_urg = "severe", "expected"
        elif "advice" in wt_lower:
            sev = "medium"
            cap_sev, cap_urg = "moderate", "expected"
        else:
            sev = _severity_from_text(title, desc or "")
            cap_sev, cap_urg = "moderate", "expected"

        # Geometry from geo-source - may contain BOTH point markers and polygons
        # We want to collect all polygons for the warning area, or fall back to point
        geom: Optional[Dict[str, Any]] = None
        bbox_out: Optional[List[float]] = None
        geo_source = w.get("geo-source") or {}
        features = geo_source.get("features") or []

        polygons: List[Dict[str, Any]] = []
        points: List[Dict[str, Any]] = []

        for feat in features:
            if not isinstance(feat, dict):
                continue
            fg = feat.get("geometry")
            if not isinstance(fg, dict):
                continue
            gtype = fg.get("type")
            if gtype in ("Polygon", "MultiPolygon"):
                polygons.append(fg)
            elif gtype == "Point":
                points.append(fg)

        # Prefer polygon geometry (actual warning zone) over point markers
        if len(polygons) == 1:
            geom = polygons[0]
        elif len(polygons) > 1:
            geom = {"type": "GeometryCollection", "geometries": polygons}
        elif points:
            geom = points[0]

        if geom:
            bb = _geom_bbox(geom)
            if bb:
                bbox_out = [bb[0], bb[1], bb[2], bb[3]]

        # Timestamps
        published = w.get("published-date-time") or None
        updated = w.get("updatedAt") or published or None

        hid = _stable_id(["wa_dfes_warn", w_id or title[:160]])

        out.append(
            HazardEvent(
                id=hid,
                source="wa_dfes",
                kind=kind,                   # type: ignore
                severity=sev,                # type: ignore
                urgency=_normalise_cap_urgency(cap_urg),    # type: ignore
                certainty="observed" if sev == "high" else "likely",  # type: ignore
                effective_priority=_compute_effective_priority(cap_sev, cap_urg, "observed" if sev == "high" else "likely"),
                title=title,
                description=desc,
                url="https://www.emergency.wa.gov.au/",
                issued_at=str(published) if published else None,
                start_at=str(published) if published else None,
                end_at=None,
                geometry=geom,
                bbox=bbox_out,
                region="wa",
                raw=w,
            )
        )

    return out


# ══════════════════════════════════════════════════════════════
# National: DEA Satellite Fire Hotspots (CC-BY 4.0)
# ══════════════════════════════════════════════════════════════

def _parse_dea_hotspots_json(json_text: str, *, bbox: BBox4) -> List[HazardEvent]:
    """
    Parse DEA (Digital Earth Australia) satellite fire hotspot GeoJSON.

    Source: https://hotspots.dea.ga.gov.au/data/recent-hotspots.json
    Coverage: ALL Australian states via MODIS, HIMAWARI-9, VIIRS, AQUA satellites.

    GeoJSON FeatureCollection where each feature has properties:
      satellite, sensor, latitude, longitude, temp_kelvin, datetime, power,
      confidence (0-100), australian_state, fire_category_name,
      hours_since_hotspot, accuracy (±1-2km)

    Filters:
      - confidence >= settings.dea_hotspots_min_confidence (default 50)
      - hours_since_hotspot <= settings.dea_hotspots_max_hours (default 72)
      - Must fall within query bbox
    """
    out: List[HazardEvent] = []
    min_conf = getattr(settings, "dea_hotspots_min_confidence", 50)
    max_hours = getattr(settings, "dea_hotspots_max_hours", 72)

    try:
        data = json.loads(json_text)
    except Exception:
        return out

    features = []
    if isinstance(data, dict):
        features = data.get("features") or []
    elif isinstance(data, list):
        features = data

    for f in features:
        if not isinstance(f, dict):
            continue

        props = f.get("properties") or {}
        if not isinstance(props, dict):
            props = f

        # Filter by confidence
        confidence = props.get("confidence")
        try:
            conf_val = int(confidence) if confidence is not None else 0
        except (ValueError, TypeError):
            conf_val = 0
        if conf_val < min_conf:
            continue

        # Filter by recency
        hours_since = props.get("hours_since_hotspot")
        try:
            hours_val = float(hours_since) if hours_since is not None else 999
        except (ValueError, TypeError):
            hours_val = 999
        if hours_val > max_hours:
            continue

        # Geometry
        geom = f.get("geometry") or None
        lat = _safe_float(props.get("latitude"))
        lng = _safe_float(props.get("longitude"))

        if not geom and lat is not None and lng is not None:
            geom = {"type": "Point", "coordinates": [lng, lat]}
        elif geom and isinstance(geom, dict):
            # Extract lat/lng from geometry if not in properties
            coords = geom.get("coordinates")
            if isinstance(coords, (list, tuple)) and len(coords) >= 2:
                if lng is None:
                    lng = _safe_float(coords[0])
                if lat is None:
                    lat = _safe_float(coords[1])

        if lat is None or lng is None:
            continue

        # Bbox filter - only include hotspots within the query bbox
        if lng < bbox.minLng or lng > bbox.maxLng or lat < bbox.minLat or lat > bbox.maxLat:
            continue

        bbox_out = [lng, lat, lng, lat]

        # Build title
        state = str(props.get("australian_state") or "").strip()
        fire_cat = str(props.get("fire_category_name") or "").strip()
        satellite = str(props.get("satellite") or "").strip()
        sensor = str(props.get("sensor") or "").strip()
        temp_k = props.get("temp_kelvin")
        power = props.get("power")
        hotspot_dt = str(props.get("datetime") or "").strip()

        region = state.lower() if state else None

        # Title: informative but concise
        if fire_cat and state:
            title = f"Satellite Fire Hotspot - {fire_cat} ({state})"
        elif state:
            title = f"Satellite Fire Hotspot ({state})"
        else:
            title = "Satellite Fire Hotspot"

        # Description: actionable info only (skip satellite/sensor/temp/power)
        desc_parts: List[str] = []
        if fire_cat:
            desc_parts.append(fire_cat)
        desc_parts.append(f"Confidence: {conf_val}%")
        if hours_val < 999:
            desc_parts.append(f"Detected {hours_val:.0f}h ago")
        desc = ". ".join(desc_parts) if desc_parts else None

        # Severity based on confidence + recency
        if conf_val >= 80 and hours_val <= 6:
            sev = "high"
            cap_sev, cap_urg, cap_cer = "severe", "immediate", "observed"
        elif conf_val >= 60 and hours_val <= 24:
            sev = "medium"
            cap_sev, cap_urg, cap_cer = "moderate", "expected", "likely"
        else:
            sev = "low"
            cap_sev, cap_urg, cap_cer = "minor", "future", "possible"

        hid = _stable_id([
            "dea_hotspot",
            f"{lat:.4f}",
            f"{lng:.4f}",
            hotspot_dt[:20] if hotspot_dt else "",
            satellite[:20],
        ])

        out.append(
            HazardEvent(
                id=hid,
                source="dea_hotspots",
                kind="fire",                         # type: ignore
                severity=sev,                        # type: ignore
                urgency=_normalise_cap_urgency(cap_urg),      # type: ignore
                certainty=_normalise_cap_certainty(cap_cer),  # type: ignore
                effective_priority=_compute_effective_priority(cap_sev, cap_urg, cap_cer),
                title=title,
                description=desc,
                url="https://hotspots.dea.ga.gov.au/",
                issued_at=hotspot_dt if hotspot_dt else None,
                start_at=hotspot_dt if hotspot_dt else None,
                end_at=None,
                geometry=geom,
                bbox=bbox_out,
                region=region,
                raw=props,
            )
        )

    return out


# ══════════════════════════════════════════════════════════════
# TAS: TheList ArcGIS Emergency Management (public, no auth)
# ══════════════════════════════════════════════════════════════

def _parse_tas_thelist_json(json_text: str) -> List[HazardEvent]:
    """
    Parse TAS emergency alerts from TheList ArcGIS REST service.

    IMPORTANT: This is ArcGIS JSON format, NOT GeoJSON.
    Geometry uses {"x": lng, "y": lat} in spatial reference 4326.

    Response structure:
    {
      "features": [
        {
          "attributes": {
            "ALERT_TYPE": "Smoke Alert",
            "ALERT_SUMMARY": "...",
            "AREA_DESCRIPTION": "...",
            "ALERT_INSTRUCTIONS": "...",
            "FULL_DESCRIPTION": "...",
            "EVENT": "Bushfire",
            "TASALERT_LINK": "https://...",
            "EFFECTIVE_FROM_DATE": 1708123456000,  // epoch millis
            "EXPIRES_DATE": 1708209856000,         // epoch millis
            "SENDER_NAME": "Tasmania Fire Service"
          },
          "geometry": {"x": 147.123, "y": -42.456}
        }
      ]
    }
    """
    out: List[HazardEvent] = []
    try:
        data = json.loads(json_text)
    except Exception:
        return out

    features = data.get("features") or []
    if not isinstance(features, list):
        return out

    for f in features:
        if not isinstance(f, dict):
            continue

        attrs = f.get("attributes") or {}
        if not isinstance(attrs, dict):
            continue

        alert_type = str(attrs.get("ALERT_TYPE") or "").strip()
        alert_summary = str(attrs.get("ALERT_SUMMARY") or "").strip()
        area_desc = str(attrs.get("AREA_DESCRIPTION") or "").strip()
        instructions = str(attrs.get("ALERT_INSTRUCTIONS") or "").strip()
        full_desc = str(attrs.get("FULL_DESCRIPTION") or "").strip()
        event = str(attrs.get("EVENT") or "").strip()
        tas_link = str(attrs.get("TASALERT_LINK") or "").strip() or None
        sender = str(attrs.get("SENDER_NAME") or "").strip()
        object_id = attrs.get("OBJECTID") or attrs.get("FID") or ""

        # Parse epoch-millis timestamps
        effective_raw = attrs.get("EFFECTIVE_FROM_DATE")
        expires_raw = attrs.get("EXPIRES_DATE")

        effective_at: Optional[str] = None
        expires_at: Optional[str] = None

        if isinstance(effective_raw, (int, float)) and effective_raw > 1_000_000_000:
            try:
                effective_at = datetime.fromtimestamp(effective_raw / 1000, tz=timezone.utc).isoformat()
            except Exception:
                pass
        elif effective_raw:
            effective_at = str(effective_raw)

        if isinstance(expires_raw, (int, float)) and expires_raw > 1_000_000_000:
            try:
                expires_at = datetime.fromtimestamp(expires_raw / 1000, tz=timezone.utc).isoformat()
            except Exception:
                pass
        elif expires_raw:
            expires_at = str(expires_raw)

        # Prune expired
        if expires_at and _is_expired(expires_at):
            continue

        # ArcGIS geometry: {"x": lng, "y": lat} - NOT GeoJSON
        raw_geom = f.get("geometry") or {}
        geom: Optional[Dict[str, Any]] = None
        bbox_out: Optional[List[float]] = None

        if isinstance(raw_geom, dict):
            x = _safe_float(raw_geom.get("x"))
            y = _safe_float(raw_geom.get("y"))
            if x is not None and y is not None:
                geom = {"type": "Point", "coordinates": [x, y]}
                bbox_out = [x, y, x, y]
            # ArcGIS might also have rings for polygons
            rings = raw_geom.get("rings")
            if isinstance(rings, list) and rings:
                # Convert ArcGIS rings to GeoJSON Polygon
                geojson_rings: List[List[List[float]]] = []
                all_points: List[Tuple[float, float]] = []
                for ring in rings:
                    if not isinstance(ring, list):
                        continue
                    gj_ring: List[List[float]] = []
                    for pt in ring:
                        if isinstance(pt, (list, tuple)) and len(pt) >= 2:
                            px = _safe_float(pt[0])
                            py = _safe_float(pt[1])
                            if px is not None and py is not None:
                                gj_ring.append([px, py])
                                all_points.append((px, py))
                    if gj_ring:
                        geojson_rings.append(gj_ring)
                if geojson_rings:
                    geom = {"type": "Polygon", "coordinates": geojson_rings}
                    bb = _bbox_of_coords(all_points)
                    if bb:
                        bbox_out = [bb[0], bb[1], bb[2], bb[3]]

        # Build title
        if alert_type and area_desc:
            title = f"{alert_type} - {area_desc}"
        elif alert_type:
            title = alert_type
        elif alert_summary:
            title = alert_summary
        else:
            title = "TAS Emergency Alert"

        # Build description
        desc_parts: List[str] = []
        if alert_summary and alert_summary != title:
            desc_parts.append(alert_summary)
        if instructions:
            desc_parts.append(instructions)
        if full_desc and full_desc not in (alert_summary, instructions):
            # Truncate if very long
            desc_parts.append(full_desc[:500])
        if sender:
            desc_parts.append(f"Source: {sender}")
        desc = ". ".join(desc_parts) if desc_parts else None

        # Kind from EVENT or ALERT_TYPE
        event_lower = event.lower()
        alert_lower = alert_type.lower()
        combined = f"{event_lower} {alert_lower}"
        kind = _kind_from_text(combined)

        # Severity from alert_type keywords (TAS uses similar patterns to other states)
        if "emergency warning" in alert_lower:
            sev = "high"
            cap_sev, cap_urg = "extreme", "immediate"
        elif "watch and act" in alert_lower:
            sev = "high"
            cap_sev, cap_urg = "severe", "immediate"
        elif "warning" in alert_lower and "advice" not in alert_lower:
            sev = "medium"
            cap_sev, cap_urg = "severe", "expected"
        elif "advice" in alert_lower or "alert" in alert_lower:
            sev = "medium"
            cap_sev, cap_urg = "moderate", "expected"
        elif "smoke" in alert_lower:
            sev = "low"
            cap_sev, cap_urg = "minor", "expected"
        else:
            sev = _severity_from_text(title, desc or "")
            cap_sev, cap_urg = "moderate", "expected"

        hid = _stable_id(["tas_thelist", str(object_id), alert_type[:80], (effective_at or "")[:40]])

        out.append(
            HazardEvent(
                id=hid,
                source="tas_thelist",
                kind=kind,                       # type: ignore
                severity=sev,                    # type: ignore
                urgency=_normalise_cap_urgency(cap_urg),    # type: ignore
                certainty="observed" if sev == "high" else "likely",  # type: ignore
                effective_priority=_compute_effective_priority(
                    cap_sev, cap_urg, "observed" if sev == "high" else "likely"
                ),
                title=title,
                description=desc,
                url=tas_link,
                issued_at=effective_at,
                start_at=effective_at,
                end_at=expires_at,
                geometry=geom,
                bbox=bbox_out,
                region="tas",
                raw=attrs,
            )
        )

    return out


# ══════════════════════════════════════════════════════════════
# NT: Emergency Announcements (roadreport.nt.gov.au)
# ══════════════════════════════════════════════════════════════

def _parse_nt_emergency_announcements(json_text: str) -> List[HazardEvent]:
    """
    Parse NT Road Report emergency announcements.

    Response: {response: [{id, title, content, emergency: bool,
                           lastUpdatedTime, postedDate}], success: bool}

    These are text-only, state-wide alerts about major events such as
    "Major Flooding - Katherine and Darwin Region". No geometry is provided
    so they are treated as non-spatial (shown when bbox is large enough).
    """
    out: List[HazardEvent] = []
    try:
        data = json.loads(json_text)
    except Exception:
        return out

    if not isinstance(data, dict) or not data.get("success"):
        return out

    announcements = data.get("response") or []
    if not isinstance(announcements, list):
        return out

    for ann in announcements:
        if not isinstance(ann, dict):
            continue

        ann_id = str(ann.get("id") or "").strip()
        title = str(ann.get("title") or "").strip()
        content = str(ann.get("content") or "").strip()
        is_emergency = bool(ann.get("emergency") or False)
        last_updated_raw = ann.get("lastUpdatedTime") or ann.get("postedDate") or None

        if not title:
            continue

        kind = _kind_from_text(title, content)
        sev: str
        cap_sev: str
        cap_urg: str
        if is_emergency:
            sev = "high"
            cap_sev, cap_urg = "extreme", "immediate"
        else:
            sev = _severity_from_text(title, content)
            cap_sev, cap_urg = "moderate", "expected"

        # Parse ISO timestamp
        issued_at: Optional[str] = None
        if last_updated_raw:
            issued_at = str(last_updated_raw)

        hid = _stable_id(["nt_emergency_ann", ann_id or title[:160]])

        out.append(
            HazardEvent(
                id=hid,
                source="nt_emergency_ann",
                kind=kind,           # type: ignore
                severity=sev,        # type: ignore
                urgency=_normalise_cap_urgency(cap_urg),    # type: ignore
                certainty="observed",                        # type: ignore
                effective_priority=_compute_effective_priority(cap_sev, cap_urg, "observed"),
                title=title,
                description=(content or None),
                url="https://roadreport.nt.gov.au/",
                issued_at=issued_at,
                start_at=issued_at,
                end_at=None,
                geometry=None,
                bbox=None,
                region="nt",
                raw=ann,
            )
        )

    return out


# ══════════════════════════════════════════════════════════════
# Source registry - maps states to their feeds
# ══════════════════════════════════════════════════════════════

def _bom_rss_url_for_state(state: str) -> Optional[str]:
    """Return BOM RSS warning feed URL for a given state code."""
    if not settings.hazards_enable_bom_rss:
        return None
    mapping = {
        "qld": settings.bom_rss_qld_url,
        "nsw": settings.bom_rss_nsw_url,
        "vic": settings.bom_rss_vic_url,
        "sa":  settings.bom_rss_sa_url,
        "wa":  settings.bom_rss_wa_url,
        "nt":  settings.bom_rss_nt_url,
        "tas": settings.bom_rss_tas_url,
    }
    url = mapping.get(state)
    return str(url) if url else None


def _cap_feeds_for_state(state: str) -> List[Tuple[str, str]]:
    """Return list of (source_name, url) CAP/emergency feeds for a state."""
    feeds: List[Tuple[str, str]] = []

    if state == "qld":
        if settings.qld_disaster_cap_url:
            feeds.append(("qld_disaster_cap", settings.qld_disaster_cap_url))
        if settings.qld_emergency_alerts_url:
            feeds.append(("qld_emergency_alerts", settings.qld_emergency_alerts_url))

    elif state == "nsw":
        # NSW SES warnings XML confirmed 404/dead - no CAP feeds for NSW
        pass
        # NSW RFS is JSON, handled separately in _json_feeds_for_state

    return feeds


def _json_feeds_for_state(state: str) -> List[Tuple[str, str, str]]:
    """Return list of (source_name, url, parser_type) JSON feeds for a state."""
    feeds: List[Tuple[str, str, str]] = []

    if state == "nsw":
        if settings.nsw_rfs_fires_url:
            feeds.append(("nsw_rfs", settings.nsw_rfs_fires_url, "nsw_rfs"))

    elif state == "vic":
        if settings.vic_emergency_url:
            feeds.append(("vic_emergency", settings.vic_emergency_url, "vic_emergency"))

    elif state == "sa":
        if settings.sa_cfs_url:
            feeds.append(("sa_cfs", settings.sa_cfs_url, "sa_cfs"))

    elif state == "wa":
        if getattr(settings, "wa_dfes_enabled", False):
            base = (getattr(settings, "wa_dfes_base_url", "") or "").rstrip("/")
            feed_names = [f.strip() for f in (getattr(settings, "wa_dfes_feeds", "") or "").split(",") if f.strip()]
            for fname in feed_names:
                if fname == "incidents":
                    feeds.append(("wa_dfes_incidents", f"{base}/incidents", "wa_dfes_incidents"))
                elif fname == "warnings":
                    feeds.append(("wa_dfes_warnings", f"{base}/warnings", "wa_dfes_warnings"))

    elif state == "nt":
        if settings.nt_traffic_enabled:
            url = getattr(settings, "nt_emergency_announcements_url", "") or ""
            if url:
                feeds.append(("nt_emergency_ann", url, "nt_emergency_ann"))

    elif state == "tas":
        if getattr(settings, "tas_hazards_enabled", False):
            url = getattr(settings, "tas_thelist_url", "") or ""
            if url:
                feeds.append(("tas_thelist", url, "tas_thelist"))
        # TasALERT direct feed - uncomment when email permission is granted:
        # if getattr(settings, "tas_alert_direct_enabled", False):
        #     url = getattr(settings, "tas_alert_direct_url", "") or ""
        #     if url:
        #         feeds.append(("tas_alert_direct", url, "tas_alert_direct"))

    return feeds


# JSON parser dispatch
_JSON_PARSERS: Dict[str, Any] = {
    "nsw_rfs": lambda text: _parse_nsw_rfs_json(text),
    "vic_emergency": lambda text: _parse_vic_emergency_json(text),
    "sa_cfs": lambda text: _parse_sa_cfs_json(text),
    "wa_dfes_incidents": lambda text: _parse_wa_dfes_incidents(text),
    "wa_dfes_warnings": lambda text: _parse_wa_dfes_warnings(text),
    "tas_thelist": lambda text: _parse_tas_thelist_json(text),
    "nt_emergency_ann": lambda text: _parse_nt_emergency_announcements(text),
}


# ══════════════════════════════════════════════════════════════
# National: RADAR roadworks / closures (federal ArcGIS FeatureServer)
# ══════════════════════════════════════════════════════════════

_RADAR_KIND_MAP: Dict[str, str] = {
    "flood": "flood",
    "crash": "road_crash",
    "fire": "fire",
    "planned roadworks": "road_closure",
    "unplanned roadworks": "road_closure",
}

_RADAR_SEVERITY_MAP: Dict[str, str] = {
    "flood": "high",
    "crash": "high",
    "fire": "high",
}


def _radar_kind(updated_category: str) -> str:
    return _RADAR_KIND_MAP.get(updated_category.lower(), "road_closure")


def _radar_severity(category: str) -> str:
    return _RADAR_SEVERITY_MAP.get(category.lower(), "medium")


def _epoch_ms_to_iso(ms: Any) -> Optional[str]:
    if ms is None:
        return None
    try:
        val = float(ms)
        if val > 0:
            return datetime.fromtimestamp(val / 1000, tz=timezone.utc).isoformat()
    except Exception:
        pass
    return None


async def _fetch_radar_roadworks(
    client: httpx.AsyncClient,
    bbox: BBox4,
) -> List[HazardEvent]:
    """Fetch active RADAR roadworks/closures from the federal ArcGIS FeatureServer."""
    if not getattr(settings, "radar_roadworks_enabled", True):
        return []
    url = getattr(settings, "radar_roadworks_url", "") or ""
    if not url:
        return []

    envelope = json.dumps({
        "xmin": bbox.minLng,
        "ymin": bbox.minLat,
        "xmax": bbox.maxLng,
        "ymax": bbox.maxLat,
        "spatialReference": {"wkid": 4326},
    })
    params = {
        "f": "json",
        "where": "status='Active' AND (to_date >= CURRENT_TIMESTAMP OR to_date IS NULL)",
        "geometry": envelope,
        "geometryType": "esriGeometryEnvelope",
        "inSR": "4326",
        "outFields": (
            "street_name,side_street,category,updated_category,"
            "state,from_date,to_date,description,location"
        ),
        "outSR": "4326",
        "returnGeometry": "true",
        "resultRecordCount": "200",
    }

    out: List[HazardEvent] = []
    try:
        r = await client.get(url, params=params, headers={"User-Agent": "roam/hazards"})
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning("hazards:radar_roadworks failed: %s", e)
        return []

    features = data.get("features") or []
    for f in features:
        if not isinstance(f, dict):
            continue
        attrs = f.get("attributes") or {}
        if not isinstance(attrs, dict):
            continue

        street_name = str(attrs.get("street_name") or "").strip()
        category = str(attrs.get("category") or "").strip()
        updated_category = str(attrs.get("updated_category") or "").strip()
        state_val = str(attrs.get("state") or "").strip()
        description = str(attrs.get("description") or "").strip() or None
        from_date = attrs.get("from_date")
        to_date = attrs.get("to_date")

        # Title
        uc = updated_category or category or "Roadworks"
        if street_name:
            title = f"{uc}: {street_name}"
        elif description:
            title = description[:120]
        else:
            title = uc

        kind = _radar_kind(updated_category)
        sev = _radar_severity(category)

        # Geometry: ArcGIS {x, y} point from location field, or feature geometry
        geom: Optional[Dict[str, Any]] = None
        bbox_out: Optional[List[float]] = None

        # Try location attribute first ({x: lng, y: lat})
        loc = attrs.get("location")
        if isinstance(loc, dict):
            lx = _safe_float(loc.get("x"))
            ly = _safe_float(loc.get("y"))
            if lx is not None and ly is not None:
                geom = {"type": "Point", "coordinates": [lx, ly]}
                bbox_out = [lx, ly, lx, ly]

        # Fallback: feature-level geometry (ArcGIS point or rings)
        if not geom:
            raw_geom = f.get("geometry") or {}
            if isinstance(raw_geom, dict):
                gx = _safe_float(raw_geom.get("x"))
                gy = _safe_float(raw_geom.get("y"))
                if gx is not None and gy is not None:
                    geom = {"type": "Point", "coordinates": [gx, gy]}
                    bbox_out = [gx, gy, gx, gy]

        start_at = _epoch_ms_to_iso(from_date)
        end_at = _epoch_ms_to_iso(to_date)

        # Prune expired
        if end_at and _is_expired(end_at):
            continue

        hid = _stable_id([
            "radar_roadworks",
            title[:160],
            state_val,
            str(from_date or ""),
        ])

        out.append(
            HazardEvent(
                id=hid,
                source="radar_national",
                kind=kind,            # type: ignore
                severity=sev,         # type: ignore
                urgency="expected",   # type: ignore
                certainty="observed", # type: ignore
                effective_priority=_compute_effective_priority(
                    "extreme" if kind == "flood" else ("severe" if kind in ("road_crash", "fire") else "moderate"),
                    "expected",
                    "observed",
                ),
                title=title,
                description=description,
                url=None,
                issued_at=None,
                start_at=start_at,
                end_at=end_at,
                geometry=geom,
                bbox=bbox_out,
                region=state_val.lower() if state_val else None,
                raw=attrs,
            )
        )

    return out


# ══════════════════════════════════════════════════════════════
# Park Alerts: QLD Parks + NSW NPWS RSS feeds
# ══════════════════════════════════════════════════════════════

_log = logging.getLogger(__name__)

# Approximate latitude bounds for state overlap checks (minLat, maxLat)
_STATE_LAT_BOUNDS: Dict[str, Tuple[float, float]] = {
    "qld": (-29.0, -10.0),
    "nsw": (-37.5, -28.0),
    "vic": (-39.2, -34.0),
    "sa":  (-38.1, -26.0),
    "nt":  (-26.0, -11.0),
    "tas": (-43.7, -40.5),
    "wa":  (-35.0, -14.0),
}

_PARK_ALERT_MAX_AGE_DAYS = 30


def _strip_html(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text).strip()


def _parse_park_rss(xml_text: str, source: str) -> List[HazardEvent]:
    """Parse a park-alerts RSS 2.0 feed into HazardEvent list."""
    out: List[HazardEvent] = []
    cutoff = datetime.now(tz=timezone.utc) - timedelta(days=_PARK_ALERT_MAX_AGE_DAYS)
    try:
        root = ET.fromstring(xml_text)
    except Exception:
        return out

    for item in root.findall(".//item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip() or None
        raw_desc = (item.findtext("description") or "").strip()
        desc = _strip_html(raw_desc) or None
        pub_raw = (item.findtext("pubDate") or "").strip() or None

        if not title and not desc:
            continue

        # Parse pubDate; skip if older than 30 days
        published_at: Optional[str] = None
        if pub_raw:
            try:
                dt = email.utils.parsedate_to_datetime(pub_raw)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                if dt < cutoff:
                    continue
                published_at = dt.isoformat()
            except Exception:
                pass

        # Classify type
        hay = f"{title} {desc or ''}".lower()
        if any(kw in hay for kw in ("closed", "closure", "access unavailable")):
            kind = "road_closure"
        else:
            kind = "unknown"

        # Severity
        sev = "medium" if "closed" in title.lower() else "low"

        hid = _stable_id([source, title[:160], (link or "")[:160], (published_at or pub_raw or "")[:80]])

        out.append(
            HazardEvent(
                id=hid,
                source=source,
                kind=kind,            # type: ignore
                severity=sev,         # type: ignore
                urgency="expected",   # type: ignore
                certainty="observed", # type: ignore
                effective_priority=_compute_effective_priority(sev, "expected", "observed"),
                title=title or "Park Alert",
                description=desc,
                url=link,
                issued_at=published_at,
                start_at=published_at,
                end_at=None,
                geometry=None,
                bbox=None,
                region=source.split("_")[0],  # "qld" or "nsw"
                raw={},
            )
        )

    return out


def _bbox_overlaps_state(bbox: BBox4, state: str) -> bool:
    """Return True if bbox latitudes overlap the approximate lat band for state."""
    bounds = _STATE_LAT_BOUNDS.get(state)
    if bounds is None:
        return False
    state_min_lat, state_max_lat = bounds
    return not (bbox.maxLat < state_min_lat or bbox.minLat > state_max_lat)


async def _fetch_park_alerts(client: httpx.AsyncClient, bbox: BBox4) -> List[HazardEvent]:
    """Fetch park alert RSS feeds for all enabled states concurrently."""
    sources: List[Tuple[str, str]] = []  # (source_label, url)

    if settings.parks_qld_alerts_enabled and _bbox_overlaps_state(bbox, "qld"):
        url = settings.parks_qld_alerts_url or ""
        if url:
            sources.append(("qld_parks", url))

    if settings.parks_nsw_alerts_enabled and _bbox_overlaps_state(bbox, "nsw"):
        url = settings.parks_nsw_alerts_url or ""
        if url:
            sources.append(("nsw_npws", url))

    if settings.parks_vic_alerts_enabled and _bbox_overlaps_state(bbox, "vic"):
        url = settings.parks_vic_alerts_url or ""
        if url:
            sources.append(("vic_parks", url))

    if settings.parks_sa_alerts_enabled and _bbox_overlaps_state(bbox, "sa"):
        url = settings.parks_sa_alerts_url or ""
        if url:
            sources.append(("sa_parks", url))

    if settings.parks_nt_alerts_enabled and _bbox_overlaps_state(bbox, "nt"):
        url = settings.parks_nt_alerts_url or ""
        if url:
            sources.append(("nt_parks", url))

    if settings.parks_tas_alerts_enabled and _bbox_overlaps_state(bbox, "tas"):
        url = settings.parks_tas_alerts_url or ""
        if url:
            sources.append(("tas_parks", url))

    if not sources:
        return []

    async def _fetch_one(source: str, url: str) -> List[HazardEvent]:
        try:
            r = await client.get(url, headers={"User-Agent": "roam/hazards"})
            r.raise_for_status()
            ct = r.headers.get("content-type", "")
            text = r.text
            # Reject HTML responses silently - the URL is misconfigured
            if "html" in ct.lower() or (text.lstrip()[:1] not in ("<", "")):
                _log.warning("hazards:park_alerts:%s returned non-RSS content (content-type=%s)", source, ct)
                return []
            return _parse_park_rss(text, source)
        except Exception as e:
            _log.warning("hazards:park_alerts:%s failed: %s", source, e)
            return []

    results = await asyncio.gather(*[_fetch_one(src, u) for src, u in sources])
    out: List[HazardEvent] = []
    for batch in results:
        out.extend(batch)
    return out


# ══════════════════════════════════════════════════════════════
# Main Hazards service - state-aware orchestrator
# ══════════════════════════════════════════════════════════════

class Hazards:
    def __init__(self, *, conn):
        self.conn = conn

    async def poll(
        self,
        *,
        bbox: BBox4,
        sources: Optional[List[str]] = None,
        cache_seconds: int | None = None,
        timeout_s: float | None = None,
    ) -> HazardOverlay:
        algo_version = settings.hazards_algo_version
        max_age = int(cache_seconds or settings.overlays_cache_seconds)
        timeout = float(timeout_s or settings.overlays_timeout_s)

        # Determine which states the bbox overlaps
        active_states = states_for_bbox(bbox)
        # ACT is covered by NSW
        if "act" in active_states and "nsw" not in active_states:
            active_states.append("nsw")
            active_states.sort()

        hazards_key = stable_key(
            "hazards",
            {
                "bbox": bbox.model_dump(),
                "states": active_states,
                "algo_version": algo_version,
            },
        )

        # SQLite cache hit
        cached = get_hazards_pack(self.conn, hazards_key)
        if cached:
            try:
                pack = HazardOverlay.model_validate(cached)
                if is_fresh(pack.created_at, max_age_s=max_age):
                    return pack
            except Exception:
                pass

        warnings_out: List[str] = []
        items: List[HazardEvent] = []

        if not active_states:
            pack = HazardOverlay(
                hazards_key=hazards_key,
                bbox=bbox,
                provider="no_states",
                algo_version=algo_version,
                created_at=utc_now_iso(),
                items=[],
                warnings=["No Australian states overlap this bbox."],
            )
            put_hazards_pack(
                self.conn,
                hazards_key=hazards_key,
                created_at=pack.created_at,
                algo_version=algo_version,
                pack=pack.model_dump(),
            )
            return pack

        # Heuristic: allow non-spatial warnings only if bbox is "big enough"
        dx = bbox.maxLng - bbox.minLng
        dy = bbox.maxLat - bbox.minLat
        bbox_diag = (dx * dx + dy * dy) ** 0.5

        def _filter_events(evs: List[HazardEvent]) -> List[HazardEvent]:
            out: List[HazardEvent] = []
            for ev in evs:
                if ev.bbox and len(ev.bbox) == 4:
                    bb: BBox = (float(ev.bbox[0]), float(ev.bbox[1]), float(ev.bbox[2]), float(ev.bbox[3]))
                    if _bbox_intersects(bb, bbox):
                        out.append(ev)
                elif bbox_diag >= 0.35:
                    out.append(ev)
            return out

        transport = httpx.AsyncHTTPTransport(retries=1)
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True, transport=transport) as client:

            # Build all fetch coroutines upfront so they can run concurrently.
            async def _fetch_bom_rss(state: str, url: str) -> List[HazardEvent]:
                try:
                    r = await client.get(url, headers={"User-Agent": "roam/hazards"})
                    r.raise_for_status()
                    return _filter_events(_parse_rss(r.text, f"bom_rss_{state}", region=state))
                except Exception as e:
                    warnings_out.append(f"hazards:bom_rss_{state} failed: {e}")
                    return []

            async def _fetch_cap(cap_name: str, cap_url: str, state: str) -> List[HazardEvent]:
                try:
                    r = await client.get(cap_url, headers={"User-Agent": "roam/hazards"})
                    r.raise_for_status()
                    text = r.text
                    evs = _parse_cap(text, cap_name, region=state)
                    if not evs:
                        evs = _parse_rss(text, cap_name, region=state)
                    return _filter_events(evs)
                except Exception as e:
                    warnings_out.append(f"hazards:{cap_name} failed: {e}")
                    return []

            async def _fetch_json_feed(json_name: str, json_url: str, parser_key: str, state: str) -> List[HazardEvent]:
                parser = _JSON_PARSERS.get(parser_key)
                if not parser:
                    return []
                try:
                    r = await client.get(json_url, headers={"User-Agent": "roam/hazards"})
                    r.raise_for_status()
                    return _filter_events(parser(r.text))
                except Exception as e:
                    warnings_out.append(f"hazards:{json_name} failed: {e}")
                    return []

            async def _fetch_dea() -> List[HazardEvent]:
                if not getattr(settings, "dea_hotspots_enabled", False):
                    return []
                dea_url = getattr(settings, "dea_hotspots_url", "") or ""
                if not dea_url:
                    return []
                try:
                    r = await client.get(dea_url, headers={"User-Agent": "roam/hazards"})
                    r.raise_for_status()
                    return _parse_dea_hotspots_json(r.text, bbox=bbox)
                except Exception as e:
                    warnings_out.append(f"hazards:dea_hotspots failed: {e}")
                    return []

            # Collect every coroutine for all states + DEA + RADAR + park alerts into one gather call.
            coros = []
            for state in active_states:
                rss_url = _bom_rss_url_for_state(state)
                if rss_url:
                    coros.append(_fetch_bom_rss(state, rss_url))
                for cap_name, cap_url in _cap_feeds_for_state(state):
                    coros.append(_fetch_cap(cap_name, cap_url, state))
                for json_name, json_url, parser_key in _json_feeds_for_state(state):
                    coros.append(_fetch_json_feed(json_name, json_url, parser_key, state))
            coros.append(_fetch_dea())
            coros.append(_fetch_radar_roadworks(client, bbox))
            coros.append(_fetch_park_alerts(client, bbox))

            all_results = await asyncio.gather(*coros)
            for batch in all_results:
                items.extend(batch)

        # Dedup by id
        dedup: Dict[str, HazardEvent] = {}
        for it in items:
            dedup[it.id] = it

        provider_str = "+".join(active_states) if dedup else "empty"
        if getattr(settings, "dea_hotspots_enabled", False):
            provider_str += "+dea"
        if getattr(settings, "radar_roadworks_enabled", True):
            provider_str += "+radar"
        if any([
            settings.parks_qld_alerts_enabled,
            settings.parks_nsw_alerts_enabled,
            settings.parks_vic_alerts_enabled,
            settings.parks_sa_alerts_enabled,
            settings.parks_nt_alerts_enabled,
            settings.parks_tas_alerts_enabled,
        ]):
            provider_str += "+parks"

        pack = HazardOverlay(
            hazards_key=hazards_key,
            bbox=bbox,
            provider=provider_str,
            algo_version=algo_version,
            created_at=utc_now_iso(),
            items=list(dedup.values()),
            warnings=warnings_out,
        )

        put_hazards_pack(
            self.conn,
            hazards_key=hazards_key,
            created_at=pack.created_at,
            algo_version=algo_version,
            pack=pack.model_dump(),
        )
        return pack
