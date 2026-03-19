# app/services/fuel.py
"""
Fuel intelligence service for Roam.

Data sources (all government / open data - no IP concerns):
  - NSW FuelCheck V2 (NSW+ACT+TAS)  - Government-mandated station-level prices
                          https://api.nsw.gov.au/FuelPriceCheck/v2
                          V2 endpoints cover NSW and Tasmania on the same platform.
  - WA FuelWatch (WA)               - Government-mandated station-level prices via RSS
                          https://www.fuelwatch.wa.gov.au/fuelwatch/fuelWatchRSS
  - Open Charge Map                 - EV charger locations nationally (CC-BY-SA)
                          https://api.openchargemap.io/v3/poi/

Remaining states requiring API registration:
  - QLD: fuelpricesqld.com.au - register for API tokens (Informed Sources) ✓ implemented
  - VIC: Servo Saver Public API - apply at service.vic.gov.au (24h data delay) ✓ implemented
  - SA:  safuelpricinginformation.com.au - CBS/Informed Sources publisher registration ✓ implemented
  - NT:  Contact NT Consumer Affairs - MyFuel NT API status unclear

Range anxiety: along_route() computes max gap between fuel stops and emits a
warning if any gap exceeds `no_fuel_gap_km` (default 200 km).
"""
from __future__ import annotations

import asyncio
import base64
import logging
import math
import time
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import httpx

from app.core.contracts import (
    BBox4,
    EVCharger,
    EVConnector,
    FuelOverlay,
    FuelPrice,
    FuelStation,
)
from app.core.geo_registry import states_for_bbox
from app.core.polyline6 import decode_polyline6
from app.core.settings import settings
from app.core.storage import get_fuel_pack, put_fuel_pack
from app.core.time import utc_now_iso
from app.core.geo import haversine_km, min_dist_to_route
from app.core.cache_utils import is_fresh, stable_key


# ══════════════════════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════════════════════



def _route_bbox(coords: List[Tuple[float, float]]) -> BBox4:
    lats = [c[0] for c in coords]
    lngs = [c[1] for c in coords]
    return BBox4(
        minLat=min(lats),
        maxLat=max(lats),
        minLng=min(lngs),
        maxLng=max(lngs),
    )


def _expand_bbox(bbox: BBox4, km: float) -> BBox4:
    """Expand bbox by ~km in each direction (rough degrees)."""
    deg = km / 111.0
    return BBox4(
        minLat=bbox.minLat - deg,
        maxLat=bbox.maxLat + deg,
        minLng=bbox.minLng - deg / math.cos(math.radians((bbox.minLat + bbox.maxLat) / 2)),
        maxLng=bbox.maxLng + deg / math.cos(math.radians((bbox.minLat + bbox.maxLat) / 2)),
    )




# ══════════════════════════════════════════════════════════════
# NSW + TAS FuelCheck provider (V2 API - OneGov gateway)
# ══════════════════════════════════════════════════════════════
# Migrated from api.nsw.gov.au (dead, returns 404 since ~2025) to
# api.onegov.nsw.gov.au.  Swagger spec at:
#   https://apinsw.onegov.nsw.gov.au/api/swagger/spec/22
#
# V2 endpoints cover BOTH NSW and Tasmania.
#
# Auth: OAuth2 client_credentials flow:
#   1. POST /oauth/client_credential/accesstoken?grant_type=client_credentials
#      with header Authorization: Basic base64(api_key:api_secret)
#      → returns { "access_token": "...", "expires_in": "..." }
#   2. Subsequent calls send:
#      - Authorization: Bearer {access_token}
#      - apikey: {api_key}
#      - transactionid: {uuid}
#      - requesttimestamp: {dd/MM/yyyy hh:mm:ss AM/PM} (UTC)
#      - Content-Type: application/json; charset=utf-8
#
# V2 price endpoints:
#   POST /FuelPriceCheck/v2/fuel/prices/nearby   (body: {fueltype, latitude, longitude, radius, sortby, sortascending})
#   GET  /FuelPriceCheck/v2/fuel/prices/new       (all new prices)
#   GET  /FuelPriceCheck/v2/fuel/prices            (all current prices)
# ══════════════════════════════════════════════════════════════

_nsw_log = logging.getLogger(__name__)

# Module-level token cache - simple, sufficient for a single-process server.
_nsw_token_cache: Dict[str, Any] = {"token": None, "expires_at": 0.0}


async def _nsw_get_bearer_token(client: httpx.AsyncClient, warnings: List[str]) -> Optional[str]:
    """Obtain an OAuth2 Bearer token from the OneGov gateway, with simple caching."""
    now = time.time()
    if _nsw_token_cache["token"] and now < _nsw_token_cache["expires_at"] - 30:
        return _nsw_token_cache["token"]

    base = settings.nsw_fuel_base_url.rstrip("/")
    url = f"{base}/oauth/client_credential/accesstoken"
    cred = f"{settings.nsw_fuel_api_key}:{settings.nsw_fuel_api_secret}"
    auth_header = "Basic " + base64.b64encode(cred.encode()).decode()

    try:
        resp = await client.post(
            url,
            params={"grant_type": "client_credentials"},
            headers={"Authorization": auth_header},
        )
        if resp.status_code != 200:
            warnings.append(f"nsw_fuel oauth HTTP {resp.status_code}")
            _nsw_log.warning("nsw_fuel oauth token failed: HTTP %s - %s", resp.status_code, resp.text[:200])
            return None
        data = resp.json()
        token = data.get("access_token")
        expires_in = int(data.get("expires_in", 1800))
        _nsw_token_cache["token"] = token
        _nsw_token_cache["expires_at"] = now + expires_in
        return token
    except Exception as e:
        warnings.append(f"nsw_fuel oauth error: {e}")
        return None


def _nsw_api_headers(token: str) -> Dict[str, str]:
    """Build request headers for an authenticated FuelCheck V2 call."""
    now_utc = datetime.now(timezone.utc)
    return {
        "Authorization": f"Bearer {token}",
        "apikey": settings.nsw_fuel_api_key,
        "transactionid": str(uuid.uuid4()),
        "requesttimestamp": now_utc.strftime("%d/%m/%Y %I:%M:%S %p"),
        "Content-Type": "application/json; charset=utf-8",
        "accept": "application/json",
    }


