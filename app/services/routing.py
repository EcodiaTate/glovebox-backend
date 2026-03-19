from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

import httpx

import math

import logging

from app.core.contracts import (
    AvoidZoneRequest,
    BBox4,
    NavLeg,
    NavManeuver,
    NavPack,
    NavRequest,
    NavRoute,
    NavStep,
    RouteAlternates,
    TripStop,
)
from app.core.errors import bad_request, service_unavailable
from app.core.keying import route_key_from_request
from app.core.polyline6 import decode_polyline6, encode_polyline6
from app.core.time import utc_now_iso

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────
# Geometry helpers
# ──────────────────────────────────────────────────────────────

def _bbox_from_coords(coords: List[Tuple[float, float]]) -> BBox4:
    """Compute bounding box from [(lat, lng), ...] pairs."""
    if not coords:
        return BBox4(minLng=0, minLat=0, maxLng=0, maxLat=0)
    lats = [c[0] for c in coords]
    lngs = [c[1] for c in coords]
    return BBox4(minLng=min(lngs), minLat=min(lats), maxLng=max(lngs), maxLat=max(lats))


def _concat_step_geometries(steps: List[NavStep]) -> str:
    """
    Build a single polyline6 from an ordered list of NavSteps.

    Each step's geometry starts where the previous one ended (shared junction
    point).  We decode each, drop the duplicate first point from steps 2+,
    then re-encode the full sequence.
    """
    if not steps:
        return ""

    all_pts: List[Tuple[float, float]] = []
    for i, step in enumerate(steps):
        if not step.geometry:
            continue
        pts = decode_polyline6(step.geometry)
        if i == 0:
            all_pts.extend(pts)
        else:
            # Skip first point - it's the same as the last point of the
            # previous step (the shared junction).
            if pts:
                all_pts.extend(pts[1:])

    return encode_polyline6(all_pts) if all_pts else ""


# ──────────────────────────────────────────────────────────────
# OSRM → NavStep parsing
# ──────────────────────────────────────────────────────────────

# Valid OSRM maneuver types we map 1:1.  Anything unknown falls back to "turn".
_KNOWN_MANEUVER_TYPES = frozenset({
    "turn", "depart", "arrive",
    "merge", "fork", "on ramp", "off ramp",
    "roundabout", "rotary", "exit roundabout",
    "new name", "continue", "end of road",
    "notification",
})

# Valid OSRM modifiers.  Anything unknown is dropped (None).
_KNOWN_MODIFIERS = frozenset({
    "left", "right",
    "slight left", "slight right",
    "sharp left", "sharp right",
    "straight", "uturn",
})


def _parse_maneuver(raw: Dict[str, Any]) -> NavManeuver:
    """Parse an OSRM maneuver dict into a NavManeuver."""
    raw_type = raw.get("type", "turn")
    raw_modifier = raw.get("modifier")
    loc = raw.get("location", [0, 0])  # OSRM gives [lng, lat]

    return NavManeuver(
        type=raw_type if raw_type in _KNOWN_MANEUVER_TYPES else "turn",
        modifier=raw_modifier if raw_modifier in _KNOWN_MODIFIERS else None,
        location=[float(loc[0]), float(loc[1])] if len(loc) >= 2 else [0.0, 0.0],
        bearing_before=int(raw.get("bearing_before", 0)),
        bearing_after=int(raw.get("bearing_after", 0)),
        exit=raw.get("exit"),
    )


def _parse_step(osrm_step: Dict[str, Any]) -> NavStep:
    """Parse a single OSRM step dict into a NavStep."""
    m = osrm_step.get("maneuver", {})
    return NavStep(
        maneuver=_parse_maneuver(m),
        name=osrm_step.get("name", ""),
        ref=osrm_step.get("ref") or None,
        distance_m=float(osrm_step.get("distance", 0)),
        duration_s=float(osrm_step.get("duration", 0)),
        geometry=osrm_step.get("geometry", ""),  # already polyline6
        mode=osrm_step.get("mode", "driving"),
        pronunciation=osrm_step.get("pronunciation") or None,
    )


