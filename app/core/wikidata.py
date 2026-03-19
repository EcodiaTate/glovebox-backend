# app/core/wikidata.py
"""
Wikidata enrichment for POI items that carry a `wikidata` OSM tag.

Fetches two properties per entity:
  P18  - main image (Wikimedia Commons filename)
  P856 - official website URL

For each P18 image we also call the Commons imageinfo API to resolve:
  - A thumbnail URL (400 px)
  - The licence name  (e.g. "CC BY-SA 4.0")
  - The attribution   (artist / author field)

Everything is batched to minimise round-trips:
  - Up to 50 Wikidata Q-IDs per entity API call
  - Up to 50 Commons filenames per imageinfo API call

Only CC-compatible licences (CC0, CC BY, CC BY-SA) are stored.
CC BY-NC and fully proprietary images are rejected so callers can
cache & display without manual licence review.

Rate limits:
  - Wikidata entity API: moderate, authenticated by User-Agent header
  - Commons imageinfo: same Wikimedia API tier
Both respond in < 1 s for small batches; we use a 10 s timeout.
"""
from __future__ import annotations

import logging
from typing import Dict, List, Optional, Tuple

import httpx

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────────────────────

_WIKIDATA_API = "https://www.wikidata.org/w/api.php"
_COMMONS_API  = "https://commons.wikimedia.org/w/api.php"
_THUMB_WIDTH  = 400   # px - match existing _WIKI_THUMB_WIDTH in places.py
_BATCH_SIZE   = 50    # max Q-IDs / filenames per API call

# Licences we will accept for offline caching in a commercial product.
# CC BY-SA is ShareAlike on the image itself, which is fine for display.
# We reject CC BY-NC and proprietary licences.
_ALLOWED_LICENCE_PREFIXES = (
    "cc0",
    "cc-pd",            # public domain dedication via Commons template
    "public domain",
    "cc by",            # covers CC BY 4.0, CC BY 3.0, CC BY 2.0 etc.
    "cc by-sa",         # covers CC BY-SA 4.0, CC BY-SA 3.0 etc.
)

_UA = "roam-backend/1.0 (https://ecodia.com.au; mailto:hello@ecodia.com.au)"


# ──────────────────────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────────────────────

class WikidataEnrichment:
    """
    Result for a single Wikidata Q-ID.

    Attributes:
        qid:                 e.g. "Q12345"
        website:             P856 official website, or None
        image_filename:      P18 Commons filename, or None  (e.g. "File:Foo.jpg")
        thumbnail_url:       Resolved Commons thumb URL, or None
        image_licence:       Short licence string, or None  (e.g. "CC BY-SA 4.0")
        image_attribution:   Attribution text, or None      (e.g. "© Jane Smith")
    """
    __slots__ = ("qid", "website", "image_filename",
                 "thumbnail_url", "image_licence", "image_attribution")

    def __init__(
        self,
        qid: str,
        *,
        website: Optional[str] = None,
        image_filename: Optional[str] = None,
        thumbnail_url: Optional[str] = None,
        image_licence: Optional[str] = None,
        image_attribution: Optional[str] = None,
    ) -> None:
        self.qid               = qid
        self.website           = website
        self.image_filename    = image_filename
        self.thumbnail_url     = thumbnail_url
        self.image_licence     = image_licence
        self.image_attribution = image_attribution


async def enrich_qids(
    qids: List[str],
    *,
    client: httpx.AsyncClient,
) -> Dict[str, WikidataEnrichment]:
    """
    Fetch Wikidata P18 + P856 for a list of Q-IDs, then resolve Commons
    imageinfo for any images found.

    Returns a dict keyed by Q-ID. Missing or failed Q-IDs are absent.
    """
    if not qids:
        return {}

    results: Dict[str, WikidataEnrichment] = {}

    # Step 1: batch-fetch entity claims from Wikidata
    for batch_start in range(0, len(qids), _BATCH_SIZE):
        batch = qids[batch_start : batch_start + _BATCH_SIZE]
        claims = await _fetch_wikidata_claims(batch, client=client)
        for qid, (website, image_filename) in claims.items():
            results[qid] = WikidataEnrichment(
                qid, website=website, image_filename=image_filename
            )

    # Step 2: batch-fetch Commons imageinfo for all found filenames
    filenames = [
        (qid, r.image_filename)
        for qid, r in results.items()
        if r.image_filename
    ]
    if filenames:
        imageinfo = await _fetch_commons_imageinfo(
            [fn for _, fn in filenames], client=client
        )
        for qid, filename in filenames:
            info = imageinfo.get(filename)
            if info:
                r = results[qid]
                r.thumbnail_url     = info.get("thumbnail_url")
                r.image_licence     = info.get("licence")
                r.image_attribution = info.get("attribution")

    return results