async def _fetch_nsw_fuel(
    client: httpx.AsyncClient,
    *,
    bbox: BBox4,
    warnings: List[str],
) -> Tuple[List[FuelStation], Dict[str, Any]]:
    """
    Fetch NSW FuelCheck V2 data via the OneGov gateway.

    Strategy:
    1. Obtain a Bearer token (cached).
    2. POST /FuelPriceCheck/v2/fuel/prices/nearby with centre + radius.
    3. If that fails, GET /FuelPriceCheck/v2/fuel/prices/new + bbox filter.
    """
    if not settings.nsw_fuel_enabled or not settings.nsw_fuel_api_key or not settings.nsw_fuel_api_secret:
        return [], {}

    token = await _nsw_get_bearer_token(client, warnings)
    if not token:
        return [], {}

    base = settings.nsw_fuel_base_url.rstrip("/")
    hdrs = _nsw_api_headers(token)

    centre_lat = (bbox.minLat + bbox.maxLat) / 2
    centre_lng = (bbox.minLng + bbox.maxLng) / 2
    half_diag = haversine_km((bbox.minLat, bbox.minLng), (bbox.maxLat, bbox.maxLng)) / 2
    radius_km = max(5.0, min(half_diag * 1.1, 100.0))

    stations: List[FuelStation] = []
    raw_by_id: Dict[str, Any] = {}

    # Try nearby (POST with JSON body)
    try:
        resp = await client.post(
            f"{base}/FuelPriceCheck/v2/fuel/prices/nearby",
            headers=hdrs,
            json={
                "fueltype": "All",
                "latitude": str(centre_lat),
                "longitude": str(centre_lng),
                "radius": str(int(radius_km)),
                "sortby": "Price",
                "sortascending": "true",
            },
        )
        if resp.status_code == 200:
            data = resp.json()
            stations, raw_by_id = _parse_nsw_bylocation(data, bbox=bbox)
            return stations, raw_by_id
        else:
            warnings.append(f"nsw_fuel nearby HTTP {resp.status_code}, falling back to /prices/new")
    except Exception as e:
        warnings.append(f"nsw_fuel nearby error: {e}, falling back to /prices/new")

    # Fallback: GET all new prices + filter by bbox
    try:
        hdrs2 = _nsw_api_headers(token)  # fresh transactionid
        resp = await client.get(f"{base}/FuelPriceCheck/v2/fuel/prices/new", headers=hdrs2)
        if resp.status_code != 200:
            warnings.append(f"nsw_fuel prices/new HTTP {resp.status_code}")
            return [], {}
        data = resp.json()
        # /prices/new returns {"stations": [...], "prices": [...]}
        station_list = data.get("stations", [])
        price_list = data.get("prices", [])
        stations, raw_by_id = _parse_nsw_fallback(station_list, price_list, bbox=bbox)
    except Exception as e:
        warnings.append(f"nsw_fuel prices/new error: {e}")

    return stations, raw_by_id


def _parse_nsw_bylocation(
    data: Dict[str, Any],
    *,
    bbox: BBox4,
) -> Tuple[List[FuelStation], Dict[str, Any]]:
    """Parse /bylocation response into FuelStation list."""
    stations: List[FuelStation] = []
    raw_by_id: Dict[str, Any] = {}

    # Response shape: {"stations": [...], "prices": [...]}
    # Each station: {stationcode, brandid, brand, name, address, location: {latitude, longitude}}
    # Each price:   {stationcode, fueltype, price, lastupdated}
    raw_stations = {
        s["stationcode"]: s
        for s in data.get("stations", [])
        if "stationcode" in s
    }
    price_map: Dict[str, List[FuelPrice]] = {}
    for p in data.get("prices", []):
        code = str(p.get("stationcode", ""))
        if not code:
            continue
        fp = FuelPrice(
            fuel_type=str(p.get("fueltype", "")),
            price_cents=float(p.get("price", 0)),
            last_updated=str(p.get("lastupdated", "")) or None,
        )
        price_map.setdefault(code, []).append(fp)

    for code, s in raw_stations.items():
        loc = s.get("location") or {}
        lat = float(loc.get("latitude", 0))
        lng = float(loc.get("longitude", 0))
        if lat == 0 and lng == 0:
            continue
        # Filter to bbox
        if not (bbox.minLat <= lat <= bbox.maxLat and bbox.minLng <= lng <= bbox.maxLng):
            continue

        station = FuelStation(
            id=f"nsw_fc_{code}",
            source="nsw_fuelcheck",
            name=str(s.get("name", "")),
            brand=str(s.get("brand", "")) or None,
            lat=lat,
            lng=lng,
            address=str(s.get("address", "")) or None,
            fuel_types=price_map.get(code, []),
        )
        stations.append(station)
        raw_by_id[f"nsw_fc_{code}"] = s

    return stations, raw_by_id


def _parse_nsw_fallback(
    station_list: List[Dict[str, Any]],
    price_list: List[Dict[str, Any]],
    *,
    bbox: BBox4,
) -> Tuple[List[FuelStation], Dict[str, Any]]:
    """Parse /stations + /prices/new fallback response."""
    raw_stations = {
        str(s.get("code", s.get("stationcode", ""))): s
        for s in station_list
        if s.get("code") or s.get("stationcode")
    }
    price_map: Dict[str, List[FuelPrice]] = {}
    for p in price_list:
        code = str(p.get("stationcode", p.get("code", "")))
        if not code:
            continue
        fp = FuelPrice(
            fuel_type=str(p.get("fueltype", p.get("FuelType", ""))),
            price_cents=float(p.get("price", p.get("Price", 0))),
            last_updated=str(p.get("lastupdated", p.get("LastUpdated", ""))) or None,
        )
        price_map.setdefault(code, []).append(fp)

    stations: List[FuelStation] = []
    raw_by_id: Dict[str, Any] = {}
    for code, s in raw_stations.items():
        lat_raw = s.get("latitude", s.get("Latitude"))
        lng_raw = s.get("longitude", s.get("Longitude"))
        if lat_raw is None or lng_raw is None:
            continue
        lat = float(lat_raw)
        lng = float(lng_raw)
        if not (bbox.minLat <= lat <= bbox.maxLat and bbox.minLng <= lng <= bbox.maxLng):
            continue

        station = FuelStation(
            id=f"nsw_fc_{code}",
            source="nsw_fuelcheck",
            name=str(s.get("name", s.get("Name", ""))),
            brand=str(s.get("brand", s.get("Brand", ""))) or None,
            lat=lat,
            lng=lng,
            address=str(s.get("address", s.get("Address", ""))) or None,
            fuel_types=price_map.get(code, []),
        )
        stations.append(station)
        raw_by_id[f"nsw_fc_{code}"] = s

    return stations, raw_by_id