def _parse_osrm_leg(
    osrm_leg: Dict[str, Any],
    idx: int,
    from_stop_id: Optional[str],
    to_stop_id: Optional[str],
) -> NavLeg:
    """
    Parse an OSRM leg into a NavLeg with full step data.

    The leg geometry is built by concatenating step geometries (which is more
    accurate than the overview geometry for multi-leg routes).
    """
    steps = [_parse_step(s) for s in osrm_leg.get("steps", [])]

    # Build per-leg geometry from step segments
    leg_geometry = _concat_step_geometries(steps)

    return NavLeg(
        idx=idx,
        from_stop_id=from_stop_id,
        to_stop_id=to_stop_id,
        distance_m=int(round(float(osrm_leg.get("distance", 0)))),
        duration_s=int(round(float(osrm_leg.get("duration", 0)))),
        geometry=leg_geometry,
        steps=steps,
    )


# ──────────────────────────────────────────────────────────────
# Route hazard scoring
# ──────────────────────────────────────────────────────────────

def _haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Great-circle distance in km between two points."""
    d_lat = math.radians(lat2 - lat1)
    d_lng = math.radians(lng2 - lng1)
    a = (
        math.sin(d_lat / 2) ** 2
        + math.cos(math.radians(lat1))
        * math.cos(math.radians(lat2))
        * math.sin(d_lng / 2) ** 2
    )
    return 2 * 6371 * math.asin(min(1.0, math.sqrt(a)))


def _score_route_against_zones(
    route_pts: List[Tuple[float, float]],
    zones: List[AvoidZoneRequest],
    sample_every: int = 20,
) -> float:
    """
    Score a route based on proximity to avoid zones.
    Lower score = less hazard exposure = better.

    Samples the route geometry every `sample_every` points and sums up
    penalty for points within zone radii. Penalty is proportional to
    proximity (closer = higher penalty).
    """
    if not zones or not route_pts:
        return 0.0

    total_penalty = 0.0
    for idx in range(0, len(route_pts), sample_every):
        lat, lng = route_pts[idx]
        for z in zones:
            d = _haversine_km(lat, lng, z.lat, z.lng)
            if d < z.radius_km:
                # Closer to centre = higher penalty
                proximity = 1.0 - (d / z.radius_km)
                total_penalty += proximity * 100.0

    return total_penalty


# ──────────────────────────────────────────────────────────────
# Routing service
# ──────────────────────────────────────────────────────────────

class Routing:
    def __init__(self, *, osrm_base_url: str, osrm_profile: str, algo_version: str):
        self.osrm_base_url = osrm_base_url.rstrip("/")
        self.osrm_profile = osrm_profile
        self.algo_version = algo_version
        self.client = httpx.Client(timeout=30.0)

    def _parse_osrm_route(
        self,
        osrm_route: Dict[str, Any],
        req: NavRequest,
        rkey_suffix: str = "",
    ) -> Optional[NavRoute]:
        """Parse a single OSRM route dict into a NavRoute."""
        overview_poly6: str = osrm_route.get("geometry", "")
        if not overview_poly6:
            return None

        overview_pts = decode_polyline6(overview_poly6)
        bbox = _bbox_from_coords(overview_pts)

        dist_m = int(round(float(osrm_route.get("distance") or 0)))
        dur_s = int(round(float(osrm_route.get("duration") or 0)))

        osrm_legs = osrm_route.get("legs") or []
        legs_out: List[NavLeg] = []
        for i, osrm_leg in enumerate(osrm_legs):
            from_id = req.stops[i].id if i < len(req.stops) else None
            to_id = req.stops[i + 1].id if i + 1 < len(req.stops) else None
            legs_out.append(_parse_osrm_leg(osrm_leg, i, from_id, to_id))

        req_dict: Dict[str, Any] = req.model_dump()
        rkey = route_key_from_request(req_dict, self.algo_version)
        if rkey_suffix:
            rkey = f"{rkey}_{rkey_suffix}"

        return NavRoute(
            route_key=rkey,
            profile=req.profile,
            distance_m=dist_m,
            duration_s=dur_s,
            geometry=overview_poly6,
            bbox=bbox,
            legs=legs_out,
            provider="osrm",
            created_at=utc_now_iso(),
            algo_version=self.algo_version,
        )

    # ── OSRM call helper ────────────────────────────────────────

    def _call_osrm(
        self, stops: List[TripStop], *, alternatives: int = 0,
    ) -> Optional[Dict[str, Any]]:
        """
        Call OSRM and return the JSON body, or *None* if OSRM responds
        with a ``NoRoute`` / ``NoSegment`` error (i.e. impossible route).

        Raises ``service_unavailable`` only for real failures (network,
        5xx, unexpected status).
        """
        coords = ";".join([f"{s.lng},{s.lat}" for s in stops])
        url = f"{self.osrm_base_url}/route/v1/{self.osrm_profile}/{coords}"
        params = {
            "overview": "full",
            "geometries": "polyline6",
            "steps": "true",
            "annotations": "distance,duration,speed",
            "alternatives": str(alternatives) if alternatives else "false",
        }

        try:
            r = self.client.get(url, params=params)
        except Exception as e:
            service_unavailable("osrm_unreachable", f"OSRM request failed: {e}")

        if r.status_code == 200:
            return r.json()

        # OSRM returns 400 for client-routable failures like NoRoute / NoSegment.
        # These are not server errors - they mean the graph can't connect the points.
        if r.status_code == 400:
            try:
                body = r.json()
            except Exception:
                body = {}
            osrm_code = body.get("code", "")
            if osrm_code in ("NoRoute", "NoSegment"):
                logger.info("OSRM %s between %d stops", osrm_code, len(stops))
                return None

        # Anything else is a genuine upstream failure
        service_unavailable(
            "osrm_error",
            f"OSRM returned {r.status_code}: {r.text[:500]}",
        )

    # ── Detour waypoint generation ────────────────────────────

    @staticmethod
    def _detour_waypoints(
        start: TripStop,
        end: TripStop,
        zones: List[AvoidZoneRequest],
    ) -> List[TripStop]:
        """
        Generate intermediate via-waypoints that steer the route around
        each avoid zone.

        For each zone that lies roughly between start and end, we create
        a waypoint offset perpendicular to the start→end bearing, placed
        just outside the zone radius.  This gives OSRM a concrete path
        that avoids the hazard area.
        """
        if not zones:
            return []

        s_lat = math.radians(start.lat)
        s_lng = math.radians(start.lng)
        e_lat = math.radians(end.lat)
        e_lng = math.radians(end.lng)

        # Bearing from start to end
        d_lng = e_lng - s_lng
        x = math.cos(e_lat) * math.sin(d_lng)
        y = (
            math.cos(s_lat) * math.sin(e_lat)
            - math.sin(s_lat) * math.cos(e_lat) * math.cos(d_lng)
        )
        bearing = math.atan2(x, y)

        detours: List[Tuple[float, TripStop]] = []
        for z in zones:
            # Only create a detour for zones that are between start and end
            # (project zone centre onto the start→end line)
            d_start = _haversine_km(start.lat, start.lng, z.lat, z.lng)
            d_end = _haversine_km(end.lat, end.lng, z.lat, z.lng)
            total = _haversine_km(start.lat, start.lng, end.lat, end.lng)
            if total < 1:
                continue
            # Zone is roughly "between" if both distances are < total + some slack
            if d_start > total * 1.3 or d_end > total * 1.3:
                continue

            # Offset perpendicular to bearing, just outside zone radius
            offset_km = z.radius_km + 5.0  # 5 km buffer beyond zone edge
            # Angular distance in radians
            angular = offset_km / 6371.0

            # Perpendicular bearing (try the side closer to start→end midpoint)
            perp_bearing = bearing + math.pi / 2

            # Compute offset point from zone centre
            z_lat = math.radians(z.lat)
            z_lng = math.radians(z.lng)

            wp_lat = math.asin(
                math.sin(z_lat) * math.cos(angular)
                + math.cos(z_lat) * math.sin(angular) * math.cos(perp_bearing)
            )
            wp_lng = z_lng + math.atan2(
                math.sin(perp_bearing) * math.sin(angular) * math.cos(z_lat),
                math.cos(angular) - math.sin(z_lat) * math.sin(wp_lat),
            )

            wp = TripStop(
                type="via",
                lat=math.degrees(wp_lat),
                lng=math.degrees(wp_lng),
            )

            # Sort key = distance from start so waypoints are in route order
            detours.append((d_start, wp))

        detours.sort(key=lambda t: t[0])
        return [wp for _, wp in detours]

    # ── Main route method ─────────────────────────────────────

    def route(self, req: NavRequest) -> NavPack:
        if len(req.stops) < 2:
            bad_request("bad_nav_request", "stops must contain at least 2 points")

        has_avoid = bool(req.avoid_zones)
        warnings: List[str] = []

        # 1) Primary OSRM call (with alternatives when avoid zones present)
        data = self._call_osrm(
            req.stops, alternatives=3 if has_avoid else 0,
        )

        # 2) If NoRoute and we have avoid zones, try routing via detour waypoints
        if data is None and has_avoid:
            logger.info("NoRoute with avoid zones - attempting detour waypoints")
            detour_wps = self._detour_waypoints(
                req.stops[0], req.stops[-1], req.avoid_zones,
            )
            if detour_wps:
                detour_stops = [req.stops[0]] + detour_wps + [req.stops[-1]]
                data = self._call_osrm(detour_stops, alternatives=0)
                if data is not None:
                    warnings.append(
                        "Route diverted around hazard zone(s). "
                        "Check conditions before travelling."
                    )

        # 3) If still no route, fall back to routing without avoid zones
        if data is None and has_avoid:
            logger.info("Detour failed - falling back to direct route (no avoidance)")
            data = self._call_osrm(req.stops, alternatives=0)
            if data is not None:
                warnings.append(
                    "Could not find a route avoiding all hazard zones. "
                    "This route may pass through warned areas - check "
                    "conditions before travelling."
                )

        # 4) Still nothing - hard fail
        if data is None:
            service_unavailable(
                "osrm_no_route",
                "No route could be found between the given points",
            )

        routes = data.get("routes") or []
        if not routes:
            service_unavailable("osrm_no_routes", "OSRM returned no routes")

        # Parse all candidate routes
        candidates: List[NavRoute] = []
        for i, osrm_route in enumerate(routes):
            parsed = self._parse_osrm_route(
                osrm_route, req, rkey_suffix=f"alt{i}" if i > 0 else "",
            )
            if parsed:
                candidates.append(parsed)

        if not candidates:
            service_unavailable("osrm_bad_geometry", "OSRM returned no usable routes")

        # If avoid zones are present, score each route and pick the safest one
        # that doesn't add too much distance
        if has_avoid and len(candidates) > 1:
            base_dist = candidates[0].distance_m
            scored: List[Tuple[float, int, NavRoute]] = []

            for idx, c in enumerate(candidates):
                pts = decode_polyline6(c.geometry)
                hazard_score = _score_route_against_zones(pts, req.avoid_zones)

                # Penalize routes that are significantly longer (>50% more distance)
                dist_ratio = c.distance_m / max(1, base_dist)
                distance_penalty = max(0, (dist_ratio - 1.0) * 200)

                # Combined score: lower is better
                # Hazard score heavily weighted - safety over speed
                total = hazard_score + distance_penalty
                scored.append((total, idx, c))

            scored.sort(key=lambda x: x[0])
            primary = scored[0][2]
            alternates = [s[2] for s in scored[1:]]
        else:
            primary = candidates[0]
            alternates = candidates[1:] if len(candidates) > 1 else []

        return NavPack(
            req=req,
            primary=primary,
            alternates=RouteAlternates(alternates=alternates),
            warnings=warnings,
        )
