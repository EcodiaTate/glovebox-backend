from __future__ import annotations

from typing import Any, Dict, List, Optional, Set, Tuple
import asyncio
import collections
import concurrent.futures
import hashlib
import logging
import math
import time
import random
import re
import functools

import httpx

from app.core.contracts import PlaceItem, PlacesPack, PlacesRequest, BBox4, PlaceCategory, StopSuggestionItem
from app.core.keying import places_key
from app.core.time import utc_now_iso
from app.core.storage import get_places_pack, put_places_pack
from app.core.settings import settings
from app.core.polyline6 import decode_polyline6
from app.core.http_client import http_client

from app.services.places_store import PlacesStore
from app.services.places_supa import SupaPlacesRepo

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────
# Geometry helpers
# ──────────────────────────────────────────────────────────────
def _bbox_from_req(req: PlacesRequest) -> Optional[BBox4]:
    if req.bbox:
        return req.bbox
    if req.center and req.radius_m:
        dlat = req.radius_m / 111_320.0
        cosv = max(0.2, math.cos(math.radians(req.center.lat)))
        dlng = req.radius_m / (111_320.0 * cosv)
        return BBox4(
            minLng=req.center.lng - dlng,
            minLat=req.center.lat - dlat,
            maxLng=req.center.lng + dlng,
            maxLat=req.center.lat + dlat,
        )
    return None


def _haversine_m(a: Tuple[float, float], b: Tuple[float, float]) -> float:
    """Distance in metres between two (lat, lng) points."""
    R = 6_371_000.0
    lat1, lon1 = math.radians(a[0]), math.radians(a[1])
    lat2, lon2 = math.radians(b[0]), math.radians(b[1])
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    x = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2.0 * R * math.asin(min(1.0, math.sqrt(x)))


def _bbox_around_points(
    points: List[Tuple[float, float]], buffer_km: float
) -> BBox4:
    """Build a tight BBox4 around a set of (lat, lng) points with a km buffer."""
    lats = [p[0] for p in points]
    lngs = [p[1] for p in points]
    min_lat, max_lat = min(lats), max(lats)
    min_lng, max_lng = min(lngs), max(lngs)

    buf_deg_lat = buffer_km / 111.32
    center_lat = (min_lat + max_lat) / 2.0
    cos_v = max(0.2, math.cos(math.radians(center_lat)))
    buf_deg_lng = buffer_km / (111.32 * cos_v)

    return BBox4(
        minLat=min_lat - buf_deg_lat,
        maxLat=max_lat + buf_deg_lat,
        minLng=min_lng - buf_deg_lng,
        maxLng=max_lng + buf_deg_lng,
    )


def _min_distance_to_samples_m(
    lat: float, lng: float, samples: List[Tuple[float, float]]
) -> float:
    best = float("inf")
    for s in samples:
        d = _haversine_m((lat, lng), s)
        if d < best:
            best = d
            if d < 500.0:
                break
    return best


# ──────────────────────────────────────────────────────────────
# Route sampling (shared between corridor + suggest)
# ──────────────────────────────────────────────────────────────