# ══════════════════════════════════════════════════════════════
# Open Charge Map EV chargers
# ══════════════════════════════════════════════════════════════
# Endpoint: GET https://api.openchargemap.io/v3/poi/
# Params: latitude, longitude, distance (km), distanceunit=KM,
#         maxresults, countrycode=AU, output=json, key=<API_KEY>
# Returns: JSON array of POIs
# ══════════════════════════════════════════════════════════════

_OCM_URL = "https://api.openchargemap.io/v3/poi/"

# Maps OCM status type IDs to operational bool
_OCM_OPERATIONAL_STATUS = {1, 2, 50}  # Available, Unknown, Operational


async def _fetch_ev_chargers(
    client: httpx.AsyncClient,
    *,
    bbox: BBox4,
    warnings: List[str],
    max_results: int = 200,
) -> List[EVCharger]:
    """Fetch EV chargers from Open Charge Map for a bbox."""
    if not settings.openchargemap_enabled:
        return []

    centre_lat = (bbox.minLat + bbox.maxLat) / 2
    centre_lng = (bbox.minLng + bbox.maxLng) / 2
    half_diag = haversine_km((bbox.minLat, bbox.minLng), (bbox.maxLat, bbox.maxLng)) / 2
    radius_km = max(5.0, min(half_diag * 1.1, 200.0))

    params: Dict[str, Any] = {
        "latitude": centre_lat,
        "longitude": centre_lng,
        "distance": radius_km,
        "distanceunit": "KM",
        "maxresults": max_results,
        "countrycode": "AU",
        "output": "json",
    }
    if settings.openchargemap_api_key:
        params["key"] = settings.openchargemap_api_key

    try:
        resp = await client.get(_OCM_URL, params=params)
        if resp.status_code != 200:
            warnings.append(f"openchargemap HTTP {resp.status_code}")
            return []
        data = resp.json()
        return _parse_ocm_pois(data, bbox=bbox)
    except Exception as e:
        warnings.append(f"openchargemap error: {e}")
        return []


def _parse_ocm_pois(data: List[Dict[str, Any]], *, bbox: BBox4) -> List[EVCharger]:
    chargers: List[EVCharger] = []
    for poi in data:
        addr_info = poi.get("AddressInfo") or {}
        lat = addr_info.get("Latitude")
        lng = addr_info.get("Longitude")
        if lat is None or lng is None:
            continue
        lat = float(lat)
        lng = float(lng)
        # Filter to bbox
        if not (bbox.minLat <= lat <= bbox.maxLat and bbox.minLng <= lng <= bbox.maxLng):
            continue

        poi_id = str(poi.get("ID", ""))
        name = str(addr_info.get("Title", "") or "EV Charger")
        address = addr_info.get("AddressLine1") or addr_info.get("Town") or None
        if address:
            town = addr_info.get("Town", "")
            if town and town not in address:
                address = f"{address}, {town}"

        operator_info = poi.get("OperatorInfo") or {}
        operator = operator_info.get("Title") or None

        # Status
        status_type = poi.get("StatusType") or {}
        status_id = status_type.get("ID")
        is_operational = status_id in _OCM_OPERATIONAL_STATUS if status_id else None

        usage_cost = poi.get("UsageCost") or None

        # Connectors
        connectors: List[EVConnector] = []
        for conn_entry in poi.get("Connections") or []:
            ctype_info = conn_entry.get("ConnectionType") or {}
            ctype = ctype_info.get("Title") or ctype_info.get("FormalName") or "Unknown"
            power_kw_raw = conn_entry.get("PowerKW")
            quantity = int(conn_entry.get("Quantity") or 1)
            connectors.append(EVConnector(
                type=str(ctype),
                power_kw=float(power_kw_raw) if power_kw_raw is not None else None,
                quantity=max(1, quantity),
            ))

        chargers.append(EVCharger(
            id=f"ocm_{poi_id}",
            source="openchargemap",
            name=name,
            operator=operator,
            lat=lat,
            lng=lng,
            address=address,
            connectors=connectors,
            is_operational=is_operational,
            usage_cost=usage_cost,
        ))
    return chargers


# ══════════════════════════════════════════════════════════════
# WA FuelWatch RSS provider
# ══════════════════════════════════════════════════════════════
# Official WA Government feed - https://www.fuelwatch.wa.gov.au/fuelwatch/fuelWatchRSS
# Free, no auth. Product IDs: 1=ULP, 2=PULP, 4=Diesel, 5=LPG, 6=98RON, 11=E85
# Each product has its own feed; we query them concurrently and deduplicate
# stations by (lat, lng), merging fuel types across feeds.

_WA_FUELWATCH_PRODUCTS = {
    1: "ULP",
    2: "PULP",
    4: "Diesel",
    5: "LPG",
    6: "98RON",
    11: "E85",
}

# Mapping from FuelWatch product names to standard display names
_WA_FUEL_TYPE_NAMES = {
    "ULP": "Unleaded",
    "PULP": "PULP 95",
    "Diesel": "Diesel",
    "LPG": "LPG",
    "98RON": "PULP 98",
    "E85": "E85",
}


