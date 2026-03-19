# app/services/inaturalist.py
"""
iNaturalist Node API v1 client.

Fetches wildlife observation data filtered to commercially-usable
Creative Commons licenses (CC0 and CC-BY).

Rate limits (strict):
  - 60 requests per minute
  - 10,000 requests per day (not tracked here - operator responsibility)

Image URL rules:
  ALLOW   inaturalist-open-data.s3.amazonaws.com  (CC0 / CC-BY open S3)
  REJECT  static.inaturalist.org                  (All Rights Reserved)

Reference: https://api.inaturalist.org/v1/docs/
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, List, Optional

import httpx
from pydantic import BaseModel

logger = logging.getLogger(__name__)

_BASE_URL = "https://api.inaturalist.org/v1"

# Only these CC licenses allow commercial use.
_COMMERCIAL_LICENSES = "cc0,cc-by"

# Image URL allow/deny
_ALLOWED_IMG_HOST = "inaturalist-open-data.s3.amazonaws.com"
_DENIED_IMG_HOST = "static.inaturalist.org"

# Available image size suffixes
_SIZE_SUFFIXES = ("square", "thumb", "small", "medium", "large", "original")


# ──────────────────────────────────────────────────────────────
# Output model
# ──────────────────────────────────────────────────────────────

class INatObservation(BaseModel):
    id: int
    species_guess: Optional[str] = None
    location: List[float]           # [lat, lng]
    attribution: str
    photos: List[str]               # filtered + size-swapped photo URLs


# ──────────────────────────────────────────────────────────────
# Rate limiter (token-bucket, 60 req/min)
# ──────────────────────────────────────────────────────────────

class _RateLimiter:
    """
    Simple async token-bucket rate limiter.

    Allows `rate` requests per `period` seconds.
    Callers await acquire() before each request.
    """

    def __init__(self, rate: int = 60, period: float = 60.0) -> None:
        self._rate = rate
        self._period = period
        self._tokens = float(rate)
        self._last_refill = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            now = time.monotonic()
            elapsed = now - self._last_refill
            # Refill tokens proportionally
            self._tokens = min(
                float(self._rate),
                self._tokens + elapsed * (self._rate / self._period),
            )
            self._last_refill = now
            if self._tokens < 1.0:
                wait = (1.0 - self._tokens) * (self._period / self._rate)
                await asyncio.sleep(wait)
                self._tokens = 0.0
            else:
                self._tokens -= 1.0


# ──────────────────────────────────────────────────────────────
# Image helpers
# ──────────────────────────────────────────────────────────────

def _is_open_image_url(url: str) -> bool:
    """Return True only for URLs hosted on the open-data S3 bucket."""
    return _ALLOWED_IMG_HOST in url and _DENIED_IMG_HOST not in url


def swap_image_size(url: str, size: str) -> str:
    """
    Replace the size suffix in an iNaturalist photo URL.

    Example:
      swap_image_size(".../photos/123/medium.jpg", "large")
      → ".../photos/123/large.jpg"

    Supported sizes: square, thumb, small, medium, large, original
    """
    if size not in _SIZE_SUFFIXES:
        raise ValueError(f"Invalid size '{size}'. Choose from: {_SIZE_SUFFIXES}")
    for suffix in _SIZE_SUFFIXES:
        needle = f"/{suffix}."
        if needle in url:
            return url.replace(needle, f"/{size}.", 1)
    return url  # no recognisable suffix - return unchanged


# ──────────────────────────────────────────────────────────────
# Data sanitiser
# ──────────────────────────────────────────────────────────────

def _build_attribution(obs: Dict[str, Any]) -> str:
    """
    Construct a human-readable attribution string from the raw observation.

    Prefers the API-supplied `license_code` field; falls back to a generic
    CC-BY statement.
    """
    license_code: str = (obs.get("license_code") or "cc-by").upper().replace("CC0", "CC0 1.0")
    taxon_name: str = obs.get("species_guess") or (
        (obs.get("taxon") or {}).get("preferred_common_name") or "Unknown species"
    )
    user: str = ((obs.get("user") or {}).get("login") or "unknown")
    return f"© {user} via iNaturalist, {taxon_name}, {license_code}"


def _sanitise_observation(obs: Dict[str, Any], photo_size: str = "medium") -> Optional[INatObservation]:
    """
    Parse one raw iNaturalist observation dict into a clean INatObservation.

    Returns None if the observation lacks a valid location or any open photos.
    """
    # Location - API returns "lat,lng" as a string or separate fields
    location_str: Optional[str] = obs.get("location")
    if not location_str:
        return None
    try:
        parts = [float(x) for x in location_str.split(",")]
        if len(parts) != 2:
            return None
        lat, lng = parts
    except (ValueError, AttributeError):
        return None

    # Photos - filter to open-licensed S3 URLs only
    raw_photos: List[Dict[str, Any]] = obs.get("photos") or []
    open_photos: List[str] = []
    for photo in raw_photos:
        url: str = photo.get("url") or ""
        if not url or not _is_open_image_url(url):
            continue
        # Swap to requested size
        open_photos.append(swap_image_size(url, photo_size))

    if not open_photos:
        return None  # no commercially usable images

    obs_id: Optional[int] = obs.get("id")
    if obs_id is None:
        return None

    species_guess: Optional[str] = obs.get("species_guess") or (
        (obs.get("taxon") or {}).get("preferred_common_name")
    )

    return INatObservation(
        id=int(obs_id),
        species_guess=species_guess,
        location=[lat, lng],
        attribution=_build_attribution(obs),
        photos=open_photos,
    )


# ──────────────────────────────────────────────────────────────
# Client
# ──────────────────────────────────────────────────────────────

class INaturalistClient:
    """
    Async iNaturalist API v1 client.

    Rate-limited to 60 req/min. All observation queries permanently
    enforce license=cc0,cc-by (commercial-use CC licenses only).

    Usage:
        async with INaturalistClient() as client:
            obs = await client.get_observations(lat=-27.5, lng=153.0, radius=50)
    """

    def __init__(
        self,
        *,
        timeout_s: float = 15.0,
        rate_per_min: int = 60,
    ) -> None:
        self._timeout = timeout_s
        self._limiter = _RateLimiter(rate=rate_per_min, period=60.0)
        self._client: Optional[httpx.AsyncClient] = None

    async def __aenter__(self) -> "INaturalistClient":
        self._client = httpx.AsyncClient(
            base_url=_BASE_URL,
            timeout=self._timeout,
            headers={
                "Accept": "application/json",
                "User-Agent": "Roam/1.0 (wildlife-overlay; contact: dev@ecodia.au)",
            },
        )
        return self

    async def __aexit__(self, *_: Any) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    async def get_observations(
        self,
        *,
        lat: float,
        lng: float,
        radius: float = 50.0,
        taxon_id: Optional[int] = None,
        per_page: int = 200,
        photo_size: str = "medium",
        extra_params: Optional[Dict[str, Any]] = None,
    ) -> List[INatObservation]:
        """
        Fetch wildlife observations near a point.

        Args:
            lat: Latitude of search centre.
            lng: Longitude of search centre.
            radius: Search radius in kilometres (default 50).
            taxon_id: Optional iNaturalist taxon ID to filter by.
            per_page: Max results to return (max 200 per API spec).
            photo_size: One of square/thumb/small/medium/large/original.
            extra_params: Additional query parameters to merge.

        Returns:
            List of sanitised observations with open-licensed photos only.

        Note:
            license=cc0,cc-by is always enforced regardless of extra_params.
        """
        if self._client is None:
            raise RuntimeError("Use INaturalistClient as an async context manager")

        params: Dict[str, Any] = {
            "lat": lat,
            "lng": lng,
            "radius": radius,
            "license": _COMMERCIAL_LICENSES,   # permanently hardcoded
            "photo_license": _COMMERCIAL_LICENSES,
            "photos": "true",
            "per_page": min(per_page, 200),
            "order": "desc",
            "order_by": "created_at",
        }
        if taxon_id is not None:
            params["taxon_id"] = taxon_id
        if extra_params:
            # Never allow callers to override the license parameters
            safe_extras = {
                k: v for k, v in extra_params.items()
                if k not in ("license", "photo_license")
            }
            params.update(safe_extras)

        await self._limiter.acquire()

        try:
            resp = await self._client.get("/observations", params=params)
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            logger.warning("[inat] HTTP %s for observations near %.4f,%.4f: %s",
                           exc.response.status_code, lat, lng, exc)
            return []
        except httpx.RequestError as exc:
            logger.warning("[inat] Request error for observations near %.4f,%.4f: %s", lat, lng, exc)
            return []

        data = resp.json()
        raw_results: List[Dict[str, Any]] = data.get("results") or []

        observations: List[INatObservation] = []
        for raw in raw_results:
            parsed = _sanitise_observation(raw, photo_size=photo_size)
            if parsed is not None:
                observations.append(parsed)

        logger.debug(
            "[inat] %.4f,%.4f r=%skm → %d raw / %d open-licensed",
            lat, lng, radius, len(raw_results), len(observations),
        )
        return observations
