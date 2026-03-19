from __future__ import annotations

import io
import logging
import zipfile
from dataclasses import dataclass

from app.core.contracts import OfflineBundleManifest
from app.core.errors import not_found
from app.core.storage import (
    put_manifest,
    get_manifest,
    get_manifest_raw,
    get_nav_pack_raw,
    bulk_pack_bytes,
    bulk_pack_raw,
)
from app.core.time import utc_now_iso

logger = logging.getLogger(__name__)


# Maps overlay pack-type → ZIP filename
_OVERLAY_ZIP_NAMES = {
    "places":       "places.json",
    "traffic":      "traffic.json",
    "hazards":      "hazards.json",
    "weather":      "weather.json",
    "flood":        "flood.json",
    "fuel":         "fuel.json",
    "coverage":     "coverage.json",
    "wildlife":     "wildlife.json",
    "rest":         "rest_areas.json",
    "score":        "route_score.json",
    "emergency":    "emergency.json",
    "heritage":     "heritage.json",
    "aqi":          "air_quality.json",
    "bushfire":     "bushfire.json",
    "cameras":      "speed_cameras.json",
    "toilets":      "toilets.json",
    "school_zones": "school_zones.json",
    "roadkill":     "roadkill.json",
}

# Maps manifest attribute name → pack-type for bulk queries
_MANIFEST_KEY_MAP = {
    "corridor_key":      "corridor",
    "places_key":        "places",
    "traffic_key":       "traffic",
    "hazards_key":       "hazards",
    "weather_key":       "weather",
    "flood_key":         "flood",
    "fuel_key":          "fuel",
    "coverage_key":      "coverage",
    "wildlife_key":      "wildlife",
    "rest_key":          "rest",
    "score_key":         "score",
    "emergency_key":     "emergency",
    "heritage_key":      "heritage",
    "aqi_key":           "aqi",
    "bushfire_key":      "bushfire",
    "cameras_key":       "cameras",
    "toilets_key":       "toilets",
    "school_zones_key":  "school_zones",
    "roadkill_key":      "roadkill",
}


@dataclass(frozen=True)
class BundleZipResult:
    plan_id: str
    zip_bytes: bytes
    bytes_zip: int
    bytes_manifest: int
    bytes_navpack: int
    bytes_corridor: int
    bytes_places: int
    bytes_traffic: int
    bytes_hazards: int
    bytes_weather: int
    bytes_fuel: int
    bytes_flood: int
    bytes_coverage: int
    bytes_wildlife: int
    bytes_rest: int = 0
    bytes_score: int = 0
    bytes_emergency: int = 0
    bytes_heritage: int = 0
    bytes_aqi: int = 0
    bytes_bushfire: int = 0
    bytes_cameras: int = 0
    bytes_toilets: int = 0
    bytes_school_zones: int = 0
    bytes_roadkill: int = 0