async def _fetch_wa_fuelwatch_product(
    client: httpx.AsyncClient,
    *,
    product_id: int,
    product_name: str,
    warnings: List[str],
) -> List[Dict[str, Any]]:
    """Fetch one WA FuelWatch RSS product feed. Returns list of raw item dicts."""
    url = settings.wa_fuelwatch_rss_url
    try:
        resp = await client.get(
            url,
            params={"Product": str(product_id), "Suburb": "", "StateRegion": "", "Day": "today"},
        )
        if resp.status_code != 200:
            warnings.append(f"wa_fuelwatch product={product_id} HTTP {resp.status_code}")
            return []
        root = ET.fromstring(resp.content)
        items = []
        for item in root.iter("item"):
            def _tag(name: str) -> str:
                el = item.find(name)
                return el.text.strip() if el is not None and el.text else ""

            lat_str = _tag("latitude")
            lng_str = _tag("longitude")
            if not lat_str or not lng_str:
                continue
            try:
                lat = float(lat_str)
                lng = float(lng_str)
            except ValueError:
                continue

            # Price is in the <title> element as cents (e.g. "210.9")
            price_str = _tag("title")
            try:
                price_cents = float(price_str)
            except ValueError:
                price_cents = 0.0

            open247_raw = _tag("open-247")
            is_open_247 = open247_raw.upper() in ("YES", "TRUE", "1")

            items.append({
                "product_id": product_id,
                "product_name": product_name,
                "price_cents": price_cents,
                "brand": _tag("brand"),
                "trading_name": _tag("trading-name"),
                "address": _tag("address"),
                "location": _tag("location"),
                "lat": lat,
                "lng": lng,
                "phone": _tag("phone"),
                "site_features": _tag("site-features"),
                "open_247": is_open_247,
            })
        return items
    except ET.ParseError as e:
        warnings.append(f"wa_fuelwatch product={product_id} XML parse error: {e}")
        return []
    except Exception as e:
        warnings.append(f"wa_fuelwatch product={product_id} error: {e}")
        return []