# ──────────────────────────────────────────────────────────────
# Internal helpers
# ──────────────────────────────────────────────────────────────

async def _fetch_wikidata_claims(
    qids: List[str],
    *,
    client: httpx.AsyncClient,
) -> Dict[str, Tuple[Optional[str], Optional[str]]]:
    """
    Returns {qid: (website, image_filename)} for each Q-ID.
    Uses the wbgetentities action - one HTTP call per batch.
    """
    params = {
        "action":   "wbgetentities",
        "ids":      "|".join(qids),
        "props":    "claims",
        "format":   "json",
        "formatversion": "2",
    }
    try:
        resp = await client.get(
            _WIKIDATA_API,
            params=params,
            headers={"User-Agent": _UA},
            timeout=10.0,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        logger.warning("[wikidata] entity fetch failed: %s", exc)
        return {}

    out: Dict[str, Tuple[Optional[str], Optional[str]]] = {}
    for qid in qids:
        entity = (data.get("entities") or {}).get(qid, {})
        claims = entity.get("claims") or {}

        website    = _first_string_value(claims.get("P856") or [])
        image_file = _first_string_value(claims.get("P18")  or [])

        # P18 values are bare filenames like "Foo.jpg" - normalise to "File:Foo.jpg"
        if image_file and not image_file.startswith("File:"):
            image_file = f"File:{image_file}"

        if website or image_file:
            out[qid] = (website, image_file)

    return out


def _first_string_value(claims: list) -> Optional[str]:
    """Extract the first string datavalue from a claims list."""
    for claim in claims:
        try:
            dv = claim["mainsnak"]["datavalue"]
            if dv["type"] == "string":
                return str(dv["value"])
        except (KeyError, TypeError):
            continue
    return None


async def _fetch_commons_imageinfo(
    filenames: List[str],
    *,
    client: httpx.AsyncClient,
) -> Dict[str, Dict[str, str]]:
    """
    Returns {filename: {thumbnail_url, licence, attribution}} for each file.
    Uses the query+imageinfo action - one HTTP call per batch.
    """
    out: Dict[str, Dict[str, str]] = {}

    for batch_start in range(0, len(filenames), _BATCH_SIZE):
        batch = filenames[batch_start : batch_start + _BATCH_SIZE]

        params = {
            "action":    "query",
            "titles":    "|".join(batch),
            "prop":      "imageinfo",
            "iiprop":    "url|extmetadata",
            "iiurlwidth": str(_THUMB_WIDTH),
            "format":    "json",
            "formatversion": "2",
        }
        try:
            resp = await client.get(
                _COMMONS_API,
                params=params,
                headers={"User-Agent": _UA},
                timeout=10.0,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            logger.warning("[wikidata] Commons imageinfo fetch failed: %s", exc)
            continue

        pages = (data.get("query") or {}).get("pages") or []
        # formatversion=2 gives a list, not a dict keyed by pageid
        if isinstance(pages, dict):
            pages = list(pages.values())

        for page in pages:
            title = page.get("title", "")
            ii_list = page.get("imageinfo") or []
            if not ii_list:
                continue
            ii = ii_list[0]

            thumb = ii.get("thumburl") or ii.get("url")
            if not thumb:
                continue

            extmeta = ii.get("extmetadata") or {}
            licence_raw  = (extmeta.get("LicenseShortName") or {}).get("value", "")
            attribution  = (
                (extmeta.get("Artist") or {}).get("value", "")
                or (extmeta.get("Credit") or {}).get("value", "")
            )
            # Strip HTML from attribution (Commons uses <a> tags etc.)
            attribution = _strip_html(attribution)[:200] if attribution else None

            # Reject non-commercial or unrecognised licences
            licence_lower = licence_raw.lower()
            if not any(licence_lower.startswith(p) for p in _ALLOWED_LICENCE_PREFIXES):
                logger.debug(
                    "[wikidata] skipping %s: licence %r not allowed", title, licence_raw
                )
                continue

            out[title] = {
                "thumbnail_url": thumb[:500],
                "licence":       licence_raw[:80],
                "attribution":   attribution or "",
            }

    return out


def _strip_html(text: str) -> str:
    """Very minimal HTML tag stripper - avoids importing html.parser for speed."""
    import re
    return re.sub(r"<[^>]+>", "", text).strip()