class Bundle:
    def __init__(self, *, conn):
        self.conn = conn

    def build_manifest(
        self,
        *,
        plan_id: str,
        route_key: str,
        styles: list[str],
        navpack_ready: bool,
        corridor_key: str | None,
        corridor_ready: bool,
        places_key: str | None,
        places_ready: bool,
        traffic_key: str | None,
        traffic_ready: bool,
        hazards_key: str | None,
        hazards_ready: bool,
        weather_key: str | None = None,
        weather_ready: bool = False,
        flood_key: str | None = None,
        flood_ready: bool = False,
        fuel_key: str | None = None,
        fuel_ready: bool = False,
        coverage_key: str | None = None,
        coverage_ready: bool = False,
        wildlife_key: str | None = None,
        wildlife_ready: bool = False,
        rest_key: str | None = None,
        rest_ready: bool = False,
        score_key: str | None = None,
        score_ready: bool = False,
        emergency_key: str | None = None,
        emergency_ready: bool = False,
        heritage_key: str | None = None,
        heritage_ready: bool = False,
        aqi_key: str | None = None,
        aqi_ready: bool = False,
        bushfire_key: str | None = None,
        bushfire_ready: bool = False,
        cameras_key: str | None = None,
        cameras_ready: bool = False,
        toilets_key: str | None = None,
        toilets_ready: bool = False,
        school_zones_key: str | None = None,
        school_zones_ready: bool = False,
        roadkill_key: str | None = None,
        roadkill_ready: bool = False,
    ) -> OfflineBundleManifest:
        # Build key map for bulk byte-size query - only include ready overlays.
        ready_flags = {
            "corridor": corridor_ready, "places": places_ready,
            "traffic": traffic_ready, "hazards": hazards_ready,
            "weather": weather_ready, "flood": flood_ready,
            "fuel": fuel_ready, "coverage": coverage_ready,
            "wildlife": wildlife_ready, "rest": rest_ready,
            "score": score_ready, "emergency": emergency_ready,
            "heritage": heritage_ready, "aqi": aqi_ready,
            "bushfire": bushfire_ready, "cameras": cameras_ready,
            "toilets": toilets_ready, "school_zones": school_zones_ready,
            "roadkill": roadkill_ready,
        }
        key_map = {
            "corridor": corridor_key, "places": places_key,
            "traffic": traffic_key, "hazards": hazards_key,
            "weather": weather_key, "flood": flood_key,
            "fuel": fuel_key, "coverage": coverage_key,
            "wildlife": wildlife_key, "rest": rest_key,
            "score": score_key, "emergency": emergency_key,
            "heritage": heritage_key, "aqi": aqi_key,
            "bushfire": bushfire_key, "cameras": cameras_key,
            "toilets": toilets_key, "school_zones": school_zones_key,
            "roadkill": roadkill_key,
        }

        # Only query sizes for overlays that are ready AND have a key.
        query_keys = {
            "nav": route_key if navpack_ready else None,
        }
        for pack_type, key in key_map.items():
            query_keys[pack_type] = key if (ready_flags.get(pack_type) and key) else None

        sizes = bulk_pack_bytes(self.conn, query_keys)
        bytes_total = sum(sizes.values())

        m = OfflineBundleManifest(
            plan_id=plan_id,
            route_key=route_key,
            styles=styles,
            navpack_status="ready" if navpack_ready else "missing",
            corridor_status="ready" if corridor_ready else "missing",
            places_status="ready" if places_ready else "missing",
            traffic_status="ready" if traffic_ready else "missing",
            hazards_status="ready" if hazards_ready else "missing",
            weather_status="ready" if weather_ready else "missing",
            flood_status="ready" if flood_ready else "missing",
            fuel_status="ready" if fuel_ready else "missing",
            coverage_status="ready" if coverage_ready else "missing",
            wildlife_status="ready" if wildlife_ready else "missing",
            rest_status="ready" if rest_ready else "missing",
            score_status="ready" if score_ready else "missing",
            emergency_status="ready" if emergency_ready else "missing",
            heritage_status="ready" if heritage_ready else "missing",
            aqi_status="ready" if aqi_ready else "missing",
            bushfire_status="ready" if bushfire_ready else "missing",
            cameras_status="ready" if cameras_ready else "missing",
            toilets_status="ready" if toilets_ready else "missing",
            school_zones_status="ready" if school_zones_ready else "missing",
            roadkill_status="ready" if roadkill_ready else "missing",
            corridor_key=corridor_key,
            places_key=places_key,
            traffic_key=traffic_key,
            hazards_key=hazards_key,
            weather_key=weather_key,
            flood_key=flood_key,
            fuel_key=fuel_key,
            coverage_key=coverage_key,
            wildlife_key=wildlife_key,
            rest_key=rest_key,
            score_key=score_key,
            emergency_key=emergency_key,
            heritage_key=heritage_key,
            aqi_key=aqi_key,
            bushfire_key=bushfire_key,
            cameras_key=cameras_key,
            toilets_key=toilets_key,
            school_zones_key=school_zones_key,
            roadkill_key=roadkill_key,
            bytes_total=bytes_total,
            created_at=utc_now_iso(),
        )

        put_manifest(
            self.conn,
            plan_id=plan_id,
            route_key=route_key,
            created_at=m.created_at,
            manifest=m.model_dump(),
        )
        return m

    def build_zip(self, *, plan_id: str) -> BundleZipResult:
        # Read manifest to get the keys for each pack.
        manifest_row = get_manifest(self.conn, plan_id)
        if not manifest_row:
            not_found("bundle_missing", f"no manifest for plan_id {plan_id}")
        manifest = OfflineBundleManifest.model_validate(manifest_row)

        # Fetch manifest raw bytes.
        b_manifest = get_manifest_raw(self.conn, plan_id) or b""

        # Fetch nav pack (required).
        b_nav = get_nav_pack_raw(self.conn, manifest.route_key)
        if not b_nav:
            not_found("navpack_missing", f"no navpack cached for route_key {manifest.route_key}")

        # Build key map for all optional overlays + corridor.
        overlay_keys = {
            "corridor": manifest.corridor_key,
        }
        for manifest_attr, pack_type in _MANIFEST_KEY_MAP.items():
            if pack_type == "corridor":
                continue  # already added
            overlay_keys[pack_type] = getattr(manifest, manifest_attr, None)

        # Single bulk fetch of all raw blobs.
        raw_blobs = bulk_pack_raw(self.conn, overlay_keys)

        # Corridor is required.
        b_corr = raw_blobs.get("corridor")
        if not b_corr:
            if not manifest.corridor_key:
                not_found("corridor_missing", "manifest has no corridor_key")
            not_found("corridor_missing", f"no corridor cached for corridor_key {manifest.corridor_key}")

        # Log cache misses for optional overlays.
        for pack_type, key in overlay_keys.items():
            if pack_type == "corridor":
                continue
            if key and not raw_blobs.get(pack_type):
                logger.warning("%s cache miss for key %s - omitting from ZIP", pack_type, key)

        # compresslevel=1 is fastest DEFLATE (Zlib level 1: speed >> size).
        # JSON compresses well at any level; level 1 is ~3-5× faster than default 6.
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=1) as z:
            z.writestr("manifest.json", b_manifest)
            z.writestr("navpack.json", b_nav)
            z.writestr("corridor.json", b_corr)
            for pack_type, zip_name in _OVERLAY_ZIP_NAMES.items():
                blob = raw_blobs.get(pack_type)
                if blob:
                    z.writestr(zip_name, blob)

        zip_bytes = buf.getvalue()

        return BundleZipResult(
            plan_id=plan_id,
            zip_bytes=zip_bytes,
            bytes_zip=len(zip_bytes),
            bytes_manifest=len(b_manifest),
            bytes_navpack=len(b_nav),
            bytes_corridor=len(b_corr),
            bytes_places=len(raw_blobs.get("places") or b""),
            bytes_traffic=len(raw_blobs.get("traffic") or b""),
            bytes_hazards=len(raw_blobs.get("hazards") or b""),
            bytes_weather=len(raw_blobs.get("weather") or b""),
            bytes_flood=len(raw_blobs.get("flood") or b""),
            bytes_fuel=len(raw_blobs.get("fuel") or b""),
            bytes_coverage=len(raw_blobs.get("coverage") or b""),
            bytes_wildlife=len(raw_blobs.get("wildlife") or b""),
            bytes_rest=len(raw_blobs.get("rest") or b""),
            bytes_score=len(raw_blobs.get("score") or b""),
            bytes_emergency=len(raw_blobs.get("emergency") or b""),
            bytes_heritage=len(raw_blobs.get("heritage") or b""),
            bytes_aqi=len(raw_blobs.get("aqi") or b""),
            bytes_bushfire=len(raw_blobs.get("bushfire") or b""),
            bytes_cameras=len(raw_blobs.get("cameras") or b""),
            bytes_toilets=len(raw_blobs.get("toilets") or b""),
            bytes_school_zones=len(raw_blobs.get("school_zones") or b""),
            bytes_roadkill=len(raw_blobs.get("roadkill") or b""),
        )