async def _fetch_wa_fuelwatch(
    client: httpx.AsyncClient,
    *,
    bbox: BBox4,
    warnings: List[str],
) -> List[FuelStation]:
    """
    Fetch WA FuelWatch RSS for all product IDs concurrently.

    Deduplicates stations by (lat, lng) - merges fuel types from different
    product feeds into one FuelStation with multiple FuelPrice entries.
    """
    if not settings.wa_fuel_enabled:
        return []

    # Fetch all product feeds concurrently
    tasks = [
        _fetch_wa_fuelwatch_product(
            client,
            product_id=pid,
            product_name=pname,
            warnings=warnings,
        )
        for pid, pname in _WA_FUELWATCH_PRODUCTS.items()
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    # Collect all raw items, handling exceptions
    all_items: List[Dict[str, Any]] = []
    for i, result in enumerate(results):
        if isinstance(result, Exception):
            pid = list(_WA_FUELWATCH_PRODUCTS.keys())[i]
            warnings.append(f"wa_fuelwatch product={pid} task error: {result}")
        elif isinstance(result, list):
            all_items.extend(result)

    # Deduplicate by (lat, lng) - build station map, merge fuel prices
    # Key: rounded (lat, lng) to handle minor float variation in same station
    station_map: Dict[Tuple[float, float], Dict[str, Any]] = {}
    for item in all_items:
        lat = item["lat"]
        lng = item["lng"]

        # Filter to bbox
        if not (bbox.minLat <= lat <= bbox.maxLat and bbox.minLng <= lng <= bbox.maxLng):
            continue

        key = (round(lat, 5), round(lng, 5))
        if key not in station_map:
            station_map[key] = {
                "lat": lat,
                "lng": lng,
                "brand": item["brand"],
                "trading_name": item["trading_name"],
                "address": item["address"],
                "location": item["location"],
                "phone": item["phone"],
                "site_features": item["site_features"],
                "open_247": item["open_247"],
                "prices": [],
            }
        display_name = _WA_FUEL_TYPE_NAMES.get(item["product_name"], item["product_name"])
        if item["price_cents"] > 0:
            station_map[key]["prices"].append(FuelPrice(
                fuel_type=display_name,
                price_cents=item["price_cents"],
            ))

    stations: List[FuelStation] = []
    for i, ((lat, lng), data) in enumerate(station_map.items()):
        name = data["trading_name"] or data["brand"] or "WA Fuel Station"
        address_parts = [data["address"]]
        if data["location"]:
            address_parts.append(data["location"])
        full_address = ", ".join(p for p in address_parts if p) or None

        station = FuelStation(
            id=f"wa_fw_{round(lat, 5)}_{round(lng, 5)}".replace(".", "_").replace("-", "m"),
            source="wa_fuelwatch",
            name=name,
            brand=data["brand"] or None,
            lat=lat,
            lng=lng,
            address=full_address,
            fuel_types=data["prices"],
            is_open=data["open_247"] if data["open_247"] else None,
            open_hours="24/7" if data["open_247"] else None,
        )
        stations.append(station)

    return stations


# ══════════════════════════════════════════════════════════════
# PetrolSpy national fuel station provider (primary)
# ══════════════════════════════════════════════════════════════
# Endpoint: GET https://petrolspy.com.au/webservice-1/station/box
# Params: neLat, neLng, swLat, swLng, ts (unix ms timestamp)
# No auth required.
# Response: {message: {list: [{id, name, brand, address, suburb, postCode,
#   location: {x (lng), y (lat)}, open24, restrooms, phone,
#   prices: {DIESEL: {amount, updated, relevant}, U91: {...}, ...}}]}}

_PETROLSPY_URL = "https://petrolspy.com.au/webservice-1/station/box"

# Maps PetrolSpy fuel keys to canonical fuel_type strings
_PETROLSPY_FUEL_MAP: Dict[str, str] = {
    "DIESEL":    "diesel",
    "U91":       "unleaded",
    "U95":       "premium_unleaded_95",
    "U98":       "premium_unleaded_98",
    "E10":       "e10",
    "LPG":       "lpg",
    "AdBlue":    "adblue",
    "TruckDSL":  "truck_diesel",
    "PremDSL":   "premium_diesel",
    "E85":       "e85",
    "BIODIESEL": "biodiesel",
}


async def _fetch_petrolspy(
    client: httpx.AsyncClient,
    *,
    bbox: BBox4,
    warnings: List[str],
) -> List[FuelStation]:
    """Fetch fuel stations from PetrolSpy for a bounding box."""
    if not settings.petrolspy_enabled:
        return []

    try:
        resp = await client.get(
            _PETROLSPY_URL,
            params={
                "neLat": bbox.maxLat,
                "neLng": bbox.maxLng,
                "swLat": bbox.minLat,
                "swLng": bbox.minLng,
                "ts": int(time.time() * 1000),
            },
        )
        if resp.status_code != 200:
            warnings.append(f"petrolspy HTTP {resp.status_code}")
            return []

        data = resp.json()
        raw_list = (data.get("message") or {}).get("list") or []
        stations: List[FuelStation] = []

        for item in raw_list:
            loc = item.get("location") or {}
            lat = loc.get("y")
            lng = loc.get("x")
            if lat is None or lng is None:
                continue
            try:
                lat = float(lat)
                lng = float(lng)
            except (TypeError, ValueError):
                continue

            # Map prices - only include relevant=true entries
            fuel_prices: List[FuelPrice] = []
            for ps_key, canonical in _PETROLSPY_FUEL_MAP.items():
                price_data = (item.get("prices") or {}).get(ps_key)
                if not price_data:
                    continue
                if not price_data.get("relevant"):
                    continue
                amount = price_data.get("amount")
                if amount is None:
                    continue
                try:
                    fuel_prices.append(FuelPrice(
                        fuel_type=canonical,
                        price_cents=float(amount),
                        last_updated=price_data.get("updated") or None,
                    ))
                except (TypeError, ValueError):
                    pass

            open24 = bool(item.get("open24"))
            station = FuelStation(
                id=f"ps_{item.get('id', '')}",
                source="petrolspy",
                name=str(item.get("name") or ""),
                brand=str(item.get("brand") or "") or None,
                lat=lat,
                lng=lng,
                address=str(item.get("address") or "") or None,
                fuel_types=fuel_prices,
                is_open=open24,
                open_hours="24 hours" if open24 else None,
                extra={
                    "restrooms": item.get("restrooms"),
                    "phone": item.get("phone"),
                    "suburb": item.get("suburb"),
                },
            )
            stations.append(station)

        return stations

    except Exception as e:
        warnings.append(f"petrolspy error: {e}")
        return []


# ══════════════════════════════════════════════════════════════
# QLD Fuel Price Reporting (fuelpricesqld.com.au)
# ══════════════════════════════════════════════════════════════
# Operated by Informed Sources on behalf of QLD Treasury.
# Swagger: https://fppdirectapi-prod.fuelpricesqld.com.au/swagger/docs/v1
# Auth: Header "Authorization: FPDAPI SubscriberToken={token}"
# No bbox query - fetch all QLD, filter client-side.
# ══════════════════════════════════════════════════════════════

_QLD_BASE = "https://fppdirectapi-prod.fuelpricesqld.com.au"
_QLD_SITES = f"{_QLD_BASE}/Subscriber/GetFullSiteDetails"
_QLD_PRICES = f"{_QLD_BASE}/Price/GetSitesPrices"

# FuelId → canonical fuel_type
_QLD_FUEL_MAP: Dict[int, str] = {
    2:  "unleaded",
    5:  "premium_unleaded_95",
    8:  "premium_unleaded_98",
    12: "e10",
    19: "e85",
    3:  "diesel",
    14: "premium_diesel",
    4:  "lpg",
}


async def _fetch_qld_fuel(
    client: httpx.AsyncClient,
    *,
    bbox: BBox4,
    warnings: List[str],
) -> List[FuelStation]:
    """Fetch QLD fuel stations + prices from fuelpricesqld.com.au."""
    if not settings.qld_fuel_enabled or not settings.qld_fuel_api_token:
        return []

    hdrs = {
        "Authorization": f"FPDAPI SubscriberToken={settings.qld_fuel_api_token}",
        "Content-Type": "application/json",
    }
    params = {"countryId": 21, "geoRegionLevel": 3, "geoRegionId": 1}

    try:
        # Fetch sites and prices concurrently
        sites_resp, prices_resp = await asyncio.gather(
            client.get(_QLD_SITES, headers=hdrs, params=params),
            client.get(_QLD_PRICES, headers=hdrs, params=params),
            return_exceptions=False,
        )

        if sites_resp.status_code != 200:
            warnings.append(f"qld_fuel sites HTTP {sites_resp.status_code}")
            return []
        if prices_resp.status_code != 200:
            warnings.append(f"qld_fuel prices HTTP {prices_resp.status_code}")
            return []

        sites_data = sites_resp.json()
        prices_data = prices_resp.json()

        # Build site lookup: SiteId → site dict
        raw_sites: Dict[int, Dict[str, Any]] = {}
        for s in sites_data.get("S", []):
            sid = s.get("S")
            if sid is not None:
                raw_sites[int(sid)] = s

        # Build price lookup: SiteId → [FuelPrice]
        price_map: Dict[int, List[FuelPrice]] = {}
        for p in prices_data.get("SitePrices", []):
            sid = p.get("SiteId")
            fid = p.get("FuelId")
            price_val = p.get("Price")
            if sid is None or fid is None or price_val is None:
                continue
            canonical = _QLD_FUEL_MAP.get(int(fid))
            if not canonical:
                continue
            cents = float(price_val)
            # API sometimes returns tenths of a cent - normalize to cpl
            if cents > 500:
                cents = cents / 10.0
            price_map.setdefault(int(sid), []).append(FuelPrice(
                fuel_type=canonical,
                price_cents=cents,
                last_updated=p.get("TransactionDateUtc"),
            ))

        # Build FuelStation list, filtered to bbox
        stations: List[FuelStation] = []
        for sid, s in raw_sites.items():
            lat = s.get("Lat")
            lng = s.get("Lng")
            if lat is None or lng is None:
                continue
            lat, lng = float(lat), float(lng)
            if not (bbox.minLat <= lat <= bbox.maxLat and bbox.minLng <= lng <= bbox.maxLng):
                continue

            stations.append(FuelStation(
                id=f"qld_{sid}",
                source="qld_fuel",
                name=str(s.get("N", "")),
                brand=None,  # Brand is an ID; would need brand lookup
                lat=lat,
                lng=lng,
                address=str(s.get("A", "")) or None,
                fuel_types=price_map.get(sid, []),
            ))

        return stations

    except Exception as e:
        warnings.append(f"qld_fuel error: {e}")
        return []


# ══════════════════════════════════════════════════════════════
# VIC Fair Fuel Open Data API (Servo Saver)
# ══════════════════════════════════════════════════════════════
# Official VIC Government API - all retailers legally required to report.
# Base URL: https://api.fuel.service.vic.gov.au/open-data/v1
# Auth: x-consumer-id header (issued by Service Victoria)
# Data has ~24-hour delay after retailer submission.
# Single endpoint returns all stations + prices in one call.
# ══════════════════════════════════════════════════════════════

_VIC_PRICES_URL = "https://api.fuel.service.vic.gov.au/open-data/v1/fuel/prices"

# VIC fuel type codes → canonical
_VIC_FUEL_MAP: Dict[str, str] = {
    "U91":  "unleaded",
    "P95":  "premium_unleaded_95",
    "P98":  "premium_unleaded_98",
    "E10":  "e10",
    "E85":  "e85",
    "DL":   "diesel",
    "PDL":  "premium_diesel",
    "LPG":  "lpg",
    "B20":  "biodiesel",
    "EV":   "ev",
    "AdBlue": "adblue",
}


async def _fetch_vic_fuel(
    client: httpx.AsyncClient,
    *,
    bbox: BBox4,
    warnings: List[str],
) -> List[FuelStation]:
    """Fetch VIC fuel stations + prices from the Fair Fuel Open Data API."""
    if not settings.vic_fuel_enabled or not settings.vic_fuel_consumer_id:
        return []

    hdrs = {
        "User-Agent": "Roam/1.0",
        "x-consumer-id": settings.vic_fuel_consumer_id,
        "x-transactionid": str(uuid.uuid4()),
    }

    try:
        resp = await client.get(_VIC_PRICES_URL, headers=hdrs)
        if resp.status_code != 200:
            warnings.append(f"vic_fuel HTTP {resp.status_code}")
            return []

        data = resp.json()
        stations: List[FuelStation] = []

        for item in data.get("fuelPriceDetails", []):
            fs = item.get("fuelStation") or {}
            loc = fs.get("location") or {}
            lat = loc.get("latitude")
            lng = loc.get("longitude")
            if lat is None or lng is None:
                continue
            lat, lng = float(lat), float(lng)
            if not (bbox.minLat <= lat <= bbox.maxLat and bbox.minLng <= lng <= bbox.maxLng):
                continue

            # Parse fuel prices
            fuel_prices: List[FuelPrice] = []
            for fp in item.get("fuelPrices", []):
                ft_code = fp.get("fuelType", "")
                canonical = _VIC_FUEL_MAP.get(ft_code, ft_code.lower())
                price_val = fp.get("price")
                if price_val is None:
                    continue
                if fp.get("isAvailable") is False:
                    continue
                fuel_prices.append(FuelPrice(
                    fuel_type=canonical,
                    price_cents=float(price_val),
                    last_updated=fp.get("updatedAt"),
                ))

            station_id = fs.get("id", "")
            stations.append(FuelStation(
                id=f"vic_{station_id}",
                source="vic_fuel",
                name=str(fs.get("name", "")),
                brand=str(fs.get("brandId", "")) or None,
                lat=lat,
                lng=lng,
                address=str(fs.get("address", "")) or None,
                fuel_types=fuel_prices,
                extra={"phone": fs.get("contactPhone")} if fs.get("contactPhone") else {},
            ))

        return stations

    except Exception as e:
        warnings.append(f"vic_fuel error: {e}")
        return []


# ══════════════════════════════════════════════════════════════
# SA Fuel Pricing (safuelpricinginformation.com.au)
# ══════════════════════════════════════════════════════════════
# Operated by CBS (Consumer and Business Services) SA via Informed Sources.
# Same API contract as QLD fuelpricesqld.com.au but different base URL.
# Auth: Header "Authorization: FPDAPI SubscriberToken={token}"
# Register at: https://www.safuelpricinginformation.com.au/
# No bbox query - fetch all SA, filter client-side.
# ══════════════════════════════════════════════════════════════

_SA_BASE = "https://fppdirectapi-prod.safuelpricinginformation.com.au"
_SA_SITES = f"{_SA_BASE}/Subscriber/GetFullSiteDetails"
_SA_PRICES = f"{_SA_BASE}/Price/GetSitesPrices"

# FuelId → canonical fuel_type (same scheme as QLD)
_SA_FUEL_MAP: Dict[int, str] = {
    2:  "unleaded",
    5:  "premium_unleaded_95",
    8:  "premium_unleaded_98",
    12: "e10",
    19: "e85",
    3:  "diesel",
    14: "premium_diesel",
    4:  "lpg",
    21: "adblue",
}


async def _fetch_sa_fuel(
    client: httpx.AsyncClient,
    *,
    bbox: BBox4,
    warnings: List[str],
) -> List[FuelStation]:
    """Fetch SA fuel stations + prices from safuelpricinginformation.com.au."""
    if not settings.sa_fuel_enabled or not settings.sa_fuel_api_token:
        return []

    hdrs = {
        "Authorization": f"FPDAPI SubscriberToken={settings.sa_fuel_api_token}",
        "Content-Type": "application/json",
    }
    # countryId=21 (Australia), geoRegionLevel=3 geoRegionId=4 = SA
    params = {"countryId": 21, "geoRegionLevel": 3, "geoRegionId": 4}

    try:
        sites_resp, prices_resp = await asyncio.gather(
            client.get(_SA_SITES, headers=hdrs, params=params),
            client.get(_SA_PRICES, headers=hdrs, params=params),
            return_exceptions=False,
        )

        if sites_resp.status_code != 200:
            warnings.append(f"sa_fuel sites HTTP {sites_resp.status_code}")
            return []
        if prices_resp.status_code != 200:
            warnings.append(f"sa_fuel prices HTTP {prices_resp.status_code}")
            return []

        sites_data = sites_resp.json()
        prices_data = prices_resp.json()

        # Build site lookup: SiteId → site dict
        raw_sites: Dict[int, Dict[str, Any]] = {}
        for s in sites_data.get("S", []):
            sid = s.get("S")
            if sid is not None:
                raw_sites[int(sid)] = s

        # Build price lookup: SiteId → [FuelPrice]
        price_map: Dict[int, List[FuelPrice]] = {}
        for p in prices_data.get("SitePrices", []):
            sid = p.get("SiteId")
            fid = p.get("FuelId")
            price_val = p.get("Price")
            if sid is None or fid is None or price_val is None:
                continue
            canonical = _SA_FUEL_MAP.get(int(fid))
            if not canonical:
                continue
            cents = float(price_val)
            if cents > 500:
                cents = cents / 10.0
            price_map.setdefault(int(sid), []).append(FuelPrice(
                fuel_type=canonical,
                price_cents=cents,
                last_updated=p.get("TransactionDateUtc"),
            ))

        # Build FuelStation list, filtered to bbox
        stations: List[FuelStation] = []
        for sid, s in raw_sites.items():
            lat = s.get("Lat")
            lng = s.get("Lng")
            if lat is None or lng is None:
                continue
            lat, lng = float(lat), float(lng)
            if not (bbox.minLat <= lat <= bbox.maxLat and bbox.minLng <= lng <= bbox.maxLng):
                continue

            stations.append(FuelStation(
                id=f"sa_{sid}",
                source="sa_fuel",
                name=str(s.get("N", "")),
                brand=None,
                lat=lat,
                lng=lng,
                address=str(s.get("A", "")) or None,
                fuel_types=price_map.get(sid, []),
            ))

        return stations

    except Exception as e:
        warnings.append(f"sa_fuel error: {e}")
        return []


# ══════════════════════════════════════════════════════════════
# Range anxiety: gap detection
# ══════════════════════════════════════════════════════════════

def _check_fuel_gaps(
    stations: List[FuelStation],
    coords: List[Tuple[float, float]],
    *,
    gap_threshold_km: float = 200.0,
    warnings: List[str],
) -> None:
    """
    Walk the decoded route and detect segments with no fuel station within
    `gap_threshold_km / 2` km of the route. Emits warnings for each such gap.
    """
    if not coords or not stations:
        return

    # Build a sorted list of (km_along, station_name) for stations snapped to route
    # Accumulate cumulative distance along the route
    cum_km = [0.0]
    for i in range(1, len(coords)):
        d = haversine_km((coords[i - 1][0], coords[i - 1][1]), (coords[i][0], coords[i][1]))
        cum_km.append(cum_km[-1] + d)
    total_km = cum_km[-1]

    search_radius = gap_threshold_km / 2

    # For each station find closest route point and record km_along
    station_km: List[float] = []
    for s in stations:
        best_km = None
        best_d = math.inf
        for i, (rlat, rlng) in enumerate(coords):
            d = haversine_km((s.lat, s.lng), (rlat, rlng))
            if d < best_d:
                best_d = d
                best_km = cum_km[i]
        if best_km is not None and best_d <= search_radius:
            station_km.append(best_km)

    if not station_km:
        warnings.append(
            f"No fuel stations found within {search_radius:.0f} km of the route "
            f"({total_km:.0f} km total)."
        )
        return

    station_km_sorted = sorted(station_km)

    # Check gap from route start to first station
    if station_km_sorted[0] > gap_threshold_km:
        warnings.append(
            f"No fuel station for {station_km_sorted[0]:.0f} km from route start "
            f"to first station at km {station_km_sorted[0]:.0f}."
        )

    # Check gaps between consecutive stations
    for i in range(1, len(station_km_sorted)):
        gap = station_km_sorted[i] - station_km_sorted[i - 1]
        if gap > gap_threshold_km:
            warnings.append(
                f"No fuel station for {gap:.0f} km between km {station_km_sorted[i - 1]:.0f} "
                f"and km {station_km_sorted[i]:.0f}."
            )

    # Check gap from last station to end
    last_gap = total_km - station_km_sorted[-1]
    if last_gap > gap_threshold_km:
        warnings.append(
            f"No fuel station for {last_gap:.0f} km from km {station_km_sorted[-1]:.0f} "
            f"to route end ({total_km:.0f} km)."
        )


# ══════════════════════════════════════════════════════════════
# Fuel service
# ══════════════════════════════════════════════════════════════

class Fuel:
    """
    Fuel intelligence overlay service.

    Instantiated per-request (same pattern as Traffic / Hazards).
    Provide a SQLite cache connection via `conn`.
    """

    def __init__(self, *, conn) -> None:
        self.conn = conn

    async def poll(
        self,
        *,
        bbox: BBox4,
        fuel_types: Optional[List[str]] = None,
        cache_seconds: Optional[int] = None,
        timeout_s: Optional[float] = None,
    ) -> FuelOverlay:
        """
        Return fuel stations + EV chargers in a bounding box.

        Results are cached in SQLite for `fuel_cache_seconds` (default 30 min).
        """
        algo_version = settings.fuel_algo_version
        max_age = int(cache_seconds or settings.fuel_cache_seconds)
        timeout = float(timeout_s or 20.0)

        active_states = states_for_bbox(bbox)

        fuel_key = stable_key(
            "fuel",
            {
                "bbox": bbox.model_dump(),
                "algo_version": algo_version,
                "states": sorted(active_states),
                "fuel_types": sorted(fuel_types or []),
            },
        )

        # Cache hit
        cached = get_fuel_pack(self.conn, fuel_key)
        if cached:
            try:
                pack = FuelOverlay.model_validate(cached)
                if is_fresh(pack.created_at, max_age_s=max_age):
                    return pack
            except Exception:
                pass

        warnings: List[str] = []
        stations: List[FuelStation] = []
        ev_chargers: List[EVCharger] = []

        transport = httpx.AsyncHTTPTransport(retries=1)
        async with httpx.AsyncClient(
            timeout=timeout, follow_redirects=True, transport=transport
        ) as client:
            # Build task list from government sources based on bbox coverage.
            # NSW FuelCheck and WA FuelWatch are the primary per-station price
            # sources (government-mandated, legally clean). EV chargers from
            # Open Charge Map run concurrently.
            tasks: List[Any] = []
            task_labels: List[str] = []

            # NSW FuelCheck V2 - covers NSW + ACT + TAS
            if "nsw" in active_states or "act" in active_states or "tas" in active_states:
                tasks.append(_fetch_nsw_fuel(client, bbox=bbox, warnings=warnings))
                task_labels.append("nsw_fuel")

            # WA FuelWatch - covers WA
            if "wa" in active_states:
                tasks.append(_fetch_wa_fuelwatch(client, bbox=bbox, warnings=warnings))
                task_labels.append("wa_fuel")

            # QLD Fuel Price Reporting - covers QLD (requires registration)
            if "qld" in active_states and settings.qld_fuel_enabled and settings.qld_fuel_api_token:
                tasks.append(_fetch_qld_fuel(client, bbox=bbox, warnings=warnings))
                task_labels.append("qld_fuel")

            # VIC Servo Saver - covers VIC (requires API Consumer ID)
            if "vic" in active_states and settings.vic_fuel_enabled and settings.vic_fuel_consumer_id:
                tasks.append(_fetch_vic_fuel(client, bbox=bbox, warnings=warnings))
                task_labels.append("vic_fuel")

            # SA Fuel Pricing - covers SA (requires publisher registration)
            # Register at: https://www.safuelpricinginformation.com.au/
            if "sa" in active_states and settings.sa_fuel_enabled and settings.sa_fuel_api_token:
                tasks.append(_fetch_sa_fuel(client, bbox=bbox, warnings=warnings))
                task_labels.append("sa_fuel")

            # EV chargers - national
            tasks.append(_fetch_ev_chargers(client, bbox=bbox, warnings=warnings))
            task_labels.append("ev_chargers")

            results = await asyncio.gather(*tasks, return_exceptions=True)

            for label, result in zip(task_labels, results):
                if isinstance(result, Exception):
                    warnings.append(f"{label} task error: {result}")
                elif label == "ev_chargers" and isinstance(result, list):
                    ev_chargers = result
                elif isinstance(result, list):
                    stations.extend(result)
                elif isinstance(result, tuple):
                    # NSW returns (stations, _extra)
                    nsw_stations, _ = result
                    stations.extend(nsw_stations)

            # NT: MyFuel NT - API availability unclear, contact NT Consumer Affairs

        # Filter by requested fuel types
        if fuel_types:
            ft_set = {f.lower() for f in fuel_types}
            for s in stations:
                s.fuel_types = [
                    fp for fp in s.fuel_types
                    if fp.fuel_type.lower() in ft_set
                ]

        pack = FuelOverlay(
            fuel_key=fuel_key,
            bbox=bbox,
            algo_version=algo_version,
            created_at=utc_now_iso(),
            stations=stations,
            ev_chargers=ev_chargers,
            warnings=warnings,
        )

        put_fuel_pack(
            self.conn,
            fuel_key=fuel_key,
            created_at=pack.created_at,
            algo_version=algo_version,
            pack=pack.model_dump(),
        )
        return pack

    async def along_route(
        self,
        *,
        polyline6: str,
        buffer_km: float = 15.0,
        fuel_types: Optional[List[str]] = None,
        no_fuel_gap_km: float = 200.0,
        cache_seconds: Optional[int] = None,
        timeout_s: Optional[float] = None,
    ) -> FuelOverlay:
        """
        Return fuel stations + EV chargers along a route corridor.

        Decodes the polyline6, expands its bbox by `buffer_km`, then calls
        poll() and filters results to stations within `buffer_km` of the route.

        Also runs range-anxiety gap detection: if any stretch of the route has
        no fuel station within `buffer_km` for more than `no_fuel_gap_km` km,
        a warning is added to the overlay.
        """
        coords = decode_polyline6(polyline6)
        if not coords:
            return FuelOverlay(
                fuel_key=stable_key("fuel_route", {"polyline6": polyline6[:32]}),
                algo_version=settings.fuel_algo_version,
                created_at=utc_now_iso(),
                warnings=["Empty or invalid polyline6."],
            )

        raw_bbox = _route_bbox(coords)
        expanded_bbox = _expand_bbox(raw_bbox, buffer_km)

        overlay = await self.poll(
            bbox=expanded_bbox,
            fuel_types=fuel_types,
            cache_seconds=cache_seconds,
            timeout_s=timeout_s,
        )

        # Filter stations to those within buffer_km of the actual route line
        filtered_stations: List[FuelStation] = []
        for s in overlay.stations:
            d = min_dist_to_route(s.lat, s.lng, coords)
            if d <= buffer_km:
                s2 = s.model_copy(update={"distance_km": round(d, 2)})
                filtered_stations.append(s2)

        # Filter EV chargers similarly
        filtered_ev: List[EVCharger] = []
        for c in overlay.ev_chargers:
            d = min_dist_to_route(c.lat, c.lng, coords)
            if d <= buffer_km:
                c2 = c.model_copy(update={"distance_km": round(d, 2)})
                filtered_ev.append(c2)

        # Range anxiety warnings
        gap_warnings: List[str] = []
        _check_fuel_gaps(
            filtered_stations,
            coords,
            gap_threshold_km=no_fuel_gap_km,
            warnings=gap_warnings,
        )
        all_warnings = list(overlay.warnings) + gap_warnings

        return FuelOverlay(
            fuel_key=overlay.fuel_key,
            bbox=raw_bbox,
            algo_version=overlay.algo_version,
            created_at=overlay.created_at,
            stations=filtered_stations,
            ev_chargers=filtered_ev,
            city_averages=overlay.city_averages,
            warnings=all_warnings,
        )


async def _noop_stations() -> Tuple[List[FuelStation], Dict]:
    return [], {}


async def _noop_list() -> List[FuelStation]:
    return []
