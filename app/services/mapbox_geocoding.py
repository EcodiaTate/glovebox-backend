"""
Mapbox Geocoding v5 service for Roam place search / autocomplete.

Docs: https://docs.mapbox.com/api/search/geocoding/

This is used for user-facing "search a place by name" queries (the PlaceSearchModal).
Corridor POI data still uses the Overpass → Supabase pipeline - this is a separate concern.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from datetime import datetime, timezone
from typing import Any

import httpx

from app.core.contracts import NavCoord, PlaceItem, PlacesPack, PlacesRequest
from app.core.settings import settings
from app.core.storage import get_geocode_cache, put_geocode_cache

logger = logging.getLogger(__name__)

# ── Mapbox feature → Roam category mapping ──────────────────────────────
# Mapbox returns `properties.category` as a comma-separated string for POIs,
# plus `place_type` which is one of: country, region, postcode, district,
# place, locality, neighborhood, address, poi, poi.landmark.
#
# We map the most useful ones to Roam's PlaceCategory vocabulary.

_MAPBOX_CAT_MAP: dict[str, str] = {
    "gas station": "fuel",
    "fuel": "fuel",
    "petrol": "fuel",
    "petrol station": "fuel",
    "restaurant": "restaurant",
    "cafe": "cafe",
    "coffee": "cafe",
    "coffee shop": "cafe",
    "fast food": "fast_food",
    "bar": "bar",
    "pub": "pub",
    "hotel": "hotel",
    "motel": "motel",
    "hostel": "hostel",
    "lodging": "hotel",
    "campground": "camp",
    "camping": "camp",
    "park": "park",
    "beach": "beach",
    "hospital": "hospital",
    "pharmacy": "pharmacy",
    "mechanic": "mechanic",
    "auto repair": "mechanic",
    "grocery": "grocery",
    "supermarket": "grocery",
    "viewpoint": "viewpoint",
    "attraction": "attraction",
    "tourist attraction": "attraction",
    "museum": "attraction",
    "toilet": "toilet",
    "rest area": "toilet",
}


def _classify(feature: dict[str, Any]) -> str:
    """Best-effort category from Mapbox feature → Roam PlaceCategory."""
    # 1) Check properties.category (comma-separated for POIs)
    props = feature.get("properties") or {}
    raw_cats = str(props.get("category") or "").lower()
    for token in raw_cats.split(","):
        token = token.strip()
        if token in _MAPBOX_CAT_MAP:
            return _MAPBOX_CAT_MAP[token]

    # 2) Fall back to place_type
    place_types = feature.get("place_type") or []
    if "poi.landmark" in place_types or "poi" in place_types:
        return "attraction"
    if "address" in place_types:
        return "address"
    if "place" in place_types or "locality" in place_types:
        return "town"
    if "neighborhood" in place_types:
        return "town"
    if "region" in place_types or "district" in place_types:
        return "region"

    return "place"


def _feature_to_item(feat: dict[str, Any]) -> PlaceItem | None:
    """Convert a single Mapbox GeoJSON feature to a Roam PlaceItem."""
    center = feat.get("center")
    if not center or len(center) < 2:
        return None

    lng, lat = float(center[0]), float(center[1])
    mapbox_id = feat.get("id", "")
    place_name = feat.get("place_name") or ""
    props = feat.get("properties") or {}
    place_types = feat.get("place_type") or []

    # For address features Mapbox puts the street name in `text` and the
    # house/unit number in `properties.address` - stitch them so the primary
    # label is "123 Elizabeth Street" rather than just "Elizabeth Street".
    #
    # Fallback: some Mapbox v5 responses omit `properties.address` but still
    # embed the house number as the leading token of `place_name` (e.g.
    # "123 Elizabeth St, Brisbane QLD 4000"). Parse that so we don't lose
    # granularity on those responses.
    text = feat.get("text") or place_name or ""
    house_number = str(props.get("address") or "").strip()
    is_address = "address" in place_types
    if not house_number and is_address and place_name:
        first_token = place_name.split(",", 1)[0].split(" ", 1)[0].strip()
        # Must start with digit; accept "12", "12A", "3-5", "1/220", etc.
        if first_token and first_token[0].isdigit():
            # Only treat as a house number if the first token is NOT already
            # part of `text` (avoids stripping a street number back off a
            # street like "21st Street").
            if not text.lstrip().startswith(first_token):
                house_number = first_token
    if is_address and house_number:
        name = f"{house_number} {text}".strip()
    else:
        name = text

    # Build a short address from context (suburb, city, state)
    context = feat.get("context") or []
    context_parts: list[str] = []
    for ctx in context:
        ctx_text = ctx.get("text")
        if ctx_text:
            context_parts.append(ctx_text)
    address = ", ".join(context_parts[:3]) if context_parts else ""

    category = _classify(feat)

    return PlaceItem(
        id=f"mapbox:{mapbox_id}",
        name=name,
        lat=lat,
        lng=lng,
        category=category,  # type: ignore[arg-type]
        extra={
            "source": "mapbox_geocoding",
            "mapbox_id": mapbox_id,
            "place_name": place_name,
            "address": address,
            "mapbox_category": str(props.get("category") or ""),
            "place_type": feat.get("place_type") or [],
            "relevance": feat.get("relevance", 0),
        },
    )


def _make_places_key(query: str, proximity: tuple[float, float] | None, limit: int) -> str:
    """Deterministic key for a Mapbox geocode request (for PlacesPack.places_key)."""
    seed = json.dumps(
        {
            "type": "mapbox_geocode",
            "query": query.strip().lower(),
            "proximity": list(proximity) if proximity else None,
            "limit": limit,
            "algo": settings.places_algo_version,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(seed.encode()).hexdigest()[:24]


# ── Allowed Mapbox place types ──────────────────────────────────────────
# We include all useful types. Omitting "country" and "postcode" because
# those are rarely what a user typing "Servo near Toowoomba" wants.
# Order matters: Mapbox uses it as a tiebreaker when relevance is equal,
# so putting `address` first means "123 Elizabeth St" outranks a generic
# "Elizabeth Street" POI when the query contains a house number.
_DEFAULT_TYPES = "address,poi,poi.landmark,place,locality,neighborhood,district,region"


class MapboxGeocoding:
    """Thin wrapper around Mapbox Geocoding v5 forward search."""

    BASE_URL = "https://api.mapbox.com/geocoding/v5/mapbox.places"

    def __init__(self, conn: sqlite3.Connection | None = None) -> None:
        self.token: str = settings.mapbox_token
        self.country: str = settings.mapbox_country
        self.conn = conn
        self.cache_ttl_s: int = settings.mapbox_geocode_cache_seconds
        if not self.token:
            raise RuntimeError(
                "ROAM_MAPBOX_TOKEN is not set. "
                "Add it to your .env or environment variables."
            )

    def search(
        self,
        query: str,
        *,
        proximity: tuple[float, float] | None = None,
        limit: int = 10,
        types: str | None = None,
        bbox: tuple[float, float, float, float] | None = None,
        language: str = "en",
    ) -> PlacesPack:
        """
        Forward geocode a free-text query → PlacesPack.

        Parameters
        ----------
        query : str
            Free-text search string (e.g. "BP Servo Toowoomba").
        proximity : (lat, lng) | None
            Bias results toward this point. Mapbox expects (lng, lat) on the wire  - 
            this method accepts (lat, lng) for consistency with the rest of Roam.
        limit : int
            Max results (Mapbox allows 1–10, default 5 without paid plan features).
        types : str | None
            Comma-separated Mapbox types filter. Defaults to a broad set.
        bbox : (minLng, minLat, maxLng, maxLat) | None
            Restrict results to a bounding box.
        language : str
            BCP-47 language code.
        """
        query = query.strip()
        if not query:
            return PlacesPack(
                places_key="empty",
                req=PlacesRequest(query="", limit=0),
                items=[],
                provider="mapbox_geocoding_v5",
                created_at=datetime.now(timezone.utc).isoformat(),
                algo_version=settings.places_algo_version,
            )

        limit = max(1, min(limit, 10))  # Mapbox hard-caps at 10

        # ── Check cache ──────────────────────────────────────────
        cache_key = _make_places_key(query, proximity, limit)
        if self.conn is not None:
            cached = get_geocode_cache(self.conn, cache_key)
            if cached:
                age_s = (
                    datetime.now(timezone.utc)
                    - datetime.fromisoformat(cached["created_at"])
                ).total_seconds()
                if age_s < self.cache_ttl_s:
                    logger.info("mapbox_geocode CACHE HIT key=%s age=%.0fs", cache_key, age_s)
                    return PlacesPack.model_validate(cached["pack"])
                logger.info("mapbox_geocode CACHE EXPIRED key=%s age=%.0fs", cache_key, age_s)

        params: dict[str, str] = {
            "access_token": self.token,
            "autocomplete": "true",
            "language": language,
            "limit": str(limit),
            "types": types or _DEFAULT_TYPES,
        }

        if self.country:
            params["country"] = self.country

        # Proximity: Roam uses (lat, lng), Mapbox expects "lng,lat"
        if proximity:
            lat, lng = proximity
            params["proximity"] = f"{lng},{lat}"

        if bbox:
            params["bbox"] = ",".join(str(v) for v in bbox)

        import urllib.parse
        encoded_query = urllib.parse.quote(query, safe="")
        url = f"{self.BASE_URL}/{encoded_query}.json"

        logger.info("mapbox_geocode query=%r proximity=%s limit=%d", query, proximity, limit)

        try:
            with httpx.Client(timeout=10.0) as client:
                resp = client.get(url, params=params)
                resp.raise_for_status()
                data = resp.json()
        except httpx.HTTPStatusError as exc:
            logger.error(
                "mapbox_geocode_http_error status=%d body=%s",
                exc.response.status_code,
                exc.response.text[:500],
            )
            raise RuntimeError(f"Mapbox geocoding failed: HTTP {exc.response.status_code}") from exc
        except httpx.TimeoutException as exc:
            logger.error("mapbox_geocode_timeout query=%r", query)
            raise RuntimeError("Mapbox geocoding timed out") from exc

        features = data.get("features") or []

        items: list[PlaceItem] = []
        for feat in features:
            item = _feature_to_item(feat)
            if item:
                items.append(item)

        logger.info("mapbox_geocode results=%d key=%s", len(items), cache_key)

        now_iso = datetime.now(timezone.utc).isoformat()
        pack = PlacesPack(
            places_key=cache_key,
            req=PlacesRequest(
                query=query,
                center=NavCoord(lat=proximity[0], lng=proximity[1]) if proximity else None,
                limit=limit,
            ),
            items=items,
            provider="mapbox_geocoding_v5",
            created_at=now_iso,
            algo_version=settings.places_algo_version,
        )

        # ── Write to cache ───────────────────────────────────────
        if self.conn is not None:
            try:
                put_geocode_cache(
                    self.conn,
                    cache_key=cache_key,
                    created_at=now_iso,
                    pack=pack.model_dump(),
                )
                logger.info("mapbox_geocode CACHED key=%s", cache_key)
            except Exception as exc:
                logger.warning("mapbox_geocode cache write failed: %s", exc)

        return pack