def _sample_polyline(
    poly6: str, interval_km: float, *, include_endpoints: bool = True
) -> List[Tuple[float, float]]:
    pts = decode_polyline6(poly6)

    logger.debug(
        "_sample_polyline polyline_chars=%d decoded_points=%d interval_km=%s",
        len(poly6), len(pts), interval_km,
    )

    if not pts or len(pts) < 2:
        logger.debug("_sample_polyline: fewer than 2 decoded points, returning empty")
        return []

    interval_m = max(1000.0, interval_km * 1000.0)
    samples: List[Tuple[float, float]] = []
    if include_endpoints:
        samples.append((float(pts[0][0]), float(pts[0][1])))

    dist_acc = 0.0
    next_mark = interval_m
    zero_segs = 0
    nan_segs = 0

    for i in range(1, len(pts)):
        p0 = (float(pts[i - 1][0]), float(pts[i - 1][1]))
        p1 = (float(pts[i][0]), float(pts[i][1]))
        seg = _haversine_m(p0, p1)

        if seg != seg:  # NaN check
            nan_segs += 1
            continue

        if seg <= 0:
            zero_segs += 1
            continue

        while dist_acc + seg >= next_mark:
            overshoot = next_mark - dist_acc
            t = max(0.0, min(1.0, overshoot / seg))
            lat = p0[0] + (p1[0] - p0[0]) * t
            lng = p0[1] + (p1[1] - p0[1]) * t
            samples.append((lat, lng))
            next_mark += interval_m

        dist_acc += seg

    if include_endpoints:
        last = (float(pts[-1][0]), float(pts[-1][1]))
        if not samples or _haversine_m(samples[-1], last) > 500.0:
            samples.append(last)

    logger.debug(
        "_sample_polyline result: samples=%d total_dist_km=%.2f zero_segs=%d nan_segs=%d",
        len(samples), dist_acc / 1000, zero_segs, nan_segs,
    )

    expected_min = max(2, int(dist_acc / 1000.0 / interval_km) - 1)
    if len(samples) < expected_min and dist_acc > interval_m * 2:
        logger.warning(
            "_sample_polyline: expected ~%d samples but got %d - falling back to uniform pick",
            expected_min, len(samples),
        )
        n_want = max(2, int(dist_acc / 1000.0 / interval_km) + 2)
        step = max(1, len(pts) // n_want)
        samples = [(float(pts[j][0]), float(pts[j][1])) for j in range(0, len(pts), step)]
        last = (float(pts[-1][0]), float(pts[-1][1]))
        if _haversine_m(samples[-1], last) > 500.0:
            samples.append(last)
        logger.debug("_sample_polyline fallback produced %d samples", len(samples))

    return samples


def _sample_route_points(poly6: str, interval_km: int) -> List[Tuple[int, float, float, float]]:
    """Legacy wrapper kept for suggest_along_route compatibility."""
    pts = decode_polyline6(poly6)
    if not pts or len(pts) < 2:
        return []

    interval_m = max(5000.0, float(interval_km) * 1000.0)
    samples: List[Tuple[int, float, float, float]] = []
    dist_acc = 0.0
    next_mark = 0.0

    samples.append((0, float(pts[0][0]), float(pts[0][1]), 0.0))

    for i in range(1, len(pts)):
        p0 = (float(pts[i - 1][0]), float(pts[i - 1][1]))
        p1 = (float(pts[i][0]), float(pts[i][1]))
        seg = _haversine_m(p0, p1)
        if seg <= 0:
            continue

        while dist_acc + seg >= next_mark + interval_m:
            target = (next_mark + interval_m) - dist_acc
            t = max(0.0, min(1.0, target / seg))
            lat = p0[0] + (p1[0] - p0[0]) * t
            lng = p0[1] + (p1[1] - p0[1]) * t
            km_from_start = (next_mark + interval_m) / 1000.0
            idx = len(samples)
            samples.append((idx, lat, lng, km_from_start))
            next_mark += interval_m

        dist_acc += seg

    return samples


# ──────────────────────────────────────────────────────────────
# Overpass mapping + classification
# ──────────────────────────────────────────────────────────────

_FALLBACK_FILTERS: Dict[str, List[str]] = {
    # ── ESSENTIALS & SAFETY ──────────────────────────────────
    "fuel": [
        '["amenity"="fuel"]',
        '["amenity"="fuel"]["fuel:diesel"="yes"]',
        '["amenity"="fuel"]["fuel:lpg"="yes"]',
    ],
    "ev_charging": [
        '["amenity"="charging_station"]',
    ],
    "rest_area": [
        '["highway"="rest_area"]',
        '["highway"="services"]',
        '["amenity"="rest_area"]',
        '["highway"="layby"]',
    ],
    "toilet": [
        '["amenity"="toilets"]',
    ],
    "water": [
        '["amenity"="drinking_water"]',
        '["man_made"="water_well"]',
        '["man_made"="water_tap"]',
        '["amenity"="water_point"]',
        '["natural"="spring"]["drinking_water"="yes"]',
    ],
    "dump_point": [
        '["amenity"="sanitary_dump_station"]',
        '["amenity"="waste_disposal"]["waste"~"grey_water|black_water"]',
    ],
    "water_fill": [
        '["amenity"="water_point"]',
        '["amenity"="drinking_water"]',
    ],
    "shower": [
        '["amenity"="shower"]',
        '["shower"="yes"]',
    ],
    "mechanic": [
        '["shop"="car_repair"]',
        '["amenity"="car_repair"]',
        '["shop"="tyres"]',
        '["amenity"="car_wash"]',
        '["shop"="car_repair"]["service"~"tyre|tire|mechanical"]',
    ],
    "hospital": [
        '["amenity"="hospital"]',
        '["amenity"="clinic"]',
    ],
    "pharmacy": [
        '["amenity"="pharmacy"]',
    ],
    "emergency_phone": [
        '["amenity"="emergency_phone"]',
        '["emergency"="phone"]',
        '["amenity"="ranger_station"]',
    ],

    # ── SUPPLIES ──────────────────────────────────────────────
    "grocery": [
        '["shop"="supermarket"]',
        '["shop"="convenience"]',
        '["shop"="general"]',
    ],
    "town": [
        '["place"~"^(city|town|village|hamlet)$"]',
    ],
    "atm": [
        '["amenity"="atm"]',
        '["amenity"="bank"]',
    ],
    "laundromat": [
        '["shop"="laundry"]',
        '["amenity"="laundry"]',
    ],

    # ── FOOD & DRINK ─────────────────────────────────────────
    "bakery": [
        '["shop"="bakery"]',
    ],
    "cafe": [
        '["amenity"="cafe"]',
    ],
    "restaurant": [
        '["amenity"="restaurant"]',
    ],
    "fast_food": [
        '["amenity"="fast_food"]',
    ],
    "pub": [
        '["amenity"="pub"]',
    ],
    "bar": [
        '["amenity"="bar"]',
    ],

    # ── ACCOMMODATION ─────────────────────────────────────────
    "camp": [
        '["tourism"="camp_site"]',
        '["tourism"="caravan_site"]',
        '["tourism"="caravan_park"]',
        '["tourism"="holiday_park"]',
        '["tourism"="camp_pitch"]',
        '["tourism"="alpine_hut"]',
        '["amenity"="camping"]',
        '["landuse"="camping"]',
        '["camping"="yes"]',
        '["leisure"="holiday_park"]',
        '["brand"~"Big4|BIG4|Top Tourist|Discovery Parks|G\'Day Parks"]',
        '["highway"="rest_area"]["camping"="yes"]',
        '["amenity"="parking"]["camping"="yes"]',
        # Free / low-cost specific
        '["tourism"="camp_site"]["fee"="no"]',
        '["tourism"="caravan_site"]["fee"="no"]',
        # Informal bush camping (parking areas used as camping)
        '["amenity"="parking"]["tourism"="camp_site"]',
        # Showgrounds / recreation grounds
        '["leisure"="recreation_ground"]["camping"="yes"]',
        '["landuse"="recreation_ground"]["name"~"[Ss]howground"]',
        # Station / farm stays
        '["tourism"="camp_site"]["operator:type"~"farm|station|pastoral"]',
    ],
    "hotel": [
        '["tourism"="hotel"]',
    ],
    "motel": [
        '["tourism"="motel"]',
    ],
    "hostel": [
        '["tourism"="hostel"]',
    ],

    # ── NATURE & OUTDOORS ────────────────────────────────────
    "viewpoint": [
        '["tourism"="viewpoint"]',
    ],
    "waterfall": [
        '["waterway"="waterfall"]',
    ],
    "swimming_hole": [
        '["leisure"="swimming_area"]',
        '["natural"="water"]["sport"="swimming"]',
        '["leisure"="swimming_pool"]["access"~"^(yes|public)$"]',
        '["natural"="spring"]["bathing"="yes"]',
        '["leisure"="bathing_place"]',
    ],
    "beach": [
        '["natural"="beach"]',
        '["leisure"="beach_resort"]',
    ],
    "national_park": [
        '["boundary"="national_park"]',
        '["leisure"="nature_reserve"]',
    ],
    "hiking": [
        '["highway"="path"]["foot"="designated"]',
        '["highway"="path"]["sac_scale"]',
        '["highway"="footway"]["designation"~"walking_track|bushwalking"]',
        '["route"="hiking"]',
        '["route"="foot"]',
        '["information"="guidepost"]',
        '["tourism"="information"]["information"="route_marker"]',
        '["tourism"="wilderness_hut"]',
    ],
    "picnic": [
        '["tourism"="picnic_site"]',
        '["leisure"="picnic_table"]',
        '["amenity"="bbq"]',
    ],
    "hot_spring": [
        '["natural"="hot_spring"]',
        '["leisure"="hot_spring"]',
        '["bath:type"="hot_spring"]',
    ],
    "cave": [
        '["natural"="cave_entrance"]',
        '["tourism"="attraction"]["cave"]',
    ],
    "fishing": [
        '["leisure"="fishing"]',
        '["sport"="fishing"]',
        '["leisure"="slipway"]',
    ],
    "surf": [
        '["sport"="surfing"]',
        '["leisure"="surfing"]',
    ],

    # ── FAMILY & RECREATION ──────────────────────────────────
    "playground": [
        '["leisure"="playground"]',
    ],
    "pool": [
        '["leisure"="swimming_pool"]["access"~"^(yes|public)$"]',
        '["leisure"="water_park"]',
        '["amenity"="public_bath"]',
    ],
    "zoo": [
        '["tourism"="zoo"]',
        '["attraction"="animal"]',
        '["zoo"="petting_zoo"]',
        '["tourism"="aquarium"]',
        '["attraction"="maze"]',
    ],
    "theme_park": [
        '["tourism"="theme_park"]',
        '["leisure"="amusement_arcade"]',
        '["leisure"="miniature_golf"]',
        '["leisure"="trampoline_park"]',
        '["sport"="karting"]',
    ],
    "dog_park": [
        '["leisure"="dog_park"]',
    ],
    "golf": [
        '["leisure"="golf_course"]',
    ],
    "cinema": [
        '["amenity"="cinema"]',
    ],

    # ── CULTURE & SIGHTSEEING ────────────────────────────────
    "visitor_info": [
        '["tourism"="information"]["information"="office"]',
        '["tourism"="information"]["information"="visitor_centre"]',
        '["amenity"="ranger_station"]',
    ],
    "museum": [
        '["tourism"="museum"]',
    ],
    "gallery": [
        '["tourism"="gallery"]',
    ],
    "heritage": [
        '["heritage"]',
        '["historic"="monument"]',
        '["historic"="memorial"]',
        '["historic"="ruins"]',
        '["historic"="castle"]',
        '["historic"="fort"]',
        '["historic"="archaeological_site"]',
        '["historic"="wreck"]',
        '["historic"="mine"]',
        '["historic"="mine_shaft"]',
        '["historic"="bridge"]',
    ],
    "winery": [
        '["craft"="winery"]',
        '["tourism"="wine_cellar"]',
        '["shop"="wine"]',
    ],
    "brewery": [
        '["craft"="brewery"]',
        '["craft"="distillery"]',
        '["craft"="cider"]',
        '["microbrewery"="yes"]',
    ],
    "attraction": [
        '["tourism"="attraction"]',
        '["tourism"="artwork"]',
        '["tourism"="aquarium"]',
    ],
    "market": [
        '["amenity"="marketplace"]',
        '["shop"="farm"]',
        '["shop"="deli"]',
        '["shop"="greengrocer"]',
    ],
    "park": [
        '["leisure"="park"]',
        '["leisure"="garden"]',
    ],
    "library": [
        '["amenity"="library"]',
    ],
    "showground": [
        '["leisure"="showground"]',
        '["leisure"="horse_racing"]',
    ],
}


def _category_filters(category: str) -> List[str]:
    m = getattr(settings, "places_overpass_filters", None)
    if isinstance(m, dict):
        v = m.get(category)
        if isinstance(v, list) and v:
            return [str(x) for x in v]
    return _FALLBACK_FILTERS.get(category, [])


def _overpass_filters_for_categories(cats: List[PlaceCategory]) -> List[str]:
    out: List[str] = []
    for c in cats:
        out.extend(_category_filters(str(c)))

    seen = set()
    dedup: List[str] = []
    for f in out:
        if f not in seen:
            seen.add(f)
            dedup.append(f)
    return dedup


# ──────────────────────────────────────────────────────────────
# Category inference from OSM tags
# ──────────────────────────────────────────────────────────────

def _infer_category(tags: Dict[str, Any]) -> PlaceCategory:
    a = tags.get("amenity", "")
    t = tags.get("tourism", "")
    p = tags.get("place", "")
    s = tags.get("shop", "")
    mm = tags.get("man_made", "")
    le = tags.get("leisure", "")
    n = tags.get("natural", "")
    w = tags.get("waterway", "")
    b = tags.get("boundary", "")
    hw = tags.get("highway", "")
    cr = tags.get("craft", "")
    hi = tags.get("historic", "")
    info = tags.get("information", "")

    # ── ESSENTIALS & SAFETY (highest priority) ───────────────

    if a == "fuel":
        return "fuel"
    if a == "charging_station":
        return "ev_charging"
    # rest_area: camping=yes on a rest_area → camp beats rest_area
    if (hw in ("rest_area", "services", "layby") or a == "rest_area") and tags.get("camping") != "yes":
        return "rest_area"
    if a == "toilets":
        return "toilet"
    if a == "shower" or tags.get("shower") == "yes":
        return "shower"
    if a == "water_point":
        return "water_fill"
    if a in ("drinking_water",) or mm in ("water_well", "water_tap") or (n == "spring" and tags.get("drinking_water") == "yes"):
        return "water"
    if a == "sanitary_dump_station" or (a == "waste_disposal" and tags.get("waste", "") in ("grey_water", "black_water")):
        return "dump_point"
    if s == "car_repair" or a in ("car_repair", "car_wash") or s == "tyres":
        return "mechanic"
    if a in ("hospital", "clinic"):
        return "hospital"
    if a == "pharmacy":
        return "pharmacy"
    if a in ("emergency_phone", "ranger_station") or tags.get("emergency") == "phone":
        return "emergency_phone"

    # ── SUPPLIES ──────────────────────────────────────────────

    if s in ("supermarket", "convenience", "general"):
        return "grocery"
    if a == "atm" or a == "bank":
        return "atm"
    if s == "laundry" or a == "laundry":
        return "laundromat"

    # ── FOOD & DRINK ─────────────────────────────────────────

    if s == "bakery":
        return "bakery"
    if a == "cafe":
        return "cafe"
    if a == "restaurant":
        return "restaurant"
    if a == "fast_food":
        return "fast_food"
    if a == "pub":
        return "pub"
    if a == "bar":
        return "bar"

    # ── ACCOMMODATION ─────────────────────────────────────────

    if t in ("camp_site", "caravan_site", "caravan_park", "holiday_park", "camp_pitch", "alpine_hut"):
        return "camp"
    if le == "holiday_park":
        return "camp"
    if a == "camping" or tags.get("landuse") == "camping" or tags.get("camping") == "yes":
        return "camp"
    if tags.get("brand") and any(b in tags["brand"] for b in ("Big4", "BIG4", "Top Tourist", "Discovery Parks", "G'Day Parks")):
        return "camp"
    if t == "motel":
        return "motel"
    if t == "hotel":
        return "hotel"
    if t == "hostel":
        return "hostel"

    # ── NATURE & OUTDOORS (before generic attraction) ────────

    if w == "waterfall":
        return "waterfall"
    if n == "hot_spring" or le == "hot_spring" or tags.get("bath:type") == "hot_spring":
        return "hot_spring"
    if le in ("swimming_area", "bathing_place") or (tags.get("sport") == "swimming" and n) or (n == "spring" and tags.get("bathing") == "yes"):
        return "swimming_hole"
    if n == "beach" or le == "beach_resort":
        return "beach"
    if b == "national_park" or le == "nature_reserve":
        return "national_park"
    if t == "viewpoint":
        return "viewpoint"
    if t == "picnic_site" or le == "picnic_table" or a == "bbq":
        return "picnic"
    if (
        tags.get("route") in ("hiking", "foot")
        or (hw == "path" and tags.get("sac_scale"))
        or (hw == "path" and tags.get("foot") == "designated")
        or info == "guidepost"
        or (t == "information" and info == "route_marker")
        or t == "wilderness_hut"
    ):
        return "hiking"
    if n == "cave_entrance":
        return "cave"
    if le == "fishing" or tags.get("sport") == "fishing":
        return "fishing"
    if le == "slipway":
        return "fishing"  # boat ramps → fishing category
    if tags.get("sport") == "surfing" or le == "surfing":
        return "surf"

    # ── FAMILY & RECREATION ──────────────────────────────────

    if le == "playground":
        return "playground"
    if le in ("swimming_pool", "water_park") or a == "public_bath":
        return "pool"
    if t == "zoo" or tags.get("attraction") == "animal" or tags.get("zoo") == "petting_zoo" or tags.get("attraction") == "maze":
        return "zoo"
    if t == "theme_park" or le in ("amusement_arcade", "miniature_golf", "trampoline_park") or tags.get("sport") == "karting":
        return "theme_park"
    if le == "dog_park":
        return "dog_park"
    if le == "golf_course":
        return "golf"
    if a == "cinema":
        return "cinema"

    # ── CULTURE & SIGHTSEEING ────────────────────────────────

    if t == "information" and info in ("office", "visitor_centre"):
        return "visitor_info"
    if cr == "winery" or t == "wine_cellar" or s == "wine":
        return "winery"
    if cr in ("brewery", "distillery", "cider") or tags.get("microbrewery") == "yes":
        return "brewery"
    if t == "museum":
        return "museum"
    if t == "gallery":
        return "gallery"
    if tags.get("heritage") or hi in ("monument", "memorial", "ruins", "castle", "fort", "archaeological_site", "wreck", "mine", "mine_shaft", "bridge"):
        return "heritage"
    if a == "marketplace" or s in ("farm", "deli", "greengrocer"):
        return "market"
    if le in ("park", "garden"):
        return "park"
    if t in ("attraction", "artwork"):
        return "attraction"
    if t == "aquarium":
        return "zoo"
    if a == "library":
        return "library"
    if le in ("showground", "horse_racing"):
        return "showground"

    # ── ANCHOR POINTS ────────────────────────────────────────

    if p in ("city", "town", "village", "hamlet"):
        return "town"

    return "town"


# ──────────────────────────────────────────────────────────────
# Synthetic name generation for nameless OSM features
# ──────────────────────────────────────────────────────────────

_CATEGORY_LABELS: Dict[str, str] = {
    "fuel": "Fuel Station",
    "ev_charging": "EV Charger",
    "rest_area": "Rest Area",
    "toilet": "Public Toilet",
    "water": "Drinking Water",
    "dump_point": "Dump Point",
    "water_fill": "Water Fill",
    "shower": "Public Shower",
    "mechanic": "Mechanic",
    "hospital": "Hospital",
    "pharmacy": "Pharmacy",
    "emergency_phone": "Emergency Phone",
    "grocery": "Grocery",
    "town": "Town",
    "atm": "ATM",
    "laundromat": "Laundromat",
    "bakery": "Bakery",
    "cafe": "Café",
    "restaurant": "Restaurant",
    "fast_food": "Fast Food",
    "pub": "Pub",
    "bar": "Bar",
    "camp": "Campground",
    "hotel": "Hotel",
    "motel": "Motel",
    "hostel": "Hostel",
    "viewpoint": "Viewpoint",
    "waterfall": "Waterfall",
    "swimming_hole": "Swimming Hole",
    "beach": "Beach",
    "national_park": "National Park",
    "hiking": "Walking Track",
    "picnic": "Picnic Area",
    "hot_spring": "Hot Spring",
    "playground": "Playground",
    "pool": "Swimming Pool",
    "zoo": "Zoo",
    "theme_park": "Theme Park",
    "visitor_info": "Visitor Info",
    "museum": "Museum",
    "gallery": "Gallery",
    "heritage": "Heritage Site",
    "winery": "Winery",
    "brewery": "Brewery",
    "attraction": "Attraction",
    "market": "Market",
    "park": "Park",
    "cave": "Cave",
    "fishing": "Fishing Spot",
    "surf": "Surf Spot",
    "dog_park": "Dog Park",
    "golf": "Golf Course",
    "cinema": "Cinema",
    "library": "Library",
    "showground": "Showground",
}


def _synthetic_name(
    category: str,
    tags: Dict[str, Any],
    lat: float,
    lon: float,
) -> str:
    base = _CATEGORY_LABELS.get(category, category.replace("_", " ").title())

    locality = (
        tags.get("addr:suburb")
        or tags.get("addr:city")
        or tags.get("addr:state")
    )
    street = tags.get("addr:street")

    if category == "picnic":
        if tags.get("amenity") == "bbq":
            base = "BBQ"
        elif tags.get("leisure") == "picnic_table":
            base = "Picnic Table"
    elif category == "water":
        if tags.get("man_made") == "water_well":
            base = "Water Well"
        elif tags.get("man_made") == "water_tap":
            base = "Water Tap"
    elif category == "camp":
        t_val = tags.get("tourism", "")
        le_val = tags.get("leisure", "")
        if t_val in ("caravan_site", "caravan_park") or le_val == "holiday_park" or t_val == "holiday_park":
            base = "Caravan Park"
        elif t_val == "camp_pitch":
            base = "Camp Pitch"
        elif t_val == "alpine_hut":
            base = "Alpine Hut"
        elif tags.get("amenity") == "camping" or tags.get("landuse") == "camping":
            base = "Camping Area"
        if tags.get("fee") == "no":
            base = f"Free {base}"
    elif category == "emergency_phone":
        if tags.get("amenity") == "ranger_station":
            base = "Ranger Station"
        elif tags.get("amenity") == "emergency_phone" or tags.get("emergency") == "phone":
            base = "Emergency Phone"
    elif category == "hiking":
        if tags.get("route") in ("hiking", "foot"):
            base = "Walking Trail"
        elif tags.get("information") == "guidepost":
            base = "Trail Marker"
        elif tags.get("tourism") == "wilderness_hut":
            base = "Bush Hut"
    elif category == "heritage":
        ht = tags.get("historic", "")
        if ht == "monument":
            base = "Monument"
        elif ht == "memorial":
            base = "Memorial"
        elif ht == "ruins":
            base = "Ruins"
        elif ht == "mine" or ht == "mine_shaft":
            base = "Historic Mine"
        elif ht == "wreck":
            base = "Shipwreck"
        elif ht == "fort":
            base = "Fort"
        elif ht == "archaeological_site":
            base = "Archaeological Site"
        elif ht == "bridge":
            base = "Historic Bridge"
    elif category == "fishing":
        if tags.get("leisure") == "slipway":
            base = "Boat Ramp"
    elif category == "toilet":
        if tags.get("access") == "customers":
            base = "Customer Toilet"
        if tags.get("wheelchair") == "yes":
            base = f"{base} (Accessible)"

    if locality:
        return f"{base} - {locality}"
    elif street:
        return f"{base} - {street}"
    else:
        return base


def _enrich_camp_tags(tags: Dict[str, Any], extra: Dict[str, Any]) -> None:
    """Map OSM camp tags → typed PlaceExtra camping fields.

    Defensive: unknown / inconsistent values are silently omitted.
    Only sets a field when we have a confident positive signal.
    """
    import re

    # ── Site types & configuration ────────────────────────────

    # Pets / dogs - normalised to "yes" | "leashed" | "no"
    dog = tags.get("dog") or tags.get("dogs") or tags.get("pets")
    if dog in ("yes",):
        extra["pets_allowed"] = "yes"
    elif dog in ("leashed", "on_lead", "on lead"):
        extra["pets_allowed"] = "leashed"
    elif dog == "no":
        extra["pets_allowed"] = "no"

    # Open fires
    openfire = tags.get("openfire") or tags.get("open_fire") or tags.get("campfire")
    if openfire == "yes":
        extra["fires_allowed"] = True
    elif openfire in ("seasonal", "permitted_in_season"):
        extra["fires_allowed"] = "seasonal"
    elif openfire == "no":
        extra["fires_allowed"] = False

    # Generators
    generator = tags.get("generator") or tags.get("generators")
    if generator == "yes":
        extra["generators_allowed"] = True
    elif generator in ("hours_only", "limited_hours"):
        extra["generators_allowed"] = "hours_only"
    elif generator == "no":
        extra["generators_allowed"] = False

    # Vehicle type acceptance
    if tags.get("caravans") == "yes":
        extra["caravans"] = True
    elif tags.get("caravans") == "no":
        extra["caravans"] = False
    # caravan_site tourism type implies caravans accepted
    if tags.get("tourism") == "caravan_site" and "caravans" not in extra:
        extra["caravans"] = True

    if tags.get("motorhome") == "yes" or tags.get("motorhomes") == "yes":
        extra["motorhomes"] = True
    elif tags.get("motorhome") == "no" or tags.get("motorhomes") == "no":
        extra["motorhomes"] = False

    if tags.get("tents") == "yes":
        extra["tents"] = True
    elif tags.get("tents") == "no":
        extra["tents"] = False

    # Max vehicle / rig length
    maxlength = tags.get("maxlength") or tags.get("max_length")
    if maxlength:
        try:
            extra["max_vehicle_length_m"] = float(str(maxlength).replace("m", "").strip())
        except (ValueError, TypeError):
            pass

    # Number of sites
    capacity_raw = tags.get("capacity") or tags.get("sites")
    if capacity_raw:
        try:
            extra["num_sites"] = int(str(capacity_raw).strip())
        except (ValueError, TypeError):
            pass

    # Bookable / reservations
    booking = tags.get("booking") or tags.get("reservations")
    if booking in ("yes", "required", "recommended"):
        extra["bookable"] = True
    elif booking == "no":
        extra["bookable"] = False

    # ── Facilities ────────────────────────────────────────────

    shower = tags.get("shower") or tags.get("showers")
    if shower in ("yes", "hot", "cold", "solar", "fee"):
        extra["has_showers"] = True
    elif shower == "no":
        extra["has_showers"] = False

    dump = tags.get("sanitary_dump_station") or tags.get("dump_station") or tags.get("dump_point")
    if dump in ("yes", "public", "customers"):
        extra["has_dump_point"] = True
    elif dump == "no":
        extra["has_dump_point"] = False

    bbq = tags.get("bbq") or tags.get("bbqs")
    if bbq in ("yes", "charcoal", "electric", "gas"):
        extra["has_bbq"] = True
    elif bbq == "no":
        extra["has_bbq"] = False

    laundry = tags.get("laundry") or tags.get("washing_machine")
    if laundry in ("yes", "coin", "token"):
        extra["has_laundry"] = True
    elif laundry == "no":
        extra["has_laundry"] = False

    kitchen = tags.get("kitchen") or tags.get("communal_kitchen")
    if kitchen == "yes":
        extra["has_kitchen"] = True
    elif kitchen == "no":
        extra["has_kitchen"] = False

    wifi = tags.get("internet_access") or tags.get("wifi")
    if wifi in ("yes", "wlan", "wifi", "fee"):
        extra["has_wifi"] = True
    elif wifi == "no":
        extra["has_wifi"] = False

    if tags.get("playground") in ("yes", "designated"):
        extra["has_playground"] = True

    if tags.get("swimming") == "yes":
        extra["has_swimming"] = True

    # Phone reception (critical for outback)
    reception = tags.get("reception:mobile_phone") or tags.get("mobile_signal")
    if reception in ("yes", "good", "excellent"):
        extra["has_phone_reception"] = True
    elif reception in ("no", "none", "poor"):
        extra["has_phone_reception"] = False

    # Per-carrier reception
    carriers: list = []
    for carrier, tag_key in [("telstra", "reception:telstra"), ("optus", "reception:optus"),
                               ("vodafone", "reception:vodafone"), ("tpg", "reception:tpg")]:
        val = tags.get(tag_key)
        if val in ("yes", "good", "excellent"):
            carriers.append(carrier)
    if carriers:
        extra["reception_carriers"] = carriers
        if "has_phone_reception" not in extra:
            extra["has_phone_reception"] = True

    # ── Camping style ─────────────────────────────────────────

    tourism_type = tags.get("tourism", "")
    if "camp_type" not in extra:
        operator = (tags.get("operator") or "").lower()
        operator_type = (tags.get("operator:type") or "").lower()
        name_lower = (tags.get("name") or "").lower()
        access = tags.get("access", "")
        fee = tags.get("fee", "")
        charge_raw = tags.get("charge") or tags.get("fee:amount") or ""
        leisure = tags.get("leisure", "")
        landuse = tags.get("landuse", "")

        # Parks authority heuristic - national/state parks generally charge
        _PARKS_AUTHORITIES = (
            "npws", "parks victoria", "parks vic", "qpws", "dbca",
            "parks sa", "nt parks", "pws",
        )
        operator_is_parks_authority = any(p in operator for p in _PARKS_AUTHORITIES)
        extra["operator_is_parks_authority"] = operator_is_parks_authority

        # Normalise fee signal: fee=no / fee=0 both mean free
        fee_is_free = fee in ("no", "0")
        fee_is_set = fee not in ("", "no", "0")  # an explicit non-free fee tag

        # Informal / bush camp override - takes priority over operator heuristic
        if tags.get("informal") == "yes":
            if not fee_is_set:
                extra["camp_type"] = "bush"
                extra["free"] = True
            else:
                extra["camp_type"] = "bush"
        # Backcountry tag
        elif tags.get("backcountry") == "yes":
            extra["camp_type"] = "backcountry"
            if not fee_is_set:
                extra["free"] = True
        # Nature reserve + camping=yes → likely free/bush camp
        elif leisure == "nature_reserve" and tags.get("camping") == "yes":
            extra["camp_type"] = "bush"
            if not fee_is_set:
                extra["free"] = True
        # Showground detection
        elif (
            leisure == "showground"
            or leisure == "recreation_ground"
            or landuse == "recreation_ground"
            or "showground" in name_lower
            or "show ground" in name_lower
        ):
            extra["camp_type"] = "showground"
        # Station stay
        elif (
            "station" in operator
            or "pastoral" in operator
            or operator_type in ("farm", "station", "pastoral")
            or "station stay" in name_lower
            or "station camp" in name_lower
        ):
            extra["camp_type"] = "station_stay"
        # Farm stay
        elif (
            "farm" in operator_type
            or "farm stay" in name_lower
            or "farmstay" in name_lower
        ):
            extra["camp_type"] = "farm_stay"
        # Bush camping (informal, track-side, dispersed)
        elif (
            tags.get("camp_type") in ("basic", "dispersed", "backcountry", "wild")
            or "bush camp" in name_lower
            or "bush camping" in name_lower
            or (access in ("", "yes", "permissive") and not operator and fee in ("", "no", "0"))
        ) and tourism_type in ("camp_site", "") and tags.get("amenity") != "camp_site":
            # Distinguish truly free from commercial-looking
            if fee_is_free or not operator:
                extra["camp_type"] = (
                    "bush"
                    if tags.get("camp_type") in ("basic", "dispersed", "backcountry", "wild")
                    else "free"
                )
        # Low cost: fee exists but charge is under $15
        elif fee_is_set and charge_raw:
            try:
                import re as _re
                m = _re.search(r"\$?\s*(\d+(?:\.\d+)?)", str(charge_raw))
                if m and float(m.group(1)) < 15:
                    extra["camp_type"] = "low_cost"
                else:
                    extra["camp_type"] = "commercial"
            except (ValueError, TypeError):
                extra["camp_type"] = "commercial"
        elif tourism_type in ("caravan_site", "caravan_park"):
            extra["camp_type"] = "caravan_park"
        elif tourism_type in ("holiday_park",) or tags.get("leisure") == "holiday_park":
            extra["camp_type"] = "caravan_park"
        elif tags.get("brand") and any(
            b in tags["brand"]
            for b in ("Big4", "BIG4", "Top Tourist", "Discovery Parks", "G'Day Parks")
        ):
            extra["camp_type"] = "caravan_park"
        elif fee_is_free and tourism_type in ("camp_site", "") and tags.get("amenity") in ("camp_site", "camping", None):
            extra["camp_type"] = "free"
        elif operator and tourism_type == "camp_site":
            extra["camp_type"] = "commercial"

    # ── Free flag ─────────────────────────────────────────────
    # Resolve free=True/False from fee tags (fee=no / fee=0 → free;
    # parks authority with no explicit fee tag → assume fee=True)
    if "free" not in extra:
        _fee_tag = tags.get("fee", "")
        if _fee_tag in ("no", "0"):
            extra["free"] = True
        elif _fee_tag not in ("", None):
            extra["free"] = False
        elif extra.get("operator_is_parks_authority") and _fee_tag == "":
            # Parks authorities generally charge; flag as not free unless explicit
            extra["free"] = False
        # access=customers with no free signal → uncertain, leave unset
        elif tags.get("access") == "customers" and _fee_tag == "":
            pass  # don't set free

    # ── Wheelchair accessibility ───────────────────────────────
    wheelchair = tags.get("wheelchair", "")
    if wheelchair in ("yes", "designated"):
        extra["accessible"] = True
    elif wheelchair in ("no", "limited"):
        extra["accessible"] = False

    # ── Quality score (0–5) ───────────────────────────────────
    _score = 0.0
    if extra.get("has_toilets"):
        _score += 1.0
    if extra.get("has_water"):
        _score += 1.0
    if extra.get("has_showers"):
        _score += 0.5
    if extra.get("has_bbq"):
        _score += 0.25
    if extra.get("has_dump_point"):
        _score += 0.5
    if extra.get("has_wifi"):
        _score += 0.25
    _num_sites = extra.get("num_sites")
    if _num_sites and _num_sites > 5:
        _score += 0.25
    if extra.get("free"):
        _score += 0.25
    extra["quality_score"] = min(5.0, round(_score, 2))

    surface_map = {
        "grass": "grass", "gravel": "gravel", "dirt": "dirt", "unpaved": "dirt",
        "earth": "dirt", "sand": "sand", "concrete": "concrete", "paved": "concrete",
        "asphalt": "concrete", "compacted": "gravel",
    }
    surface_raw = tags.get("surface")
    if surface_raw and surface_raw in surface_map:
        extra["surface"] = surface_map[surface_raw]

    if tags.get("shelter") == "yes":
        extra["shelter"] = True
    if tags.get("trees") in ("yes", "many") or tags.get("shade") == "yes":
        extra["shade"] = True

    # ── Stay rules ────────────────────────────────────────────

    max_stay = tags.get("max_stay") or tags.get("maxstay")
    if max_stay:
        try:
            ms_str = str(max_stay).lower().strip()
            if "hour" in ms_str:
                hours_str = re.sub(r"[^\d.]", "", ms_str)
                if hours_str:
                    extra["max_stay_days"] = round(float(hours_str) / 24, 1)
            else:
                days_str = re.sub(r"[^\d.]", "", ms_str)
                if days_str:
                    extra["max_stay_days"] = int(float(days_str))
        except (ValueError, TypeError):
            pass

    check_in = tags.get("check_in") or tags.get("checkin")
    if check_in:
        extra["check_in"] = str(check_in)[:20]

    check_out = tags.get("check_out") or tags.get("checkout")
    if check_out:
        extra["check_out"] = str(check_out)[:20]

    quiet_hours = tags.get("quiet_hours") or tags.get("noise_curfew")
    if quiet_hours:
        extra["quiet_hours"] = str(quiet_hours)[:40]

    # ── Cost ─────────────────────────────────────────────────

    charge = tags.get("charge") or tags.get("fee:amount")
    if charge:
        m = re.search(r"\$?\s*(\d+(?:\.\d+)?)", str(charge))
        if m:
            try:
                extra["price_per_night_aud"] = float(m.group(1))
            except ValueError:
                pass
        extra["price_notes"] = str(charge)[:80]


# State-level rest-area overnight rules (AU).
# Keyed by `addr:state` or `is_in:state` OSM tag, lowercase.
_STATE_REST_AREA_RULES: Dict[str, Dict[str, Any]] = {
    "qld":  {"allowed": True,       "max_hours": 20, "note": "QLD 20hr limit at designated rest areas"},
    "wa":   {"allowed": True,       "max_hours": 24, "note": "WA 24hr limit at designated rest areas"},
    "nt":   {"allowed": True,       "max_hours": None, "note": "NT generally allows rest area camping"},
    "sa":   {"allowed": "check",    "max_hours": None, "note": "SA varies - check signage at each rest area"},
    "tas":  {"allowed": "check",    "max_hours": None, "note": "TAS varies by council"},
    "nsw":  {"allowed": "prohibited","max_hours": None, "note": "NSW generally restricts rest area camping; check local council"},
    "vic":  {"allowed": "prohibited","max_hours": None, "note": "VIC generally prohibits rest area camping"},
    "act":  {"allowed": "prohibited","max_hours": None, "note": "ACT rest areas not designated for overnight stays"},
}

# Aliases from various OSM addr:state values
_STATE_ALIAS: Dict[str, str] = {
    "queensland": "qld", "western australia": "wa", "northern territory": "nt",
    "south australia": "sa", "tasmania": "tas", "new south wales": "nsw",
    "victoria": "vic", "australian capital territory": "act",
}


def _resolve_au_state(tags: Dict[str, Any]) -> Optional[str]:
    """Return normalised 2-3 letter AU state code from OSM tags, or None."""
    raw = (
        tags.get("addr:state")
        or tags.get("is_in:state")
        or tags.get("is_in:state_code")
        or tags.get("state")
        or ""
    ).lower().strip()
    if not raw:
        return None
    # Direct match (qld, wa, nt, sa, tas, nsw, vic, act)
    if raw in _STATE_REST_AREA_RULES:
        return raw
    return _STATE_ALIAS.get(raw)


def _enrich_overnight_rules(tags: Dict[str, Any], extra: Dict[str, Any]) -> None:
    """Populate overnight_allowed / overnight_max_hours / overnight_notes from
    OSM tags and state-level rules.  Already-set values are not overwritten."""

    if "overnight_allowed" in extra:
        return  # caller already set it (e.g. explicit OSM tag)

    hw = tags.get("highway", "")
    tourism = tags.get("tourism", "")
    is_rest_area = hw == "rest_area" or tags.get("amenity") == "rest_area"

    # ── Explicit OSM tags take highest priority ───────────────
    max_stay_raw = tags.get("max_stay") or tags.get("maxstay") or ""
    overnight_tag = tags.get("overnight") or tags.get("overnight_camping") or ""

    if overnight_tag in ("yes", "permitted", "allowed"):
        extra["overnight_allowed"] = True
    elif overnight_tag in ("no", "prohibited", "not_permitted", "forbidden"):
        extra["overnight_allowed"] = "prohibited"

    # max_stay already handled in _enrich_camp_tags; read it back
    if "overnight_allowed" not in extra and max_stay_raw:
        # Any max_stay tag implies overnight stays are expected
        extra["overnight_allowed"] = True

    # ── State-level inference for rest areas ─────────────────
    if "overnight_allowed" not in extra and is_rest_area:
        state = _resolve_au_state(tags)
        if state and state in _STATE_REST_AREA_RULES:
            rule = _STATE_REST_AREA_RULES[state]
            extra["overnight_allowed"] = rule["allowed"]
            if rule["max_hours"] is not None:
                extra["overnight_max_hours"] = rule["max_hours"]
            if rule["note"]:
                extra["overnight_notes"] = rule["note"]
        else:
            extra["overnight_allowed"] = "check"
            extra["overnight_notes"] = "Overnight rules vary - check signage"

    # ── Free campsites are implicitly overnight-allowed ───────
    if "overnight_allowed" not in extra and tourism == "camp_site":
        if extra.get("camp_type") in ("free", "bush", "showground", "station_stay", "farm_stay", "low_cost"):
            extra["overnight_allowed"] = True
        elif tags.get("fee") == "no":
            extra["overnight_allowed"] = True


def _element_to_item(el: Dict[str, Any]) -> Optional[PlaceItem]:
    tags = el.get("tags") or {}

    lat = el.get("lat")
    lon = el.get("lon")
    if lat is None or lon is None:
        center = el.get("center")
        if not center:
            return None
        lat = center.get("lat")
        lon = center.get("lon")
        if lat is None or lon is None:
            return None

    osm_type = el.get("type", "node")
    osm_id = el.get("id")
    if osm_id is None:
        return None

    category = _infer_category(tags)

    name = (
        tags.get("name")
        or tags.get("brand")
        or tags.get("operator")
        or tags.get("description")
        or tags.get("loc_name")
        or tags.get("alt_name")
        or tags.get("ref")
    )

    if not name:
        name = _synthetic_name(category, tags, lat, lon)

    extra: Dict[str, Any] = {"osm_type": osm_type, "osm_id": osm_id}

    for k in ("phone", "contact:phone", "website", "contact:website",
              "opening_hours", "fee", "access", "capacity",
              "brand", "operator", "description",
              "stars", "internet_access", "wheelchair"):
        v = tags.get(k)
        if v:
            clean_k = k.replace("contact:", "")
            extra[clean_k] = str(v)[:200]

    # Boolean facility tags extracted directly into extra
    for bool_tag in ("toilets", "drinking_water", "showers", "powered_sites",
                     "tents", "caravans", "dogs", "bbq", "dump", "fires_allowed"):
        v = tags.get(bool_tag)
        if v in ("yes", "no"):
            extra[bool_tag] = v == "yes"

    addr_parts = []
    for ak in ("addr:housenumber", "addr:street", "addr:suburb",
                "addr:city", "addr:state", "addr:postcode"):
        av = tags.get(ak)
        if av:
            addr_parts.append(str(av))
    if addr_parts:
        extra["address"] = ", ".join(addr_parts)

    fuel_types = []
    for fk in ("fuel:diesel", "fuel:unleaded", "fuel:octane_91", "fuel:octane_95",
                "fuel:octane_98", "fuel:lpg", "fuel:adblue"):
        if tags.get(fk) == "yes":
            fuel_types.append(fk.replace("fuel:", ""))
    if fuel_types:
        extra["fuel_types"] = fuel_types
        # Convenience booleans so the client doesn't have to parse the array.
        # Only set False when we have explicit fuel_types data and the type is absent.
        # Leave unset (undefined on client) if no fuel_types data at all, so the
        # client doesn't incorrectly exclude stations with missing OSM tags.
        extra["has_diesel"] = "diesel" in fuel_types
        extra["has_unleaded"] = any(
            t in fuel_types for t in ("unleaded", "octane_91", "octane_95", "octane_98")
        )
        extra["has_lpg"] = "lpg" in fuel_types

    socket_types = []
    for sk in ("socket:type2", "socket:type2_combo", "socket:chademo",
                "socket:tesla_supercharger", "socket:type1"):
        if tags.get(sk):
            socket_types.append(sk.replace("socket:", ""))
    if socket_types:
        extra["socket_types"] = socket_types

    if tags.get("fee") == "no":
        extra["free"] = True
    if tags.get("power_supply") == "yes":
        extra["powered_sites"] = True
    if tags.get("drinking_water") == "yes":
        extra["has_water"] = True
    if tags.get("toilets") == "yes" or tags.get("amenity") == "toilets":
        extra["has_toilets"] = True

    # ── Camping-specific enrichment ───────────────────────────
    if category == "camp":
        _enrich_camp_tags(tags, extra)

    # ── Rest area overnight legality ──────────────────────────
    if category in ("camp", "rest_area"):
        _enrich_overnight_rules(tags, extra)

    # ── Dump point specifics ──────────────────────────────────
    if category == "dump_point":
        waste = tags.get("waste", "")
        if "chemical" in waste or "black_water" in waste:
            extra["dump_type"] = "black_water"
        elif "grey_water" in waste or "grey" in waste:
            extra["dump_type"] = "grey_water"
        elif tags.get("sanitary_dump_station") == "yes":
            extra["dump_type"] = "both"

        dump_fee = tags.get("fee")
        if dump_fee:
            extra["dump_fee"] = "free" if dump_fee == "no" else str(dump_fee)[:80]
            if dump_fee == "no":
                extra["free"] = True

        acc = tags.get("access", "")
        if acc in ("private", "customers"):
            extra["dump_access"] = "customers_only"
        elif acc == "key":
            extra["dump_access"] = "key_required"
        else:
            extra["dump_access"] = "public"

        if tags.get("drinking_water") == "yes":
            extra["has_potable_water_at_dump"] = True
        if tags.get("rinse_water") == "yes" or tags.get("water") == "yes":
            extra["has_rinse"] = True

    # ── Water fill specifics ──────────────────────────────────
    if category == "water_fill":
        extra["has_water"] = True

    # ── Water point specifics ─────────────────────────────────
    if category == "water":
        mm = tags.get("man_made", "")
        dw = tags.get("drinking_water", "")
        dw_legal = tags.get("drinking_water:legal", "")
        if dw == "yes" or dw_legal == "yes":
            extra["water_type"] = "potable"
            if tags.get("drinking_water:treated") == "yes":
                extra["water_treated"] = True
        elif dw == "no":
            extra["water_type"] = "non_potable"

        n_tag = tags.get("natural", "")
        if tags.get("amenity") == "drinking_water" or mm == "water_tap":
            extra["water_flow"] = "tap"
        elif n_tag == "spring":
            extra["water_flow"] = "pump"
            if not extra.get("water_type"):
                extra["water_type"] = "potable"
        elif mm in ("water_well",):
            src = tags.get("water_source", "")
            if "bore" in src:
                extra["water_flow"] = "bore"
                if not extra.get("water_type"):
                    extra["water_type"] = "bore"
            else:
                extra["water_flow"] = "pump"

        if tags.get("seasonal") == "no" or tags.get("intermittent") == "no":
            extra["water_always_available"] = True
        elif tags.get("seasonal") == "yes" or tags.get("intermittent") == "yes":
            extra["water_always_available"] = False

    # ── Toilet specifics ──────────────────────────────────────
    if category == "toilet":
        disposal = tags.get("toilets:disposal", "")
        if disposal == "flush" or tags.get("flush") == "yes":
            extra["toilet_type"] = "flush"
        elif disposal in ("pitlatrine", "pit"):
            extra["toilet_type"] = "pit"
        elif disposal == "composting":
            extra["toilet_type"] = "composting"
        elif tags.get("flush") == "no":
            extra["toilet_type"] = "long_drop"

        num = tags.get("toilets:num") or tags.get("capacity")
        if num:
            try:
                extra["toilet_count"] = int(num)
            except (ValueError, TypeError):
                pass

        toilet_wc = tags.get("wheelchair")
        if toilet_wc in ("yes", "limited"):
            extra["has_disabled_access"] = True
        if tags.get("changing_table") == "yes" or tags.get("diaper") == "yes" or tags.get("baby_feeding") == "yes":
            extra["has_baby_change"] = True
        if tags.get("handwashing") == "yes":
            extra["has_hand_wash"] = True

    # ── Shower specifics ──────────────────────────────────────
    if category == "shower":
        hot = tags.get("hot_water", "")
        solar = tags.get("solar_powered", "") == "yes" or tags.get("solar") == "yes"
        if solar:
            extra["shower_type"] = "solar"
        elif hot in ("yes", "heated"):
            extra["shower_type"] = "hot"
        elif hot == "no":
            extra["shower_type"] = "cold"

        shower_fee = tags.get("fee")
        if shower_fee:
            extra["shower_fee"] = "free" if shower_fee == "no" else str(shower_fee)[:80]
            if shower_fee == "no":
                extra["free"] = True
        if tags.get("payment:coins") == "yes" or tags.get("payment:tokens") == "yes":
            extra["shower_token"] = True

        num = tags.get("capacity") or tags.get("shower:count")
        if num:
            try:
                extra["shower_count"] = int(num)
            except (ValueError, TypeError):
                pass

    if not (tags.get("name") or tags.get("brand") or tags.get("operator")):
        extra["synthetic_name"] = True

    # ── Enrichment: wikidata, wikipedia, thumbnail ────────────
    wd = tags.get("wikidata")
    if wd and isinstance(wd, str):
        extra["wikidata"] = str(wd)[:20]
    wp = tags.get("wikipedia")
    if wp and isinstance(wp, str):
        extra["wikipedia"] = str(wp)[:200]

    # Resolve a thumbnail URL from wikimedia_commons / image / wikidata
    thumb = _resolve_thumbnail(tags)
    if thumb:
        extra["thumbnail_url"] = thumb

    # Wheelchair accessibility
    wc = tags.get("wheelchair")
    if wc in ("yes", "limited"):
        extra["wheelchair"] = wc

    # Stars / rating (some OSM entries have tourism:stars)
    stars = tags.get("stars") or tags.get("tourism:stars") or tags.get("accommodation:stars")
    if stars:
        try:
            extra["stars"] = int(stars)
        except (ValueError, TypeError):
            pass

    # ── Elevation ─────────────────────────────────────────────
    # Useful for viewpoints, peaks, hiking trailheads
    ele = tags.get("ele")
    if ele:
        try:
            extra["elevation_m"] = round(float(str(ele).replace("m", "").strip()), 1)
        except (ValueError, TypeError):
            pass

    # ── Short description / blurb ─────────────────────────────
    # OSM AU mappers often add short_description as a one-liner
    sd = tags.get("short_description") or tags.get("description:en")
    if sd and not extra.get("description"):
        extra["description"] = str(sd)[:300]
    elif sd:
        extra["short_description"] = str(sd)[:300]

    # ── Contact email ─────────────────────────────────────────
    email = tags.get("email") or tags.get("contact:email")
    if email:
        extra["email"] = str(email)[:200]

    # ── Cuisine & dietary options ─────────────────────────────
    # Relevant for restaurants, cafes, pubs
    if category in ("restaurant", "cafe", "fast_food", "pub", "bar", "bakery"):
        cuisine = tags.get("cuisine")
        if cuisine:
            extra["cuisine"] = str(cuisine)[:100]
        diets = []
        for d in ("vegan", "vegetarian", "gluten_free", "halal", "kosher"):
            if tags.get(f"diet:{d}") in ("yes", "only"):
                diets.append(d)
        if diets:
            extra["diets"] = diets

    # ── Mapillary street-level photo ──────────────────────────
    # Sequence/image ID - client resolves to thumb at
    # https://graph.mapillary.com/{id}?fields=thumb_1024_url&access_token=...
    # Stored offline as fallback when wikimedia thumb is absent
    mapillary = tags.get("mapillary")
    if mapillary and not extra.get("thumbnail_url"):
        extra["mapillary_id"] = str(mapillary)[:64]

    # ── Brand Wikidata ────────────────────────────────────────
    # Lets client resolve brand logo without extra API call
    bwd = tags.get("brand:wikidata")
    if bwd and isinstance(bwd, str) and bwd.startswith("Q"):
        extra["brand_wikidata"] = str(bwd)[:20]

    # ── Heritage details ──────────────────────────────────────
    if category == "heritage":
        hr = tags.get("heritage:ref") or tags.get("ref:nrhp") or tags.get("ref:whc")
        if hr:
            extra["heritage_ref"] = str(hr)[:80]
        operator = tags.get("heritage:operator") or tags.get("owner")
        if operator:
            extra["heritage_operator"] = str(operator)[:100]
        hstate = tags.get("heritage")
        if hstate:
            extra["heritage_level"] = str(hstate)[:40]

    # ── Sport / activity type ─────────────────────────────────
    # Already used for category inference but useful to surface to users
    sport = tags.get("sport")
    if sport and category in ("fishing", "surf", "hiking", "swimming_hole", "golf", "pool"):
        extra["sport"] = str(sport)[:80]

    return PlaceItem(
        id=f"osm:{osm_type}:{osm_id}",
        name=str(name),
        lat=float(lat),
        lng=float(lon),
        category=category,
        extra=extra,
    )


# ──────────────────────────────────────────────────────────────
# Overpass querying
# ──────────────────────────────────────────────────────────────

def _overpass_bbox_str(b: BBox4) -> str:
    return f"({b.minLat},{b.minLng},{b.maxLat},{b.maxLng})"


def _build_overpass_ql(*, bbox: BBox4, filters: List[str], name_clause: str) -> str:
    bbox_str = _overpass_bbox_str(bbox)

    parts: List[str] = []
    if not filters:
        parts.append(f'node{name_clause}{bbox_str};')
        parts.append(f'way{name_clause}{bbox_str};')
        parts.append(f'relation{name_clause}{bbox_str};')
    else:
        for f in filters:
            parts.append(f'node{name_clause}{f}{bbox_str};')
            parts.append(f'way{name_clause}{f}{bbox_str};')
            parts.append(f'relation{name_clause}{f}{bbox_str};')

    http_timeout_s = int(getattr(settings, "overpass_timeout_s", 90))
    ql_timeout_s = max(10, http_timeout_s - 10)
    return (
        f'[out:json][timeout:{ql_timeout_s}][maxsize:16000000];'
        f'('
        f'{"".join(parts)}'
        f');'
        f'out center;'
    )


def _build_overpass_ql_tiled(*, bbox: BBox4, filters: List[str], name_clause: str) -> str:
    """Like _build_overpass_ql but with a short server-side timeout (8s).

    Tile queries are small bbox areas - they should be fast.  If Overpass
    is slow, we'd rather skip a tile than block the whole corridor pipeline.
    """
    bbox_str = _overpass_bbox_str(bbox)
    parts: List[str] = []
    if not filters:
        parts.append(f'node{name_clause}{bbox_str};')
        parts.append(f'way{name_clause}{bbox_str};')
    else:
        for f in filters:
            parts.append(f'node{name_clause}{f}{bbox_str};')
            parts.append(f'way{name_clause}{f}{bbox_str};')
    return (
        f'[out:json][timeout:8][maxsize:8000000];'
        f'('
        f'{"".join(parts)}'
        f');'
        f'out center;'
    )


def _build_overpass_around_ql(
    *,
    coords: List[Tuple[float, float]],
    radius_m: float,
    filters: List[str],
    name_clause: str,
    max_coords: int = 120,
) -> str:
    # Dynamically cap coords based on filter count to keep query size
    # reasonable.  Each filter generates 2 clauses (node+way), each
    # containing the full coord CSV.  Target max ~32KB of QL to stay
    # well within Overpass server limits on public instances.
    n_clauses = max(len(filters), 1) * 2
    # ~25 bytes per coord pair ("-XX.XXXXX,XXX.XXXXX,")
    max_ql_bytes = 32_000
    coord_budget = max(10, max_ql_bytes // (n_clauses * 25))
    effective_max = min(max_coords, coord_budget)

    if len(coords) > effective_max:
        step = max(1, len(coords) // effective_max)
        coords = coords[::step]
        if coords[-1] != coords[-1]:
            coords.append(coords[-1])

    coord_csv = ",".join(f"{lat:.5f},{lng:.5f}" for lat, lng in coords)
    around = f"(around:{radius_m:.0f},{coord_csv})"

    parts: List[str] = []
    if not filters:
        parts.append(f"node{name_clause}{around};")
        parts.append(f"way{name_clause}{around};")
    else:
        for f in filters:
            parts.append(f"node{name_clause}{f}{around};")
            parts.append(f"way{name_clause}{f}{around};")

    http_timeout_s = int(getattr(settings, "overpass_timeout_s", 90))
    # Set the Overpass server-side timeout 10s shorter than the HTTP
    # timeout so the server returns a proper error response instead of
    # us getting a raw ReadTimeout.
    ql_timeout_s = max(10, http_timeout_s - 10)
    return (
        f"[out:json][timeout:{ql_timeout_s}][maxsize:16000000];"
        f"("
        f"{''.join(parts)}"
        f");"
        f"out center;"
    )


def _fetch_overpass_with_retries(*, client: Any, ql: str, label: str = "places", timeout_s: float | None = None) -> Dict[str, Any]:
    """Delegate to global Overpass gate (client arg ignored for back-compat)."""
    from app.core.overpass import overpass_fetch_sync
    return overpass_fetch_sync(ql, label=label, timeout_s=timeout_s)


def _safe_overpass_name_regex(q: str) -> str:
    q = q.strip()
    if not q:
        return ""
    q = q.replace('"', "")
    q = re.escape(q)
    return q[:80]


# ──────────────────────────────────────────────────────────────
# Corridor polyline key helper
# ──────────────────────────────────────────────────────────────

def _corridor_places_key(
    polyline6: str,
    buffer_km: float,
    categories: List[str],
    limit: int,
    algo_version: str,
) -> str:
    cats_str = ",".join(sorted(categories))
    raw = (
        f"CorridorPlaces/v1|"
        f"poly_sha={hashlib.sha256(polyline6.encode()).hexdigest()}|"
        f"buf={buffer_km}|cats={cats_str}|lim={limit}|"
        f"algo={algo_version}"
    )
    return hashlib.sha256(raw.encode()).hexdigest()


# ──────────────────────────────────────────────────────────────
# Bundle-specific helpers: tiers, budget, cluster cap
# ──────────────────────────────────────────────────────────────

# Critical infrastructure - fuel and EV charging get a DEDICATED Overpass query
# with a much higher coord limit (only 2 OSM filters, so query stays small even
# with 300+ sample points).  This prevents them from being squeezed out by the
# larger tier-1 query timing out on long routes.
_CRITICAL_INFRA_CATS: List[PlaceCategory] = ["fuel", "ev_charging"]
_CRITICAL_INFRA_BUFFER_KM = 30.0   # same wide radius as tier 1
_CRITICAL_INFRA_MAX_COORDS = 300   # can afford many coords with only 2 filters

# Tier 1 - things you *need* on a road trip (fuel, safety, sleep, food supply).
# Wide search radius: towns can be 100 km off-route in the outback and still
# be the only option.  No cluster cap - you want every servo, every hospital.
# NOTE: fuel + ev_charging are excluded here - they have their own dedicated query.
_BUNDLE_TIER1_CATS: List[PlaceCategory] = [
    "water", "water_fill", "toilet", "rest_area",
    "mechanic", "hospital", "pharmacy", "dump_point", "emergency_phone",
    "grocery", "town",
    "camp", "hotel", "motel", "hostel",
    "fast_food",                    # only reliable food option on remote highways
]
_BUNDLE_TIER1_BUFFER_KM = 30.0     # towns/fuel up to 30 km either side
_BUNDLE_TIER1_FRACTION  = 0.65     # 65 % of total budget reserved for tier 1

# Tier 2 - things that enrich a trip but aren't survival-critical.
# Tight corridor only; cluster-capped so one city doesn't eat all slots.
_BUNDLE_TIER2_CATS: List[PlaceCategory] = [
    "cafe", "restaurant", "pub", "bar", "bakery",
    "viewpoint", "beach", "waterfall", "swimming_hole",
    "hot_spring", "national_park", "hiking", "picnic",
    "cave", "fishing", "surf",
    "attraction", "heritage", "museum", "gallery",
    "winery", "brewery", "visitor_info",
    "park", "market", "library", "showground",
    "atm", "laundromat",
    "playground", "pool", "zoo", "theme_park",
    "dog_park", "golf", "cinema",
]

# High-value categories that deserve a wider search radius than generic tier-2.
# A cracking waterfall or gorge 12 km off the highway is absolutely worth showing;
# a laundromat 12 km away is not.
_BUNDLE_TIER2_HIGH_VALUE_CATS: Set[str] = {
    "viewpoint", "beach", "waterfall", "swimming_hole", "hot_spring",
    "national_park", "hiking", "cave", "fishing", "surf",
    "attraction", "heritage", "museum", "zoo", "theme_park",
    "winery", "brewery", "showground",
}
_BUNDLE_TIER2_HIGH_VALUE_BUFFER_KM = 15.0  # wider net for destination-worthy spots
_BUNDLE_TIER2_BUFFER_KM = 5.0              # tight for generic amenities
_BUNDLE_TIER2_CLUSTER_KM = 10.0    # one segment = 10 km
_BUNDLE_TIER2_PER_CLUSTER = 6      # max tier-2 items per 10 km segment


def _bundle_places_budget(route_km: float) -> int:
    """
    Dynamic offline bundle size.

    Scales with route length so a city hop doesn't pull too many places and an
    outback crossing doesn't run dry.  Caps at 5000 to keep download lean.

      50 km  →  ~350   (city loop)
     200 km  → ~1200
     500 km  → ~3000
     834 km  → ~5000   (cap)
    """
    raw = max(50.0, route_km) * 6.0
    return int(max(350, min(5000, raw)))


def _corridor_places_budget(extent_km: float) -> int:
    """
    Dynamic corridor limit - scales with route *extent* (bbox diagonal).

    Takes the geographic footprint of the route, NOT the traverse distance.
    A winding 700 km loop that only spans 120 km N-S gets budgeted as ~120 km,
    preventing dense urban areas from flooding the results.

      50 km  →  ~600   (city hop)
     100 km  → ~1000   (Sunny Coast → Brisbane)
     200 km  → ~2000
     500 km  → ~5000
    1000 km  → ~8000   (cap)
    """
    raw = max(50.0, extent_km) * 10.0
    return int(max(600, min(8000, raw)))


# ──────────────────────────────────────────────────────────────
# Relevance scoring - ranks places within each tier so the
# most useful/notable items are accepted first.
# ──────────────────────────────────────────────────────────────

# Category-intrinsic importance weights.  Higher = more likely to be a
# meaningful stop vs an anonymous node.
_CATEGORY_IMPORTANCE: Dict[str, float] = {
    # Nature highlights - these are *why* people take road trips
    "national_park": 5.0, "waterfall": 4.5, "hot_spring": 4.5,
    "cave": 4.5, "surf": 3.5,
    "swimming_hole": 4.0, "viewpoint": 4.0, "beach": 3.5, "hiking": 3.0,
    "fishing": 3.0,
    # Culture - destination-worthy attractions
    "museum": 4.5, "heritage": 4.0, "gallery": 3.5, "attraction": 3.5,
    "zoo": 3.5, "theme_park": 3.5, "winery": 3.0, "brewery": 3.0,
    "showground": 2.0,
    # Towns are anchor points
    "town": 3.0, "visitor_info": 2.5,
    # Essential infrastructure - always valuable but not "destination"
    "fuel": 2.0, "ev_charging": 2.0, "hospital": 2.0, "mechanic": 1.5, "emergency_phone": 2.5,
    "grocery": 1.5, "pharmacy": 1.5,
    # Accommodation - critical for trip planning, boosted
    "camp": 4.0, "hotel": 2.5, "motel": 2.5, "hostel": 2.5,
    # Amenities
    "rest_area": 2.0, "dump_point": 1.5, "shower": 1.5,
    "water": 1.0, "water_fill": 1.0, "toilet": 0.5,
    # Nice-to-have
    "restaurant": 1.5, "cafe": 1.5, "pub": 1.5, "bakery": 1.5,
    "fast_food": 1.0, "bar": 1.0,
    "atm": 0.3, "laundromat": 0.5,
    "picnic": 1.0, "park": 1.0, "market": 1.5, "pool": 1.5,
    "playground": 1.0, "dog_park": 1.5, "golf": 1.5, "cinema": 1.5,
    "library": 1.0,
}


def _score_place(
    item: PlaceItem,
    landmark_names: Set[str] | None = None,
) -> float:
    """
    Score a PlaceItem for bundle relevance.  Higher = more worth including.

    Signals:
      - Category importance (some categories are inherently more notable)
      - Data richness (named, has website/phone/hours → real establishment)
      - Landmark match (name appears in regional knowledge → known highlight)
      - Wikidata presence (notable enough to be in Wikipedia/Wikidata)
    """
    score = 0.0
    extra = item.extra or {}

    # 1. Category base weight
    score += _CATEGORY_IMPORTANCE.get(str(item.category), 1.0)

    # 2. Data richness signals - real, well-documented places
    if not extra.get("synthetic_name"):
        score += 2.0  # has a real name
    if extra.get("website"):
        score += 1.5
    if extra.get("phone"):
        score += 1.0
    if extra.get("opening_hours"):
        score += 1.0
    if extra.get("description"):
        score += 0.5
    if extra.get("brand"):
        score += 0.5  # known chain = reliable
    if extra.get("address"):
        score += 0.3

    # 3. Wikidata / Wikipedia - strong notability signal
    if extra.get("wikidata"):
        score += 3.0
    if extra.get("wikipedia"):
        score += 2.0

    # 4. Landmark boost - match against known regional highlights
    if landmark_names and item.name:
        name_lower = item.name.lower()
        for landmark in landmark_names:
            if landmark in name_lower or name_lower in landmark:
                score += 5.0
                break

    # 5. Amenity richness for camps/rest areas
    if item.category in ("camp", "rest_area"):
        if extra.get("has_water"):
            score += 0.5
        if extra.get("has_toilets"):
            score += 0.5
        if extra.get("powered_sites"):
            score += 0.5
        if extra.get("free"):
            score += 0.5
        # Rich camping amenities (each adds a small quality signal)
        if extra.get("has_showers"):
            score += 0.4
        if extra.get("has_dump_point"):
            score += 0.3
        if extra.get("has_bbq"):
            score += 0.2
        if extra.get("has_laundry"):
            score += 0.2
        if extra.get("has_wifi"):
            score += 0.1
        if extra.get("has_phone_reception"):
            score += 0.3
        if extra.get("num_sites"):
            # Larger sites are more likely to be well-maintained
            ns = int(extra["num_sites"])
            if ns >= 50:
                score += 0.3
            elif ns >= 10:
                score += 0.1

    # 6. Fuel completeness
    if item.category == "fuel":
        fuel_types = extra.get("fuel_types") or []
        if len(fuel_types) >= 3:
            score += 1.0
        elif len(fuel_types) >= 1:
            score += 0.5

    # 7. Elevation data - viewpoints/peaks with known elevation are more notable
    if extra.get("elevation_m") and item.category in ("viewpoint", "hiking", "national_park"):
        score += 0.5

    # 8. Cuisine info - restaurants with cuisine tags are better documented
    if extra.get("cuisine"):
        score += 0.3
    if extra.get("diets"):
        score += 0.2

    # 9. Photo availability (mapillary as fallback)
    if extra.get("thumbnail_url") or extra.get("mapillary_id"):
        score += 0.5

    return score


# ──────────────────────────────────────────────────────────────
# Landmark extraction from regional knowledge
# ──────────────────────────────────────────────────────────────

# Regex to extract proper-noun landmark names from the region knowledge text.
# Matches capitalized multi-word names (2-6 words) that aren't sentence starters.
_LANDMARK_PATTERN = re.compile(
    r"(?<=[:\.\-–,])\s*"              # preceded by punctuation
    r"([A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,5})"  # 1-6 capitalized words
    r"(?:\s*(?:NP|National Park|Museum|Gallery|Falls|Gorge|Beach|"
    r"Lookout|Walk|Trail|Cave|Pool|Springs?|Range|Island|"
    r"Reef|Rocks?|Bridge|Market|Sanctuary|Reserve|Conservation Park))?"
)


@functools.lru_cache(maxsize=1)
def _load_all_landmark_names() -> Dict[str, Set[str]]:
    """
    Extract notable place names from each region's knowledge text.
    Returns {region_id: {lowercase landmark name, ...}}.
    """
    try:
        from app.services.guide_data import get_regions
        regions = get_regions()
    except Exception:
        return {}

    result: Dict[str, Set[str]] = {}
    for r in regions:
        rid = r.get("id", "")
        text = r.get("knowledge", "")
        names: Set[str] = set()

        # Extract from explicit mentions (words before parenthetical descriptions,
        # named attractions after colons/dashes)
        for m in _LANDMARK_PATTERN.finditer(text):
            candidate = m.group(0).strip()
            # Skip very short or generic terms
            if len(candidate) >= 5 and candidate.lower() not in {
                "the", "this", "that", "near", "from", "with", "most",
                "best", "book", "carry", "check", "drive", "allow",
                "watch", "avoid", "summer", "winter", "spring", "autumn",
                "excellent", "spectacular", "stunning", "extraordinary",
                "beautiful", "brilliant", "genuine", "deeply", "dramatically",
                "close", "closed", "contact", "distances", "guided",
                "confronting", "cultural", "artesian", "dozens", "great",
                "hinterland", "even", "every", "never", "serious",
                "including", "between", "about", "after", "before",
                "above", "below", "where", "which", "worth", "along",
                "around", "across", "through", "their", "these",
                "those", "other", "caves", "cellar", "gives", "catch",
                "fuel", "bore", "bill", "cash", "devil", "base",
            }:
                names.add(candidate.lower())

        # Also grab text between bold markers or inside quotes if present
        # and specifically named places (Cape X, Mt X, Lake X, etc.)
        for pattern in [
            r"((?:Cape|Mt|Mount|Lake|Port|Point)\s+[A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)",
            r"((?:Twelve|Three)\s+[A-Z][a-z]+)",
            r"([A-Z][a-z]+\s+(?:Falls|Gorge|Beach|Bay|Creek|River|Island|Ranges?|Pool|Head|Gap|Rock|Rocks|Springs?))",
        ]:
            for m2 in re.finditer(pattern, text):
                name = m2.group(1).strip()
                if len(name) >= 4:
                    names.add(name.lower())

        if names:
            result[rid] = names

    return result


def _landmarks_for_route(
    samples: List[Tuple[float, float]],
) -> Set[str]:
    """
    Determine which regions a route passes through, then return the union
    of all landmark names for those regions.
    """
    all_landmarks = _load_all_landmark_names()
    if not all_landmarks:
        return set()

    try:
        from app.services.guide_data import get_regions
        regions = get_regions()
    except Exception:
        return set()

    # Find which regions the route samples intersect
    matched_names: Set[str] = set()
    for r in regions:
        bbox = r.get("bbox", {})
        s, n = bbox.get("s", -90), bbox.get("n", 90)
        w, e = bbox.get("w", -180), bbox.get("e", 180)
        rid = r.get("id", "")
        if rid not in all_landmarks:
            continue
        for lat, lng in samples:
            if s <= lat <= n and w <= lng <= e:
                matched_names.update(all_landmarks[rid])
                break  # this region matched, move to next

    return matched_names


# ──────────────────────────────────────────────────────────────
# Wikimedia Commons thumbnail resolution
# ──────────────────────────────────────────────────────────────

_WIKI_THUMB_WIDTH = 400  # px - small enough for bundles, big enough to look good

_COMMONS_URL_TEMPLATE = (
    "https://commons.wikimedia.org/w/thumb.php?f={filename}&w={width}"
)


def _wikimedia_thumb_url(filename: str, width: int = _WIKI_THUMB_WIDTH) -> str:
    """
    Build a Wikimedia Commons thumbnail URL from a filename.
    OSM `image` or `wikimedia_commons` tags often contain
    "File:Example.jpg" or just "Example.jpg".
    """
    filename = filename.strip()
    if filename.startswith("File:"):
        filename = filename[5:]
    # URL-encode spaces
    filename = filename.replace(" ", "%20")
    return _COMMONS_URL_TEMPLATE.format(filename=filename, width=width)


def _resolve_thumbnail(tags: Dict[str, Any]) -> Optional[str]:
    """
    Try to derive a small thumbnail URL from OSM tags.
    Priority: wikimedia_commons > image > wikidata (via thumb API).
    Returns a URL string or None.  Never makes network calls - uses
    deterministic URL construction only.
    """
    # 1. Direct Wikimedia Commons file reference
    wmc = tags.get("wikimedia_commons") or tags.get("image")
    if wmc and isinstance(wmc, str):
        wmc = wmc.strip()
        # Only resolve Wikimedia/Commons references, not arbitrary URLs
        if wmc.startswith("File:") or wmc.startswith("Category:"):
            if wmc.startswith("File:"):
                return _wikimedia_thumb_url(wmc)
        elif not wmc.startswith("http"):
            # Bare filename - assume Commons
            return _wikimedia_thumb_url(wmc)
        elif "wikimedia.org" in wmc or "wikipedia.org" in wmc:
            # Already a URL - pass through (client will fetch directly)
            return wmc[:500]

    # 2. Wikidata entity → use Special:FilePath (auto-resolves to main image)
    wd = tags.get("wikidata")
    if wd and isinstance(wd, str) and wd.startswith("Q"):
        return (
            f"https://commons.wikimedia.org/wiki/Special:FilePath/"
            f"?width={_WIKI_THUMB_WIDTH}&wptype=entity&wpvalue={wd}"
        )

    return None


def _route_km_from_polyline(poly6: str) -> float:
    """Approximate route length in km by summing decoded segment distances."""
    pts = decode_polyline6(poly6)
    if not pts or len(pts) < 2:
        return 0.0
    total = 0.0
    for i in range(1, len(pts)):
        total += _haversine_m(
            (float(pts[i - 1][0]), float(pts[i - 1][1])),
            (float(pts[i][0]), float(pts[i][1])),
        )
    return total / 1000.0


def _route_extent_km(poly6: str) -> float:
    """Geographic extent of a route - bbox diagonal in km.

    Unlike _route_km_from_polyline (which sums every segment and grows with
    back-tracking / winding), this measures the *footprint* of the route.
    A 700 km winding loop that only spans ~120 km north-south returns ~120,
    not 700.  Used for the corridor places budget so loopy routes don't
    pull an absurd number of stops.
    """
    pts = decode_polyline6(poly6)
    if not pts or len(pts) < 2:
        return 0.0
    lats = [float(p[0]) for p in pts]
    lngs = [float(p[1]) for p in pts]
    sw = (min(lats), min(lngs))
    ne = (max(lats), max(lngs))
    return _haversine_m(sw, ne) / 1000.0


def _cluster_cap_tier2(
    items: List[PlaceItem],
    samples: List[Tuple[float, float]],
    segment_km: float,
    per_segment: int,
    landmark_names: Set[str] | None = None,
) -> List[PlaceItem]:
    """
    Prevent a dense city from eating all tier-2 slots.

    Divides the route into `segment_km`-length buckets (keyed by the index of
    the nearest sample point).  Within each bucket:
      1. Items are scored by relevance (_score_place).
      2. Category diversity is enforced: no single category gets more than
         half the slots (rounded up), ensuring a mix of dining/nature/culture.
      3. Highest-scoring items are picked first within those constraints.
    """
    if not samples or segment_km <= 0 or per_segment <= 0:
        return items

    # Assign each item to its nearest sample bucket
    buckets: Dict[int, List[Tuple[float, PlaceItem]]] = collections.defaultdict(list)

    for it in items:
        best_idx = 0
        best_d = float("inf")
        for idx, s in enumerate(samples):
            d = _haversine_m((it.lat, it.lng), s)
            if d < best_d:
                best_d = d
                best_idx = idx
        score = _score_place(it, landmark_names)
        buckets[best_idx].append((score, it))

    # Within each bucket: sort by score descending, then pick with diversity
    max_per_cat = max(1, (per_segment + 1) // 2)  # no category > half the slots
    result: List[PlaceItem] = []

    for bucket_items in buckets.values():
        bucket_items.sort(key=lambda x: x[0], reverse=True)
        cat_counts: Dict[str, int] = {}
        picked: List[PlaceItem] = []
        # First pass: pick diverse high-scorers
        for score, it in bucket_items:
            if len(picked) >= per_segment:
                break
            cat = str(it.category)
            if cat_counts.get(cat, 0) < max_per_cat:
                picked.append(it)
                cat_counts[cat] = cat_counts.get(cat, 0) + 1
        # Second pass: fill remaining slots from unpicked items by score
        if len(picked) < per_segment:
            picked_ids = {it.id for it in picked}
            for score, it in bucket_items:
                if len(picked) >= per_segment:
                    break
                if it.id not in picked_ids:
                    picked.append(it)
        result.extend(picked)

    return result


# ──────────────────────────────────────────────────────────────
# Corridor diversity - per-category caps + relevance scoring
# ──────────────────────────────────────────────────────────────
# Max fraction of total budget a single category can consume.
# High-value trip categories (camp, nature) get generous caps;
# low-value high-volume categories (ATM, laundromat) get tight caps.

_CORRIDOR_CAT_CAP_FRACTION: Dict[str, float] = {
    # Safety-critical - generous
    "fuel":             0.15,
    "ev_charging":      0.10,
    "hospital":         0.05,
    "mechanic":         0.05,
    "pharmacy":         0.04,
    "emergency_phone":  0.03,
    # Accommodation - generous, core trip planning
    "camp":             0.15,
    "hotel":            0.08,
    "motel":            0.08,
    "hostel":           0.06,
    # Amenities - important but not unlimited
    "rest_area":        0.08,
    "toilet":           0.05,
    "water":            0.05,
    "water_fill":       0.04,
    "dump_point":       0.04,
    "shower":           0.04,
    "grocery":          0.06,
    "town":             0.08,
    # Low-value / high-volume - tight caps
    "atm":              0.02,
    "laundromat":       0.02,
    "bar":              0.03,
    # Nature & outdoors - generous, these are why people roam
    "national_park":    0.08,
    "viewpoint":        0.08,
    "waterfall":        0.06,
    "swimming_hole":    0.06,
    "beach":            0.06,
    "hiking":           0.06,
    "picnic":           0.05,
    "hot_spring":       0.04,
    "cave":             0.04,
    "fishing":          0.04,
    "surf":             0.04,
    # Food & drink - moderate
    "cafe":             0.05,
    "restaurant":       0.05,
    "bakery":           0.04,
    "fast_food":        0.04,
    "pub":              0.04,
}
_CORRIDOR_CAT_CAP_DEFAULT = 0.04  # 4% for anything not listed


def _corridor_diversify(
    candidates: List[PlaceItem],
    limit: int,
    samples: List[Tuple[float, float]],
) -> List[PlaceItem]:
    """
    Score all candidates, then select up to `limit` with per-category caps
    so no single category dominates the results.

    Strategy:
    1. Score every item (category importance + data richness + proximity)
    2. Group by category
    3. Sort each group by score descending
    4. Round-robin pick from categories, highest-scored first, respecting
       per-category caps
    5. Fill remaining slots with overflow from any category
    """
    if not candidates:
        return []

    # Score and group by category
    scored: List[Tuple[float, PlaceItem]] = []
    for it in candidates:
        s = _score_place(it)
        # Proximity bonus - closer to route = more relevant
        d = _min_distance_to_samples_m(it.lat, it.lng, samples)
        # Items within 5km of route get a bonus, items far away get penalised
        if d < 5000:
            s += 2.0
        elif d < 15000:
            s += 1.0
        elif d > 25000:
            s -= 1.0
        scored.append((s, it))

    # Group by category, sorted by score descending
    by_cat: Dict[str, List[Tuple[float, PlaceItem]]] = collections.defaultdict(list)
    for s, it in scored:
        by_cat[str(it.category)].append((s, it))

    for cat in by_cat:
        by_cat[cat].sort(key=lambda x: x[0], reverse=True)

    # Compute per-category caps
    cat_caps: Dict[str, int] = {}
    for cat in by_cat:
        frac = _CORRIDOR_CAT_CAP_FRACTION.get(cat, _CORRIDOR_CAT_CAP_DEFAULT)
        cap = max(3, int(limit * frac))  # at least 3 per category
        cat_caps[cat] = cap

    # Round-robin pick: cycle through categories, take one from each
    result: List[PlaceItem] = []
    result_ids: set[str] = set()
    cat_taken: Dict[str, int] = collections.defaultdict(int)

    # Sort categories by importance so high-value categories fill first
    cat_order = sorted(
        by_cat.keys(),
        key=lambda c: _CATEGORY_IMPORTANCE.get(c, 1.0),
        reverse=True,
    )

    # Phase 1: Round-robin respecting caps
    pointers: Dict[str, int] = {cat: 0 for cat in cat_order}
    made_progress = True
    while len(result) < limit and made_progress:
        made_progress = False
        for cat in cat_order:
            if len(result) >= limit:
                break
            if cat_taken[cat] >= cat_caps[cat]:
                continue
            idx = pointers[cat]
            items_list = by_cat[cat]
            if idx >= len(items_list):
                continue
            _, it = items_list[idx]
            pointers[cat] = idx + 1
            if it.id not in result_ids:
                result.append(it)
                result_ids.add(it.id)
                cat_taken[cat] += 1
                made_progress = True

    # Phase 2: Fill remaining slots from overflow (ignore caps)
    if len(result) < limit:
        overflow = []
        for cat in cat_order:
            idx = pointers[cat]
            for _, it in by_cat[cat][idx:]:
                if it.id not in result_ids:
                    overflow.append((_score_place(it), it))
        overflow.sort(key=lambda x: x[0], reverse=True)
        for _, it in overflow:
            if len(result) >= limit:
                break
            result.append(it)
            result_ids.add(it.id)

    logger.info(
        "corridor_diversify: candidates=%d selected=%d cats=%d caps=%s",
        len(candidates), len(result), len(by_cat),
        {c: f"{cat_taken[c]}/{cat_caps[c]}" for c in sorted(cat_taken) if cat_taken[c] > 0},
    )

    return result


# ──────────────────────────────────────────────────────────────
# Wikidata background enrichment
# ──────────────────────────────────────────────────────────────

async def _wikidata_enrich_store(store: PlacesStore) -> None:
    """
    Background task: fetch Wikidata P18 (image) + P856 (website) for any
    camp/caravan/heritage/viewpoint places in the store that have a `wikidata`
    OSM tag but haven't been enriched yet (or are stale > 30 days).

    Runs silently - all errors are logged and swallowed so the calling
    request is never affected.
    """
    from app.core.wikidata import enrich_qids

    try:
        candidates = store.query_wikidata_candidates()
    except Exception as exc:
        logger.warning("[wikidata_enrich] query_candidates failed: %s", exc)
        return

    if not candidates:
        return

    # Build Q-ID → (osm_type, osm_id) map
    qid_map: dict[str, tuple[str, int]] = {}
    for osm_type, osm_id, tags in candidates:
        qid = tags.get("wikidata", "")
        if qid and isinstance(qid, str) and qid.startswith("Q"):
            qid_map[qid] = (osm_type, osm_id)

    if not qid_map:
        return

    logger.info("[wikidata_enrich] enriching %d places", len(qid_map))

    try:
        async with http_client(timeout=15.0) as client:
            enrichments = await enrich_qids(list(qid_map.keys()), client=client)
    except Exception as exc:
        logger.warning("[wikidata_enrich] API call failed: %s", exc)
        return

    enriched = 0
    for qid, result in enrichments.items():
        osm_type, osm_id = qid_map[qid]
        try:
            store.apply_wikidata_enrichment(
                osm_type, osm_id,
                thumbnail_url=result.thumbnail_url,
                image_licence=result.image_licence,
                image_attribution=result.image_attribution,
                website=result.website,
            )
            if result.thumbnail_url or result.website:
                enriched += 1
        except Exception as exc:
            logger.warning("[wikidata_enrich] apply failed %s/%s: %s", osm_type, osm_id, exc)

    # Stamp any Q-IDs that had no result so we don't retry them immediately
    for qid, (osm_type, osm_id) in qid_map.items():
        if qid not in enrichments:
            try:
                store.apply_wikidata_enrichment(
                    osm_type, osm_id,
                    thumbnail_url=None, image_licence=None,
                    image_attribution=None, website=None,
                )
            except Exception:
                pass

    logger.info("[wikidata_enrich] done: %d/%d places enriched with image/website",
                enriched, len(qid_map))


# ──────────────────────────────────────────────────────────────
# Service
# ──────────────────────────────────────────────────────────────

class Places:
    """
    Places service - OVERPASS-FIRST corridor search.

    For corridor searches (search_corridor_polyline), the read order is:
      1) Overpass around query (distributed along the actual route)
      2) Local store supplement (fill gaps)
      3) Supa supplement (fill gaps)

    This ensures the full route gets coverage from start to end,
    instead of being dominated by destination-area items that were
    cached by previous bbox-based searches.

    For regular bbox searches (.search()), the order remains:
      1) Local store
      2) Supa
      3) Overpass tile top-up
    """

    def __init__(
        self,
        *,
        cache_conn,
        # ── BUMPED from places.v2.expanded to v3 ──────────────
        # This invalidates all stale corridor packs that were built
        # with the old local-first ordering (destination-biased).
        algo_version: str = "places.v3.overpass_first",
        store: PlacesStore | None = None,
    ):
        self.cache_conn = cache_conn
        self.algo_version = algo_version

        self.store = store or PlacesStore(cache_conn)
        self.store.ensure_schema()

        self.supa: SupaPlacesRepo | None
        if bool(getattr(settings, "supa_enabled", False)):
            self.supa = SupaPlacesRepo()
        else:
            self.supa = None

    # ──────────────────────────────────────────────────────────
    # Supa helpers
    # ──────────────────────────────────────────────────────────

    def _supa_upsert_best_effort(self, items: List[PlaceItem], *, source: str) -> int:
        if self.supa is None or not items:
            return 0
        try:
            logger.debug("supa upsert attempt: n=%d source=%s", len(items), source)
            n = self.supa.upsert_items(items, source=source)
            logger.debug("supa upsert ok: n=%d", n)
            return int(n)
        except Exception as e:
            logger.warning("supa upsert FAILED: %r", e)
            return 0

    def _supa_ingest_best_effort(self, items: List[PlaceItem]) -> None:
        if not items:
            return
        try:
            self.store.upsert_items(items)
        except Exception as e:
            logger.warning("places_store ingest from supa FAILED: %r", e)

    def _finalize_and_cache_pack(self, pack: PlacesPack, *, publish_to_supa: bool) -> PlacesPack:
        if publish_to_supa and self.supa is not None and pack.items:
            cap = int(getattr(settings, "supa_places_publish_cap", 4000))
            cap = max(0, cap)
            subset = pack.items[:cap] if cap else pack.items
            self._supa_upsert_best_effort(subset, source="pack")

        put_places_pack(
            self.cache_conn,
            places_key=pack.places_key,
            created_at=pack.created_at,
            algo_version=self.algo_version,
            pack=pack.model_dump(),
        )
        return pack

    # ──────────────────────────────────────────────────────────
    # Bundle search - two-tier, dynamic budget
    # ──────────────────────────────────────────────────────────
    #
    # Tier 1 (essentials): wide radius (100 km), no cluster cap.
    # Tier 2 (leisure):    tight radius (5 km), cluster-capped so
    #                      one city can't eat all remaining slots.
    #
    # Total budget scales with route length (≈ 3 places/km, 350–2500).
    # ──────────────────────────────────────────────────────────

    def search_bundle(
        self,
        *,
        polyline6: str,
        categories: List[PlaceCategory] | None = None,
        density_multiplier: float = 1.0,
    ) -> PlacesPack:
        """
        Offline-bundle-optimised place search.

        Returns a dynamically-sized, relevance-structured PlacesPack ready
        for ZIP bundling.  Pass `categories` to override the default tier
        split (useful for testing); omit for production use.

        `density_multiplier` scales the total budget (default 1.0 = current
        behaviour).  Values < 1 reduce stops, > 1 increase them.
        Driven by user's stop_density preference (1–5 → 0.15–2.0).
        """
        route_km = _route_km_from_polyline(polyline6)
        if route_km < 1.0:
            # Degenerate route - fall back gracefully
            route_km = 50.0

        total_budget = int(_bundle_places_budget(route_km) * max(0.1, density_multiplier))
        t1_budget = int(total_budget * _BUNDLE_TIER1_FRACTION)
        t2_budget = total_budget - t1_budget

        # Allow caller to override categories (e.g. user-selected interests)
        t1_cats: List[PlaceCategory] = [c for c in (categories or []) if c in _BUNDLE_TIER1_CATS] or _BUNDLE_TIER1_CATS
        t2_cats: List[PlaceCategory] = [c for c in (categories or []) if c in _BUNDLE_TIER2_CATS] or _BUNDLE_TIER2_CATS

        cats_str_t1 = [str(c) for c in t1_cats]
        cats_str_t2 = [str(c) for c in t2_cats]
        cats_str_ci = [str(c) for c in _CRITICAL_INFRA_CATS]
        all_cats_str = sorted(set(cats_str_ci + cats_str_t1 + cats_str_t2))

        logger.info(
            "search_bundle: route_km=%.1f budget=%d (t1=%d t2=%d) "
            "t1_cats=%d t2_cats=%d t1_buf_km=%.0f t2_buf_km=%.0f t2_hv_buf_km=%.0f",
            route_km, total_budget, t1_budget, t2_budget,
            len(t1_cats), len(t2_cats),
            _BUNDLE_TIER1_BUFFER_KM, _BUNDLE_TIER2_BUFFER_KM,
            _BUNDLE_TIER2_HIGH_VALUE_BUFFER_KM,
        )

        pkey = _corridor_places_key(
            polyline6,
            _BUNDLE_TIER1_BUFFER_KM,  # use the wider radius as the cache key discriminator
            all_cats_str,
            total_budget,
            self.algo_version + ".bundle_v2_scored",
        )

        cached = get_places_pack(self.cache_conn, pkey)
        if cached:
            pack = PlacesPack.model_validate(cached)
            logger.info(
                "search_bundle cache HIT: key=%s items=%d", pkey[:16], len(pack.items)
            )
            return pack

        logger.info("search_bundle cache MISS - running two-tier pipeline")

        # ── Sample route at 8 km intervals ───────────────────
        samples = _sample_polyline(polyline6, 8.0, include_endpoints=True)
        if not samples:
            logger.warning("search_bundle: no samples from polyline")
            empty_req = PlacesRequest(
                bbox=BBox4(minLng=0, minLat=0, maxLng=0, maxLat=0),
                categories=t1_cats + t2_cats,
                limit=total_budget,
            )
            return self._finalize_and_cache_pack(
                PlacesPack(
                    places_key=pkey,
                    req=empty_req,
                    items=[],
                    provider="bundle_empty",
                    created_at=utc_now_iso(),
                    algo_version=self.algo_version,
                ),
                publish_to_supa=False,
            )

        t1_buffer_m = _BUNDLE_TIER1_BUFFER_KM * 1000.0
        t2_buffer_m = _BUNDLE_TIER2_BUFFER_KM * 1000.0
        t2_hv_buffer_m = _BUNDLE_TIER2_HIGH_VALUE_BUFFER_KM * 1000.0
        wide_bbox   = _bbox_around_points(samples, _BUNDLE_TIER1_BUFFER_KM)
        # Use the wider high-value buffer for the tier-2 bbox so we can catch
        # destination-worthy spots further off the road
        tight_bbox  = _bbox_around_points(samples, _BUNDLE_TIER2_HIGH_VALUE_BUFFER_KM)

        seen_ids: set[str] = set()
        t1_items: List[PlaceItem] = []
        t2_items: List[PlaceItem] = []

        timeout_s = float(getattr(settings, "overpass_timeout_s", 90))
        timeout   = httpx.Timeout(timeout_s, connect=15.0)

        def _within(it: PlaceItem, buf_m: float) -> bool:
            return (
                it.id not in seen_ids
                and _min_distance_to_samples_m(it.lat, it.lng, samples) <= buf_m
            )

        def _accept(lst: List[PlaceItem], it: PlaceItem) -> None:
            seen_ids.add(it.id)
            lst.append(it)

        # ═══════════════════════════════════════════════════════
        # OVERPASS - BOTH TIERS IN PARALLEL
        # Tier 1 (essentials, wide) and Tier 2 (leisure, tight)
        # are independent queries - fire them concurrently so the
        # total wait is max(t1, t2) instead of t1 + t2.
        # ═══════════════════════════════════════════════════════

        def _overpass_fetch_tier(
            filters: List[str],
            radius_m: float,
            label: str,
        ) -> List[PlaceItem]:
            if not filters:
                return []
            ql = _build_overpass_around_ql(
                coords=samples,
                radius_m=radius_m,
                filters=filters,
                name_clause="",
            )
            logger.info(
                "search_bundle %s Overpass: samples=%d radius_m=%.0f filters=%d ql_len=%d",
                label, len(samples), radius_m, len(filters), len(ql),
            )
            try:
                data = _fetch_overpass_with_retries(client=None, ql=ql, label=f"bundle_{label}")  # type: ignore[arg-type]
                items: List[PlaceItem] = []
                for el in data.get("elements") or []:
                    it = _element_to_item(el)
                    if it is not None:
                        items.append(it)
                logger.info("search_bundle %s Overpass raw=%d", label, len(items))
                return items
            except Exception as e:
                logger.warning("search_bundle %s Overpass FAILED: %r", label, e)
                return []

        t1_filters = _overpass_filters_for_categories(t1_cats)
        t2_filters = _overpass_filters_for_categories(t2_cats)

        # Critical infrastructure (fuel + EV) get their own dedicated query with
        # a higher coord limit.  Their 2 OSM filters keep the query small even at
        # 300 sample points, so they are never crowded out by the larger tier-1
        # query timing out on long routes.
        ci_cats: List[PlaceCategory] = list(_CRITICAL_INFRA_CATS)
        ci_filters = _overpass_filters_for_categories(ci_cats)
        ci_buffer_m = _CRITICAL_INFRA_BUFFER_KM * 1000.0

        def _overpass_fetch_critical() -> List[PlaceItem]:
            if not ci_filters:
                return []
            ql = _build_overpass_around_ql(
                coords=samples,
                radius_m=ci_buffer_m,
                filters=ci_filters,
                name_clause="",
                max_coords=_CRITICAL_INFRA_MAX_COORDS,
            )
            logger.info(
                "search_bundle critical Overpass: samples=%d radius_m=%.0f filters=%d ql_len=%d",
                len(samples), ci_buffer_m, len(ci_filters), len(ql),
            )
            try:
                data = _fetch_overpass_with_retries(client=None, ql=ql, label="bundle_critical")  # type: ignore[arg-type]
                items: List[PlaceItem] = []
                for el in data.get("elements") or []:
                    it = _element_to_item(el)
                    if it is not None:
                        items.append(it)
                logger.info("search_bundle critical Overpass raw=%d", len(items))
                return items
            except Exception as e:
                logger.warning("search_bundle critical Overpass FAILED: %r", e)
                return []

        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
            f1 = pool.submit(_overpass_fetch_tier, t1_filters, t1_buffer_m, "tier1")
            # Fetch tier 2 with the wider high-value buffer - the per-item
            # acceptance filter below applies the tight buffer to generic
            # categories while letting high-value destinations through at 15 km.
            f2 = pool.submit(_overpass_fetch_tier, t2_filters, t2_hv_buffer_m, "tier2")
            f_ci = pool.submit(_overpass_fetch_critical)
            t1_fetched = f1.result()
            t2_fetched = f2.result()
            ci_fetched = f_ci.result()

        # Persist all sets to local store + Supa (best-effort, non-blocking order)
        all_fetched = ci_fetched + t1_fetched + t2_fetched
        if all_fetched:
            try:
                self.store.upsert_items(all_fetched)
            except Exception as e:
                logger.warning("search_bundle store upsert FAILED: %r", e)
            self._supa_upsert_best_effort(all_fetched, source="bundle_overpass")
            # Fire-and-forget: runs in a daemon thread because this method
            # is sync (no running event loop in the threadpool worker).
            import threading
            threading.Thread(
                target=lambda: asyncio.run(_wikidata_enrich_store(self.store)),
                daemon=True,
            ).start()

        # ── Resolve regional landmarks for scoring ────────────
        landmark_names = _landmarks_for_route(samples)
        if landmark_names:
            logger.info(
                "search_bundle: %d landmark names from %d route regions",
                len(landmark_names),
                len(landmark_names),  # approximate; exact region count not needed
            )

        # ═══════════════════════════════════════════════════════
        # TIER 1 - collect all candidates, score, sort, accept
        # Critical infra (fuel/EV) are collected alongside other
        # tier-1 items, then the combined pool is ranked by score.
        # ═══════════════════════════════════════════════════════

        ci_cats_set = set(ci_cats)
        t1_cats_set = set(t1_cats)
        t1_all_cats = list(ci_cats) + list(t1_cats)
        t1_all_cats_set = set(t1_all_cats)

        # Collect all tier-1 candidates (deduped, within buffer)
        t1_candidates: List[PlaceItem] = []
        t1_seen: set[str] = set()

        def _collect_t1(it: PlaceItem, buf_m: float) -> None:
            if it.id not in t1_seen and _min_distance_to_samples_m(it.lat, it.lng, samples) <= buf_m:
                if it.category in t1_all_cats_set:
                    t1_candidates.append(it)
                    t1_seen.add(it.id)

        for it in ci_fetched:
            _collect_t1(it, ci_buffer_m)
        for it in t1_fetched:
            _collect_t1(it, t1_buffer_m)

        # Supplement from local store if Overpass was thin
        if len(t1_candidates) < t1_budget:
            try:
                local = self.store.query_bbox(
                    bbox=wide_bbox, categories=t1_all_cats, limit=t1_budget * 2,
                )
                for it in local:
                    _collect_t1(it, t1_buffer_m)
            except Exception as e:
                logger.warning("search_bundle tier1 local FAILED: %r", e)

        if self.supa is not None and len(t1_candidates) < t1_budget:
            try:
                supa = self.supa.query_bbox(
                    bbox=wide_bbox, categories=t1_all_cats, limit=t1_budget * 2,
                )
                if supa:
                    self._supa_ingest_best_effort(supa)
                    for it in supa:
                        _collect_t1(it, t1_buffer_m)
            except Exception as e:
                logger.warning("search_bundle tier1 supa FAILED: %r", e)

        # Critical infra (fuel/EV) are RESERVED - never budget-squeezed.
        # They get their own dedicated Overpass query precisely because they
        # are safety-critical; dropping them causes "No fuel ahead" while
        # fuel icons are visible on the map via the fuel overlay layer.
        ci_reserved = [it for it in t1_candidates if it.category in ci_cats_set]
        ci_reserved_ids = {it.id for it in ci_reserved}
        t1_non_ci = [it for it in t1_candidates if it.id not in ci_reserved_ids]

        # Sort non-CI by relevance score, then fill remaining budget
        t1_non_ci.sort(
            key=lambda it: _score_place(it, landmark_names), reverse=True,
        )
        remaining_budget = max(0, t1_budget - len(ci_reserved))
        t1_items = ci_reserved + t1_non_ci[:remaining_budget]
        seen_ids.update(it.id for it in t1_items)

        logger.info("search_bundle tier1 DONE: candidates=%d ci_reserved=%d accepted=%d / budget=%d",
                     len(t1_candidates), len(ci_reserved), len(t1_items), t1_budget)

        # ═══════════════════════════════════════════════════════
        # TIER 2 - collect all candidates, then diversity+score
        # cluster cap handles both scoring and category diversity.
        # ═══════════════════════════════════════════════════════

        t2_cats_set = set(t2_cats)
        t2_raw: List[PlaceItem] = []

        def _t2_buffer_for(cat: str) -> float:
            """High-value categories get the wider search radius."""
            return t2_hv_buffer_m if cat in _BUNDLE_TIER2_HIGH_VALUE_CATS else t2_buffer_m

        for it in t2_fetched:
            buf = _t2_buffer_for(str(it.category))
            if _within(it, buf) and it.category in t2_cats_set:
                t2_raw.append(it)
                seen_ids.add(it.id)

        if len(t2_raw) < t2_budget * 3:
            try:
                local = self.store.query_bbox(
                    bbox=tight_bbox, categories=t2_cats, limit=t2_budget * 3,
                )
                for it in local:
                    buf = _t2_buffer_for(str(it.category))
                    if it.id not in seen_ids and _within(it, buf):
                        t2_raw.append(it)
                        seen_ids.add(it.id)
            except Exception as e:
                logger.warning("search_bundle tier2 local FAILED: %r", e)

        if self.supa is not None and len(t2_raw) < t2_budget * 3:
            try:
                supa = self.supa.query_bbox(
                    bbox=tight_bbox, categories=t2_cats, limit=t2_budget * 3,
                )
                if supa:
                    self._supa_ingest_best_effort(supa)
                    for it in supa:
                        buf = _t2_buffer_for(str(it.category))
                        if it.id not in seen_ids and _within(it, buf):
                            t2_raw.append(it)
                            seen_ids.add(it.id)
            except Exception as e:
                logger.warning("search_bundle tier2 supa FAILED: %r", e)

        # Apply diversity-aware, score-ranked cluster cap then slice to budget
        t2_capped = _cluster_cap_tier2(
            t2_raw, samples,
            segment_km=_BUNDLE_TIER2_CLUSTER_KM,
            per_segment=_BUNDLE_TIER2_PER_CLUSTER,
            landmark_names=landmark_names,
        )
        t2_items = t2_capped[:t2_budget]

        logger.info(
            "search_bundle tier2 DONE: raw=%d after_cap=%d accepted=%d / budget=%d",
            len(t2_raw), len(t2_capped), len(t2_items), t2_budget,
        )

        # ═══════════════════════════════════════════════════════
        # MERGE & FINALISE
        # ═══════════════════════════════════════════════════════

        all_items = t1_items + t2_items

        logger.info(
            "search_bundle FINAL: route_km=%.1f budget=%d t1=%d t2=%d total=%d",
            route_km, total_budget, len(t1_items), len(t2_items), len(all_items),
        )

        bundle_req = PlacesRequest(
            bbox=wide_bbox,
            categories=list(ci_cats) + list(t1_cats) + list(t2_cats),
            limit=total_budget,
        )
        pack = PlacesPack(
            places_key=pkey,
            req=bundle_req,
            items=all_items,
            provider="bundle_v2_scored",
            created_at=utc_now_iso(),
            algo_version=self.algo_version,
        )
        return self._finalize_and_cache_pack(pack, publish_to_supa=True)

    # ──────────────────────────────────────────────────────────
    # Corridor-aware route search
    # ──────────────────────────────────────────────────────────

    def search_corridor_polyline(
        self,
        *,
        polyline6: str,
        buffer_km: float = 35.0,
        categories: List[PlaceCategory],
        limit: int = 8000,
        sample_interval_km: float = 8.0,
    ) -> PlacesPack:
        cats_str = [str(c) for c in categories]

        logger.info(
            "search_corridor_polyline: polyline_len=%d buffer_km=%s cats=%d limit=%d interval_km=%s",
            len(polyline6), buffer_km, len(categories), limit, sample_interval_km,
        )

        pkey = _corridor_places_key(
            polyline6, buffer_km, cats_str, limit, self.algo_version,
        )

        # ── 0) Pack cache ────────────────────────────────────
        cached = get_places_pack(self.cache_conn, pkey)
        if cached:
            pack = PlacesPack.model_validate(cached)
            logger.info("corridor cache HIT: key=%s items=%d provider=%s", pkey[:16], len(pack.items), pack.provider)
            # Migrate to supa if needed (best-effort)
            migrate_cached = bool(getattr(settings, "supa_places_publish_cached_packs", True))
            if (
                migrate_cached
                and self.supa is not None
                and pack.items
                and ("supa" not in (pack.provider or ""))
            ):
                cap = int(getattr(settings, "supa_places_publish_cap", 4000))
                subset = pack.items[:cap] if cap else pack.items
                self._supa_upsert_best_effort(subset, source="cached_pack")
            return pack

        logger.info("corridor cache MISS - running full pipeline")

        # ── 1) Sample route ──────────────────────────────────
        samples = _sample_polyline(
            polyline6, sample_interval_km, include_endpoints=True,
        )
        if not samples:
            logger.warning("corridor: no samples from polyline - returning empty")
            empty_req = PlacesRequest(
                bbox=BBox4(minLng=0, minLat=0, maxLng=0, maxLat=0),
                categories=categories,
                limit=limit,
            )
            empty_pack = PlacesPack(
                places_key=pkey,
                req=empty_req,
                items=[],
                provider="corridor_empty",
                created_at=utc_now_iso(),
                algo_version=self.algo_version,
            )
            return self._finalize_and_cache_pack(empty_pack, publish_to_supa=False)

        buffer_m = buffer_km * 1000.0
        corridor_bbox = _bbox_around_points(samples, buffer_km)

        # Collect ALL candidates first, then score + cap for diversity
        all_candidates: List[PlaceItem] = []
        seen_ids: set[str] = set()

        def _dedup_add(source: List[PlaceItem]) -> int:
            """Add items to candidates, dedup by id, filter by corridor buffer."""
            added = 0
            for it in source:
                if it.id in seen_ids:
                    continue
                d = _min_distance_to_samples_m(it.lat, it.lng, samples)
                if d <= buffer_m:
                    seen_ids.add(it.id)
                    all_candidates.append(it)
                    added += 1
            return added

        # ──────────────────────────────────────────────────────
        # STEP 2: LOCAL STORE FIRST
        # ──────────────────────────────────────────────────────
        # Query local store first. If it has good coverage, we
        # can skip or reduce the Overpass query - this avoids
        # rate-limiting and makes repeat/nearby routes instant.
        # ──────────────────────────────────────────────────────

        provider_used = "corridor"
        overpass_items_total = 0
        used_overpass = False

        try:
            local_items = self.store.query_bbox(
                bbox=corridor_bbox, categories=categories, limit=limit * 3,
            )
        except Exception as e:
            logger.warning("corridor places_store query_bbox FAILED: %r", e)
            local_items = []

        local_count = _dedup_add(local_items)

        # Count critical infra from local store to decide if Overpass is needed
        ci_cats_set = set(str(c) for c in _CRITICAL_INFRA_CATS)
        local_ci_count = sum(1 for it in all_candidates if str(it.category) in ci_cats_set)
        local_satisfy_ratio = float(getattr(settings, "places_local_satisfy_ratio", 0.70))
        local_coverage_good = len(all_candidates) >= int(limit * local_satisfy_ratio)

        logger.info(
            "corridor local-first: local=%d ci=%d limit=%d satisfy_ratio=%.0f%% coverage_good=%s",
            local_count, local_ci_count, limit, local_satisfy_ratio * 100, local_coverage_good,
        )

        # ──────────────────────────────────────────────────────
        # STEP 2.5: SUPABASE SUPPLEMENT (before Overpass)
        # ──────────────────────────────────────────────────────
        # Query Supabase first - it has accumulated POI data from
        # all previous corridor/bundle queries.  This is fast
        # (~200ms) and may provide enough coverage to skip
        # Overpass entirely.
        # ──────────────────────────────────────────────────────
        supa_hit = 0
        if self.supa is not None and len(all_candidates) < limit:
            try:
                supa_items = self.supa.query_bbox(
                    bbox=corridor_bbox, categories=categories, limit=limit * 2,
                )
                if supa_items:
                    supa_hit = _dedup_add(supa_items)
                    self._supa_ingest_best_effort(supa_items)
                    logger.info("corridor supa-first: got %d items, total candidates=%d",
                               supa_hit, len(all_candidates))
            except Exception as e:
                logger.warning("supa corridor query_bbox FAILED: %r", e)

        # Re-evaluate coverage after Supabase supplement
        local_ci_count = sum(1 for it in all_candidates if str(it.category) in ci_cats_set)
        local_coverage_good = len(all_candidates) >= int(limit * local_satisfy_ratio)

        logger.info(
            "corridor after supa: candidates=%d ci=%d coverage_good=%s",
            len(all_candidates), local_ci_count, local_coverage_good,
        )

        # ──────────────────────────────────────────────────────
        # STEP 3: OVERPASS (only if local+supa coverage is thin)
        # ──────────────────────────────────────────────────────
        # Only hit Overpass if local store + Supabase don't have
        # enough data.  This means repeat queries and popular
        # corridors skip Overpass entirely.
        # ──────────────────────────────────────────────────────

        timeout_s = float(getattr(settings, "overpass_timeout_s", 90))
        timeout = httpx.Timeout(timeout_s, connect=15.0)

        ci_cats_in_req = [c for c in _CRITICAL_INFRA_CATS if c in set(categories)]
        non_ci_cats = [c for c in categories if c not in set(_CRITICAL_INFRA_CATS)]

        def _corridor_overpass_fetch(cats: List[PlaceCategory], max_coords: int, label: str) -> List[PlaceItem]:
            f = _overpass_filters_for_categories(cats)
            if not f and not cats:
                return []
            ql = _build_overpass_around_ql(
                coords=samples,
                radius_m=buffer_m,
                filters=f,
                name_clause="",
                max_coords=max_coords,
            )
            logger.info(
                "corridor Overpass %s: samples=%d radius_m=%s filters=%d ql_len=%d",
                label, len(samples), buffer_m, len(f), len(ql),
            )
            try:
                data = _fetch_overpass_with_retries(client=None, ql=ql, label=f"corridor_{label}")  # type: ignore[arg-type]
                result: List[PlaceItem] = []
                for el in data.get("elements") or []:
                    it = _element_to_item(el)
                    if it is not None:
                        result.append(it)
                logger.info("corridor Overpass %s raw=%d", label, len(result))
                return result
            except Exception as e:
                logger.warning("corridor Overpass %s FAILED: %r", label, e)
                return []

        # Critical infra always runs (fuel/EV are safety-critical).
        # Main categories only run if local store coverage is thin.
        ci_fetched_corr: List[PlaceItem] = []
        main_fetched: List[PlaceItem] = []

        # Run critical infra first (fast, small query), then main if needed
        if ci_cats_in_req and local_ci_count < 10:
            ci_fetched_corr = _corridor_overpass_fetch(
                ci_cats_in_req, _CRITICAL_INFRA_MAX_COORDS, "critical"
            )

        if non_ci_cats and not local_coverage_good:
            # Split main categories into smaller batches to keep each
            # Overpass query lightweight.  For long corridors (many
            # samples) we need more batches so each stays under the QL
            # size / server-time budget.
            n_batches = max(2, min(4, 1 + len(samples) // 60))
            chunk_size = max(1, len(non_ci_cats) // n_batches + 1)
            cat_batches = [
                non_ci_cats[i : i + chunk_size]
                for i in range(0, len(non_ci_cats), chunk_size)
            ]
            # Reduce max_coords for main batches - 50 is plenty for
            # the around filter and keeps the query fast.
            main_max_coords = 50

            with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(cat_batches), 3)) as pool:
                futures = {}
                for idx, batch in enumerate(cat_batches):
                    if batch:
                        lbl = f"main-{chr(ord('a') + idx)}"
                        futures[lbl] = pool.submit(
                            _corridor_overpass_fetch, batch, main_max_coords, lbl,
                        )
                for lbl, fut in futures.items():
                    main_fetched.extend(fut.result())

        fetched = ci_fetched_corr + main_fetched
        if fetched:
            used_overpass = True
            overpass_items_total = len(fetched)

            # Persist to local store for future queries
            try:
                self.store.upsert_items(fetched)
            except Exception as e:
                logger.warning("corridor places_store upsert FAILED: %r", e)
            # Fire-and-forget: runs in a daemon thread because this method
            # is sync (no running event loop in the threadpool worker).
            import threading
            threading.Thread(
                target=lambda: asyncio.run(_wikidata_enrich_store(self.store)),
                daemon=True,
            ).start()

            # Publish to supa (best-effort)
            self._supa_upsert_best_effort(fetched, source="overpass_corridor")

            _dedup_add(fetched)

            logger.info(
                "corridor after Overpass: candidates=%d critical=%d main=%d",
                len(all_candidates), len(ci_fetched_corr), len(main_fetched),
            )
        elif local_coverage_good:
            logger.info(
                "corridor: skipped Overpass (local coverage good: %d items, %d ci)",
                local_count, local_ci_count,
            )

        # (Step 4 moved to Step 2.5 above - Supabase now runs BEFORE Overpass)

        # ──────────────────────────────────────────────────────
        # STEP 5: SCORE, CAP PER-CATEGORY, AND SELECT
        # ──────────────────────────────────────────────────────
        # Score every candidate for relevance, then enforce
        # per-category caps so low-value high-volume categories
        # (ATMs, banks, toilets) don't drown out campsites,
        # viewpoints, and other trip-worthy places.
        # ──────────────────────────────────────────────────────

        items = _corridor_diversify(all_candidates, limit, samples)

        if supa_hit > 0:
            provider_used = f"{provider_used}+supa"
        if used_overpass:
            provider_used = (
                f"{provider_used}+overpass"
                if "overpass" not in provider_used
                else provider_used
            )

        logger.info(
            "corridor_polyline FINAL: provider=%s samples=%d overpass_raw=%d local_first=%d supa_supplement=%d candidates=%d selected=%d",
            provider_used,
            len(samples),
            overpass_items_total,
            local_count,
            supa_hit,
            len(all_candidates),
            len(items),
        )

        corridor_req = PlacesRequest(
            bbox=corridor_bbox,
            categories=categories,
            limit=limit,
        )

        pack = PlacesPack(
            places_key=pkey,
            req=corridor_req,
            items=items[:limit],
            provider=provider_used,
            created_at=utc_now_iso(),
            algo_version=self.algo_version,
        )

        return self._finalize_and_cache_pack(pack, publish_to_supa=True)

    # ──────────────────────────────────────────────────────────
    # Original bbox-based search (unchanged)
    # ──────────────────────────────────────────────────────────

    def search(self, req: PlacesRequest) -> PlacesPack:
        pkey = places_key(req.model_dump(), self.algo_version)

        cached = get_places_pack(self.cache_conn, pkey)
        if cached:
            pack = PlacesPack.model_validate(cached)

            migrate_cached = bool(getattr(settings, "supa_places_publish_cached_packs", True))
            if migrate_cached and self.supa is not None and pack.items and ("supa" not in (pack.provider or "")):
                cap = int(getattr(settings, "supa_places_publish_cap", 4000))
                subset = pack.items[:cap] if cap else pack.items
                self._supa_upsert_best_effort(subset, source="cached_pack")

            return pack

        bbox = _bbox_from_req(req)
        if not bbox:
            pack = PlacesPack(
                places_key=pkey,
                req=req,
                items=[],
                provider="local",
                created_at=utc_now_iso(),
                algo_version=self.algo_version,
            )
            return self._finalize_and_cache_pack(pack, publish_to_supa=False)

        limit = int(req.limit or 50)
        limit = max(1, min(limit, int(getattr(settings, "places_hard_cap", 12000))))
        cats = req.categories or []

        min_ratio = float(getattr(settings, "places_local_satisfy_ratio", 0.70))
        need_count = max(1, int(limit * min_ratio))

        items: List[PlaceItem] = []
        seen_ids: set[str] = set()

        # 1) local store
        try:
            local_items = self.store.query_bbox(bbox=bbox, categories=cats, limit=limit)
        except Exception as e:
            logger.warning("PlacesStore.query_bbox FAILED: %r", e)
            local_items = []

        for it in local_items:
            if it.id in seen_ids:
                continue
            seen_ids.add(it.id)
            items.append(it)
            if len(items) >= limit:
                break

        if len(items) >= need_count:
            pack = PlacesPack(
                places_key=pkey,
                req=req,
                items=items[:limit],
                provider="local",
                created_at=utc_now_iso(),
                algo_version=self.algo_version,
            )
            return self._finalize_and_cache_pack(pack, publish_to_supa=True)

        # 2) Supabase read-through
        provider_used = "local"
        supa_hit = 0
        if self.supa is not None:
            try:
                supa_items = self.supa.query_bbox(bbox=bbox, categories=cats, limit=limit)
                if supa_items:
                    supa_hit = len(supa_items)
                    self._supa_ingest_best_effort(supa_items)

                    for it in supa_items:
                        if it.id in seen_ids:
                            continue
                        seen_ids.add(it.id)
                        items.append(it)
                        if len(items) >= limit:
                            break

                    provider_used = "local+supa"
            except Exception as e:
                logger.warning("SupaPlacesRepo.query_bbox FAILED: %r", e)

        if len(items) >= need_count:
            pack = PlacesPack(
                places_key=pkey,
                req=req,
                items=items[:limit],
                provider=provider_used,
                created_at=utc_now_iso(),
                algo_version=self.algo_version,
            )
            return self._finalize_and_cache_pack(pack, publish_to_supa=("supa" not in provider_used))

        # 3) Overpass top-up
        filters = _overpass_filters_for_categories(cats)
        name_clause = ""
        if req.query:
            safe = _safe_overpass_name_regex(req.query)
            if safe:
                name_clause = f'["name"~"{safe}",i]'

        if not filters and not name_clause:
            pack = PlacesPack(
                places_key=pkey,
                req=req,
                items=items[:limit],
                provider=provider_used,
                created_at=utc_now_iso(),
                algo_version=self.algo_version,
            )
            return self._finalize_and_cache_pack(pack, publish_to_supa=("supa" not in provider_used))

        tile_step = float(getattr(settings, "places_tile_step_deg", 0.15))
        max_tiles = int(getattr(settings, "places_max_tiles", 64))
        throttle_s = float(getattr(settings, "overpass_throttle_s", 0.20))
        ttl_s = int(getattr(settings, "places_tile_ttl_s", 60 * 60 * 24 * 14))
        time_budget_s = float(getattr(settings, "places_time_budget_s", 10.0))
        max_overpass_tiles = int(getattr(settings, "places_max_overpass_tiles_per_req", 12))

        tiles = self.store.tiles_for_bbox(bbox=bbox, step_deg=tile_step, max_tiles=max_tiles)

        timeout_s = float(getattr(settings, "overpass_timeout_s", 90))
        timeout = httpx.Timeout(timeout_s, connect=10.0)

        started = time.time()
        tiles_fetched = 0
        used_overpass = False
        total_overpass_items = 0
        total_supa_published = 0

        try:
            for (tile_key, tb) in tiles:
                if len(items) >= limit:
                    break

                if (time.time() - started) >= time_budget_s and tiles_fetched > 0:
                    break

                if self.store.tile_is_fresh(tile_key=tile_key, ttl_s=ttl_s):
                    continue

                ql = _build_overpass_ql(bbox=tb, filters=filters, name_clause=name_clause)
                data = _fetch_overpass_with_retries(client=None, ql=ql, label="tile")  # type: ignore[arg-type]

                fetched_items: List[PlaceItem] = []
                got = 0
                for el in (data.get("elements") or []):
                    it = _element_to_item(el)
                    if not it:
                        continue
                    fetched_items.append(it)
                    got += 1

                if fetched_items:
                    used_overpass = True
                    total_overpass_items += len(fetched_items)

                    try:
                        self.store.upsert_items(fetched_items)
                    except Exception as e:
                        logger.warning("PlacesStore.upsert_items FAILED: %r", e)
                    # Fire-and-forget wikidata enrichment in a background thread.
                    import threading
                    threading.Thread(
                        target=lambda: asyncio.run(_wikidata_enrich_store(self.store)),
                        daemon=True,
                    ).start()

                    total_supa_published += self._supa_upsert_best_effort(
                        fetched_items,
                        source="overpass",
                    )

                try:
                    self.store.mark_tile_fetched(
                        tile_key=tile_key,
                        bbox=tb,
                        categories=cats,
                        item_count=int(got),
                    )
                except Exception as e:
                    logger.warning("PlacesStore.mark_tile_fetched FAILED: %r", e)

                for it in fetched_items:
                    if it.id in seen_ids:
                        continue
                    seen_ids.add(it.id)
                    items.append(it)
                    if len(items) >= limit:
                        break

                tiles_fetched += 1

                if tiles_fetched >= max_overpass_tiles:
                    break
                if throttle_s > 0:
                    time.sleep(throttle_s)

        except Exception as e:
            logger.warning("overpass loop FAILED: %r", e)

        if used_overpass:
            if provider_used == "local+supa":
                final_provider = "local+supa+overpass"
            else:
                final_provider = "local+overpass"
        else:
            final_provider = provider_used

        if used_overpass and self.supa is not None and total_supa_published > 0:
            if "supa" not in final_provider:
                final_provider = final_provider.replace("local", "local+supa")

        if used_overpass or supa_hit > 0:
            logger.info(
                "search summary: provider=%s local=%d supa_hit=%d overpass_tiles=%d overpass_items=%d supa_published=%d",
                final_provider,
                len(local_items),
                supa_hit,
                tiles_fetched,
                total_overpass_items,
                total_supa_published,
            )

        pack = PlacesPack(
            places_key=pkey,
            req=req,
            items=items[:limit],
            provider=final_provider,
            created_at=utc_now_iso(),
            algo_version=self.algo_version,
        )

        return self._finalize_and_cache_pack(pack, publish_to_supa=True)

    # ──────────────────────────────────────────────────────────
    # Suggest along route
    # ──────────────────────────────────────────────────────────

    def suggest_along_route(
        self,
        *,
        polyline6: str,
        interval_km: int,
        radius_m: int,
        categories: List[PlaceCategory],
        limit_per_sample: int,
    ) -> List[dict]:
        samples = _sample_route_points(polyline6, interval_km)
        out: List[dict] = []

        for (idx, lat, lng, km) in samples:
            preq = PlacesRequest(
                center={"lat": lat, "lng": lng},
                radius_m=int(radius_m),
                categories=categories,
                limit=int(limit_per_sample),
            )
            pack = self.search(preq)
            out.append(
                {
                    "idx": idx,
                    "lat": lat,
                    "lng": lng,
                    "km_from_start": km,
                    "places": pack,
                }
            )

        return out

    def suggest_stops(
        self,
        *,
        bbox: BBox4,
        midpoint: Tuple[float, float],
        existing_categories: List[PlaceCategory],
        limit: int = 4,
    ) -> List[StopSuggestionItem]:
        """Return up to `limit` POI suggestions for the trip stop list.

        Strategy:
          1. Pick candidate categories - prioritise types NOT already in the trip
             (category diversity), but include under-represented essentials too.
          2. Query Overpass within the trip bounding box for those categories.
          3. Score each candidate: proximity to route midpoint + category importance
             + richness signals, penalised if the category is already well represented.
          4. Deduplicate by category (one suggestion per category) then return top N.
        """
        existing_set: Set[str] = set(str(c) for c in existing_categories)

        # ── 1. Choose candidate categories ───────────────────────────────
        # High-value suggestion categories ordered by trip-worthiness.
        CANDIDATE_ORDER: List[PlaceCategory] = [
            # Destination highlights (most likely to be interesting additions)
            "viewpoint", "waterfall", "beach", "swimming_hole", "hot_spring",
            "national_park", "cave", "hiking", "fishing", "surf",
            "museum", "heritage", "winery", "brewery", "attraction",
            # Practical essentials people forget to add
            "camp", "fuel", "grocery",
            "cafe", "restaurant", "pub",
            "picnic", "park",
        ]

        # Select up to 10 candidate categories - prefer those NOT in existing stops,
        # but include a few existing ones if they're essential infrastructure.
        ALWAYS_SUGGEST: Set[str] = {"fuel", "camp", "grocery"}
        candidates: List[PlaceCategory] = []
        for cat in CANDIDATE_ORDER:
            if cat not in existing_set:
                candidates.append(cat)
            elif cat in ALWAYS_SUGGEST:
                candidates.append(cat)
            if len(candidates) >= 10:
                break

        if not candidates:
            # Fallback: just use high-value categories
            candidates = ["viewpoint", "waterfall", "beach", "national_park", "camp"]

        # ── 2. Overpass bbox query (two parallel chunks) ──────────────────
        # Split candidates into two groups so both can be fetched concurrently,
        # roughly halving wall-clock time for large bboxes.
        split = len(candidates) // 2 or len(candidates)
        chunks = [candidates[:split], candidates[split:]] if len(candidates) > split else [candidates]
        candidate_set = {str(c) for c in candidates}

        def _fetch_chunk(chunk: List[PlaceCategory]) -> List[PlaceItem]:
            f = _overpass_filters_for_categories(chunk)
            q = _build_overpass_ql(bbox=bbox, filters=f, name_clause="")
            data = _fetch_overpass_with_retries(client=None, ql=q, label="suggest_stops")  # type: ignore[arg-type]
            out: List[PlaceItem] = []
            for el in data.get("elements", []):
                item = _element_to_item(el)
                if item and str(item.category) in candidate_set:
                    out.append(item)
            return out

        items: List[PlaceItem] = []
        all_failed = True
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(chunks)) as pool:
            futures = [pool.submit(_fetch_chunk, chunk) for chunk in chunks]
            for fut in concurrent.futures.as_completed(futures):
                try:
                    items.extend(fut.result())
                    all_failed = False
                except Exception as exc:
                    logger.warning("suggest_stops overpass chunk failed: %s", exc)

        if all_failed:
            return []

        if not items:
            return []

        # ── 3. Score candidates ───────────────────────────────────────────
        mid_lat, mid_lng = midpoint
        # Max distance from midpoint to bbox corner (normalisation factor)
        max_dist_m = max(
            _haversine_m((mid_lat, mid_lng), (bbox.minLat, bbox.minLng)),
            _haversine_m((mid_lat, mid_lng), (bbox.maxLat, bbox.maxLng)),
            1000.0,
        )

        scored: List[Tuple[float, PlaceItem]] = []
        for item in items:
            base = _score_place(item)

            # Proximity bonus: items near the midpoint score higher
            dist_m = _haversine_m((item.lat, item.lng), (mid_lat, mid_lng))
            proximity = 1.0 - min(1.0, dist_m / max_dist_m)  # 0–1, higher = closer
            base += proximity * 3.0

            # Diversity penalty: already have this category in the trip
            if str(item.category) in existing_set:
                base *= 0.4

            scored.append((base, item))

        scored.sort(key=lambda x: x[0], reverse=True)

        # ── 4. Deduplicate by category, return top N ──────────────────────
        seen_cats: Set[str] = set()
        result: List[StopSuggestionItem] = []
        for score, item in scored:
            cat_str = str(item.category)
            if cat_str in seen_cats:
                continue
            seen_cats.add(cat_str)
            result.append(
                StopSuggestionItem(
                    id=item.id,
                    name=item.name,
                    lat=item.lat,
                    lng=item.lng,
                    category=item.category,
                    score=round(score, 2),
                    extra=item.extra or {},
                )
            )
            if len(result) >= limit:
                break

        return result
