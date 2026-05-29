# app/services/guide.py
"""
Roam Guide - AI road trip companion for Australia.
Powered by DeepSeek-V3 via OpenAI-compatible /chat/completions API.
"""

from __future__ import annotations

import json
import math
import re
import uuid
from datetime import datetime
from typing import Any, Dict, List, Tuple

import logging

import httpx
from pydantic import ValidationError

from app.core.settings import settings
from app.core.contracts import (
    PlacesRequest,
    CorridorPlacesRequest,
    PlacesSuggestRequest,
    GuideMsg,
    TripProgress,
    WirePlace,
    GuideContext,
    GuideAction,
    GuideToolCall,
    GuideToolResult,
    GuideTurnRequest,
    GuideTurnResponse,
    GuideSource,
)
from app.services.guide_search import web_search

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════════════════════════


def _format_speed(speed_mps: float | None) -> str:
    if speed_mps is None or speed_mps < 0:
        return "unknown"
    return f"{speed_mps * 3.6:.0f} km/h"


def _trip_phase(progress: TripProgress | None, total_km: float | None) -> str:
    if not progress or not total_km or total_km <= 0:
        return "planning"
    pct = progress.km_from_start / total_km
    if pct < 0.05:
        return "departing"
    elif pct < 0.35:
        return "early_cruise"
    elif pct < 0.65:
        return "midway"
    elif pct < 0.90:
        return "home_stretch"
    else:
        return "arriving"


def _format_route_score(route_score: Dict[str, Any] | None) -> str:
    """Format RouteIntelligenceScore summary dict as a concise system prompt section."""
    if not route_score:
        return ""
    overall = route_score.get("overall", 0)
    label = route_score.get("overall_label", "")
    summary = route_score.get("summary", "")
    lines = [f"## Route Intelligence Score\nOverall: {overall}/10 ({label})"]
    worst = route_score.get("worst_category")
    if worst:
        lines.append(f"Weakest: {worst.get('name', '?')} ({worst.get('score', 0)}/10)")
    for cat in ("safety", "conditions", "services", "weather"):
        c = route_score.get(cat, {})
        if not c:
            continue
        score = c.get("score", 0)
        factors = c.get("factors", [])
        factor_str = "; ".join(factors[:3]) if factors else "No concerns"
        lines.append(f"{cat.capitalize()}: {score}/10 - {factor_str}")
    if summary:
        lines.append(f"Summary: {summary}")
    return "\n".join(lines)


def _format_flood_summary(flood: Dict[str, Any] | None) -> str:
    if not flood:
        return ""
    active = flood.get("active_gauges", 0)
    worst = flood.get("worst_severity", "minor")
    lines = [f"## Flood\n{active} active gauge(s) - worst: {worst}"]
    for g in flood.get("sample", [])[:3]:
        trend = g.get("trend", "")
        height = g.get("height_m")
        h_str = f" ({height:.1f}m)" if height is not None else ""
        lines.append(
            f"  • {g.get('name', '?')}: {g.get('severity', '?')}{h_str} {trend}"
        )
    return "\n".join(lines)


def _format_coverage_summary(coverage: Dict[str, Any] | None) -> str:
    if not coverage:
        return ""
    no_cov_km = coverage.get("total_no_coverage_km", 0)
    gap_count = coverage.get("total_gap_count", 0)
    best = coverage.get("best_carrier")
    lines = [f"## Mobile Coverage\n{no_cov_km}km with no coverage, {gap_count} gap(s)"]
    if best:
        lines.append(f"Best carrier overall: {best}")
    return "\n".join(lines)


def _format_wildlife_summary(wildlife: Dict[str, Any] | None) -> str:
    if not wildlife:
        return ""
    high = wildlife.get("high_risk_zones", 0)
    km_markers = wildlife.get("high_risk_km_markers", [])
    twilight = wildlife.get("has_twilight_risk", False)
    lines = [f"## Wildlife\n{high} high-risk zone(s)"]
    if km_markers:
        lines.append(f"Hotspots: {', '.join(km_markers[:5])}")
    if twilight:
        lines.append(
            "⚠ Twilight risk - kangaroos active at dawn/dusk along parts of route"
        )
    return "\n".join(lines)


def _format_weather_summary(weather: Dict[str, Any] | None) -> str:
    if not weather:
        return ""
    lines = ["## Weather Along Route"]
    temp = weather.get("temp_range_c")
    if temp:
        lines.append(f"Temperature: {temp}°C")
    rain = weather.get("rain_sections", 0)
    if rain:
        markers = weather.get("rain_km_markers", [])
        lines.append(f"Rain: {rain} section(s) - {', '.join(markers[:4])}")
    if weather.get("windy_sections", 0):
        lines.append(f"High wind: {weather['windy_sections']} section(s)")
    if weather.get("twilight_danger_sections", 0):
        lines.append(
            f"Twilight danger: {weather['twilight_danger_sections']} section(s)"
        )
    if weather.get("low_visibility_sections", 0):
        lines.append(f"Low visibility: {weather['low_visibility_sections']} section(s)")
    if weather.get("high_uv_sections", 0):
        lines.append(f"High UV (8+): {weather['high_uv_sections']} section(s)")
    if weather.get("extreme_heat"):
        lines.append("⚠ Extreme heat (38°C+) on route")
    if weather.get("near_freezing"):
        lines.append("⚠ Near-freezing temps (≤2°C) - watch for ice")
    return "\n".join(lines)


def _format_conditions(ctx: GuideContext) -> str:
    parts: List[str] = []
    ts = ctx.traffic_summary
    if ts and ts.get("total", 0) > 0:
        parts.append(f"Traffic: {ts['total']} events on/near route")
        for s in ts.get("sample", [])[:3]:
            sev = s.get("severity", "")
            parts.append(
                f"  • {s.get('type', 'event')}{' [' + sev.upper() + ']' if sev and sev != 'unknown' else ''}: {s.get('headline', '')[:100]}"
            )
    hs = ctx.hazards_summary
    if hs and hs.get("total", 0) > 0:
        parts.append(f"Hazards/Weather: {hs['total']} active warnings")
        for h in hs.get("sample", [])[:3]:
            parts.append(
                f"  • {h.get('kind', 'hazard')}: {h.get('headline', '')[:100]}"
            )
    return "\n".join(parts) if parts else "No active traffic or hazard alerts."


def _proximity_summary(
    places: List[WirePlace], user_lat: float | None, user_lng: float | None
) -> str:
    """For each category in NEARBY, compute the straight-line distance from
    the user GPS to the closest pre-loaded result. Surfaces gaps explicitly so
    the LLM can't pretend a 21km result is 'nearest' when the user is sitting
    in a town that isn't in the corridor cache."""
    if not places or user_lat is None or user_lng is None:
        return ""
    by_cat: Dict[str, float] = {}
    for p in places:
        d = math.sqrt((p.lat - user_lat) ** 2 + (p.lng - user_lng) ** 2) * 111.0
        cur = by_cat.get(p.category)
        if cur is None or d < cur:
            by_cat[p.category] = d
    if not by_cat:
        return ""
    lines = ["## NEARBY proximity check (straight-line km from user GPS)"]
    far_cats: List[str] = []
    for cat, d in sorted(by_cat.items(), key=lambda kv: kv[1]):
        marker = "OK" if d <= 5 else ("FAR" if d <= 15 else "VERY FAR")
        lines.append(f"  {cat}: closest cached = {d:.1f}km [{marker}]")
        if d > 5:
            far_cats.append(cat)
    if far_cats:
        lines.append(
            "  -> If user asked for nearest/next/closest "
            + "/".join(sorted(far_cats))
            + ", the NEARBY cache is stale relative to user position. Run "
            "places_search(center=user GPS, radius_m=5000-10000, categories=[...]) "
            "instead of naming a far-away result."
        )
    return "\n".join(lines)


def _format_places(places: List[WirePlace]) -> str:
    if not places:
        return "  (none pre-loaded - use tools to search)"

    by_cat: Dict[str, List[WirePlace]] = {}
    for p in places:
        by_cat.setdefault(p.category, []).append(p)

    priority = [
        "fuel",
        "ev_charging",
        "rest_area",
        "water",
        "mechanic",
        "hospital",
        "bakery",
        "cafe",
        "restaurant",
        "fast_food",
        "pub",
        "camp",
        "hotel",
        "motel",
        "viewpoint",
        "waterfall",
        "swimming_hole",
        "beach",
        "national_park",
        "hiking",
    ]
    cats = [c for c in priority if c in by_cat] + [
        c for c in by_cat if c not in priority
    ]

    lines: List[str] = []
    for cat in cats:
        lines.append(f"\n  [{cat.upper().replace('_', ' ')}]")
        for p in sorted(by_cat[cat], key=lambda p: (not p.ahead, p.dist_km or 9999)):
            parts = [f"    • {p.name} [id:{p.id} lat:{p.lat:.5f} lng:{p.lng:.5f}]"]
            if p.locality:
                parts.append(p.locality)
            if p.dist_km is not None:
                parts.append(f"{p.dist_km:.1f}km {'ahead' if p.ahead else 'behind'}")
            if p.hours:
                parts.append(f"open: {p.hours[:50]}")
            if p.phone:
                parts.append(f"ph: {p.phone}")
            if p.website:
                parts.append(f"web: {p.website}")
            # Free camping context for LLM
            if cat in ("camp", "rest_area"):
                if p.camp_type:
                    parts.append(f"type:{p.camp_type}")
                if p.free:
                    parts.append("FREE")
                elif p.price_per_night_aud is not None:
                    parts.append(f"${p.price_per_night_aud:.0f}/night")
                if p.overnight_allowed is not None:
                    if p.overnight_allowed is True:
                        note = "overnight:yes"
                        if p.overnight_max_hours:
                            note += f"({p.overnight_max_hours}hr max)"
                        parts.append(note)
                    elif p.overnight_allowed == "prohibited":
                        parts.append("overnight:NO")
                    else:
                        parts.append("overnight:check-signage")
                    if p.overnight_notes:
                        parts.append(p.overnight_notes)
                # Facility summary
                facilities = []
                if p.has_toilets:
                    facilities.append("toilets")
                if p.has_water:
                    facilities.append("water")
                if p.has_showers:
                    facilities.append("showers")
                if p.has_bbq:
                    facilities.append("BBQ")
                if facilities:
                    parts.append("has:" + ",".join(facilities))
                if p.pets_allowed:
                    parts.append("pets:ok" if p.pets_allowed is True else "pets:lead")
                if p.fires_allowed:
                    parts.append(
                        "fires:yes" if p.fires_allowed is True else "fires:seasonal"
                    )
                if p.max_stay_days:
                    parts.append(f"max:{p.max_stay_days}d")
            lines.append(" | ".join(parts))
    return "\n".join(lines)


def _format_stops(stops: List[Dict[str, Any]], visited: set, current_idx: int) -> str:
    lines = []
    for i, s in enumerate(stops):
        sid = s.get("id", f"p{i}")
        marker = "✅" if sid in visited else ("📍" if i == current_idx else "⬜")
        line = f"  {marker} [{i}] {s.get('name', '?')} ({s.get('type', 'poi')}) - {s.get('lat', 0):.4f},{s.get('lng', 0):.4f}"
        if s.get("arrive_at"):
            line += f" | arrive: {s['arrive_at']}"
        if s.get("depart_at"):
            line += f" | depart: {s['depart_at']}"
        if s.get("notes"):
            line += f" | {s['notes']}"
        lines.append(line)
    return "\n".join(lines) if lines else "  (no stops)"


# ══════════════════════════════════════════════════════════════
# LOCATION HINT
# Light nudge when user mentions a place not near their GPS.
# towns.json is used only for coordinates - no knowledge injected.
# ══════════════════════════════════════════════════════════════

_towns_cache: Dict[str, Tuple[float, float]] | None = None


def _get_towns() -> Dict[str, Tuple[float, float]]:
    global _towns_cache
    if _towns_cache is None:
        from pathlib import Path

        data_file = (
            Path(__file__).resolve().parent.parent / "data" / "guide" / "towns.json"
        )
        if data_file.exists():
            raw = json.loads(data_file.read_text(encoding="utf-8"))
            _towns_cache = {k: (v[0], v[1]) for k, v in raw.items()}
        else:
            _towns_cache = {}
    return _towns_cache


def _location_hint(
    thread: List[GuideMsg], user_lat: float | None, user_lng: float | None
) -> str:
    if not thread:
        return ""
    last_user = next(
        (m.content.lower() for m in reversed(thread) if m.role == "user"), ""
    )
    if not last_user:
        return ""

    towns = _get_towns()
    for town, (tlat, tlng) in sorted(towns.items(), key=lambda x: -len(x[0])):
        if town in last_user:
            if user_lat is not None and user_lng is not None:
                dist_km = (
                    math.sqrt((user_lat - tlat) ** 2 + (user_lng - tlng) ** 2) * 111.0
                )
                if dist_km < 30:
                    return ""
            return (
                f"User mentioned {town.title()} - their GPS is elsewhere. "
                f"If they're asking about {town.title()} specifically, search there or use your knowledge. "
                f"Don't second-guess their choice of destination."
            )
    return ""


# ══════════════════════════════════════════════════════════════
# SYSTEM PROMPT
# ══════════════════════════════════════════════════════════════


def _build_system_prompt(
    ctx: GuideContext,
    relevant_places: List[WirePlace],
    thread: List[GuideMsg] | None = None,
) -> str:
    total_km = (ctx.total_distance_m or 0) / 1000 if ctx.total_distance_m else None
    progress = ctx.progress
    visited = set(progress.visited_stop_ids if progress else [])
    current_idx = progress.current_stop_idx if progress else -1
    phase = _trip_phase(progress, total_km)

    # Position block
    if progress:
        stop_name = (
            ctx.stops[progress.current_stop_idx].get("name", "?")
            if 0 <= progress.current_stop_idx < len(ctx.stops)
            else "?"
        )
        pos = (
            f"({progress.user_lat:.5f}, {progress.user_lng:.5f}) ±{progress.user_accuracy_m:.0f}m"
            f"{', heading ' + str(int(progress.user_heading)) + '°' if progress.user_heading is not None else ''}"
            f", {_format_speed(progress.user_speed_mps)}\n"
            f'  Near stop [{progress.current_stop_idx}]: "{stop_name}"\n'
            f"  Progress: {progress.km_from_start:.0f}km done, {progress.km_remaining:.0f}km to next stop"
        )
        if total_km:
            pos += (
                f" ({min(100, int(progress.km_from_start / total_km * 100))}% of trip)"
            )
        time_str = ""
        if progress.local_time_iso:
            try:
                dt = datetime.fromisoformat(
                    progress.local_time_iso.replace("Z", "+00:00")
                )
                time_str = f"\nLocal time: {dt.strftime('%A %d %b %Y, %H:%M')} ({progress.timezone or 'local'})"
            except Exception:
                pass
    else:
        pos = "Location unavailable."
        time_str = ""

    # Driver state block
    driver_block = ""
    ds = ctx.driver_state
    if ds and isinstance(ds, dict):
        ds_parts: List[str] = []
        if ds.get("eta_iso"):
            ds_parts.append(f"ETA at destination: {ds['eta_iso']}")
        if ds.get("night_arrival"):
            ds_parts.append("⚠ Night arrival - will arrive after sunset")
        if ds.get("fatigue_level") and ds["fatigue_level"] != "none":
            ds_parts.append(f"Fatigue: {ds['fatigue_level']}")
            if ds.get("hours_since_rest") is not None:
                ds_parts.append(
                    f"Driving: {ds['hours_since_rest']:.1f}h since last rest"
                )
        if ds.get("fuel_pressure") is not None:
            fp = ds["fuel_pressure"]
            label = "low" if fp > 0.7 else ("mid" if fp > 0.4 else "good")
            ds_parts.append(f"Fuel: {label} ({fp:.0%} consumed)")
            if ds.get("km_to_next_fuel") is not None:
                ds_parts.append(f"Next fuel: {ds['km_to_next_fuel']:.0f}km")
        if ds.get("is_night"):
            ds_parts.append("Currently driving at night")
        if ds.get("temperature_c") is not None:
            ds_parts.append(f"Outside temp: {ds['temperature_c']:.0f}°C")
        if ds.get("speed_ratio") is not None and ds["speed_ratio"] < 0.8:
            ds_parts.append(
                f"Running slower than planned ({ds['speed_ratio']:.0%} of expected speed)"
            )
        if ds_parts:
            driver_block = "\n## Driver State\n" + "\n".join(ds_parts)

    # Tool availability
    tool_notes: List[str] = []
    if ctx.corridor_key:
        tool_notes.append(
            f"✅ corridor_key: {ctx.corridor_key} - places_corridor available"
        )
    else:
        tool_notes.append(
            "⚠️ No corridor_key - use places_search instead of places_corridor"
        )
    if ctx.geometry:
        tool_notes.append("✅ geometry available - places_suggest works")
    else:
        tool_notes.append("⚠️ No geometry - places_suggest unavailable")

    search_available = bool(settings.tavily_api_key or settings.google_cse_api_key)
    web_search_block = ""
    if search_available:
        web_search_block = (
            "\n  web_search - search the web for anything. Road conditions, closures, business hours, events, reviews, local tips."
            "\n    Use it aggressively - multiple searches per turn is fine. Better to search and know than to guess."
        )

    location_hint = _location_hint(
        thread or [],
        progress.user_lat if progress else None,
        progress.user_lng if progress else None,
    )
    route_score_block = _format_route_score(ctx.route_score_summary)
    flood_block = _format_flood_summary(ctx.flood_summary)
    coverage_block = _format_coverage_summary(ctx.coverage_summary)
    wildlife_block = _format_wildlife_summary(ctx.wildlife_summary)
    weather_block = _format_weather_summary(ctx.weather_summary)
    proximity_block = _proximity_summary(
        relevant_places,
        progress.user_lat if progress else None,
        progress.user_lng if progress else None,
    )

    prompt = f"""You are Roam Guide - the mate riding shotgun on an Aussie road trip. You know the highways, towns, lookouts, bakeries, gorge pools, and pubs of Australia. Talk like a local, not a brochure.

═══ HOW TO ACT (these rules override style) ═══

1. PROXIMITY DISCIPLINE. The NEARBY block is a CORRIDOR CACHE built when the trip was planned - it shows places along the route, not necessarily places near the user RIGHT NOW. If the user asks "nearest / next / closest / where's the X" and the closest NEARBY result in that category is more than 5km from the user GPS, you MUST emit places_search this turn with center=user GPS, radius_m=5000-10000, categories=[<the right one>]. DO NOT report a 20km cached result as "the nearest" - the user is sitting in a town that probably has the thing 1km away that just isn't in the corridor cache. The "NEARBY proximity check" block below tells you exactly when the cache is stale relative to user position.

2. NO BLUFFED PROMISES. Never write "let me check", "I'll look that up", "I'll search", "let me find", "let me grab their details", "checking", "give me a sec", "one moment" UNLESS you also emit the corresponding tool_call in the SAME response. If you can't run a tool this turn, just answer with what you know or say what you don't know directly. A promise without a tool_call is a lie.

3. OWN YOUR UNCERTAINTY. For current-info questions (opening hours, prices, payment methods, today's availability, road status, business is-still-open): either emit web_search this turn OR say plainly "Hours depend on the day, I'd ring ahead" / "Prices change, check their site." NEVER fabricate specific times like "open 9-5", prices, or phone numbers unless they come from a tool result.

4. NUMBERS YOU CITE MUST BE GROUNDED. Distances from the NEARBY block are fine to repeat. Drive times you can compute from the schedule. Anything else (phone numbers, addresses, hours, prices) only if it's in NEARBY or a fresh tool result. No making things up to sound helpful.

5. KNOWLEDGE IS WELCOME BUT BOUNDED. For well-known Australian places, history, geology, dreamtime stories, famous tracks - share what you know with depth and confidence. For obscure businesses, current events, or anything where being wrong matters - search or admit. Big picture: yes. Specific operational details: only if grounded.

═══ STYLE ═══

Warm but not repetitive. Vary openings - never start with "G'day". Don't recap earlier turns. Get to the new stuff. Be vivid and specific when you have grounded info; be brief and honest when you don't. Use the live overlay data - flag fuel gaps, fatigue, weather, wildlife, flood warnings without being asked. When a Route Intelligence Score is present, weave its warnings naturally ("no fuel for 287km past Coober Pedy - fill up before you leave town").

When asked "when should we leave?" / "will we make it by 3pm?" - do the maths from the schedule, current position, ETA.

═══ TRIP ═══
{ctx.label or "Unnamed"} | {ctx.profile or "drive"}{" | " + str(int(total_km)) + "km" if total_km else ""}{" | ~" + str(int((ctx.total_duration_s or 0) / 3600)) + "h drive" if ctx.total_duration_s else ""}
Phase: {phase}
Stops (arrive/depart times are the traveller's planned schedule):
{_format_stops(ctx.stops, visited, current_idx)}

═══ LIVE ═══
{pos}{time_str}
{_format_conditions(ctx)}
{f"Progress: {progress.km_from_start:.0f}km along route. Focus on places ahead." if progress and progress.km_from_start > 0 else ""}
{(chr(10) + route_score_block) if route_score_block else ""}{(chr(10) + weather_block) if weather_block else ""}{(chr(10) + flood_block) if flood_block else ""}{(chr(10) + wildlife_block) if wildlife_block else ""}{(chr(10) + coverage_block) if coverage_block else ""}{driver_block}
═══ NEARBY ═══
{_format_places(relevant_places)}
{(chr(10) + proximity_block) if proximity_block else ""}

═══ TOOLS ═══
To find places and produce action buttons, use these (they return structured place data with id/lat/lng):
{chr(10).join("  " + t for t in tool_notes)}
  places_search   {{"tool":"places_search","req":{{"center":{{"lat":-26.8,"lng":153.0}},"radius_m":8000,"categories":["fuel"],"limit":12}}}}
  places_corridor {{"tool":"places_corridor","req":{{"corridor_key":"auto","categories":["viewpoint","waterfall","swimming_hole"],"limit":30}}}}
  places_suggest  {{"tool":"places_suggest","req":{{"geometry":"auto","interval_km":50,"categories":["attraction"]}}}}

For current info (road conditions, events, hours, reviews):{web_search_block if web_search_block else chr(10) + "  web_search (unavailable - no API key configured)"}

IMPORTANT: To recommend places with action buttons, you MUST use places_search/places_corridor/places_suggest. Web search does NOT produce buttons. Use places tools first for finding stops, web_search for current conditions.

═══ OUTPUT ═══
Reply with JSON: {{"assistant":"text","done":bool,"actions":[...],"tool_calls":[...]}}

If your turn includes ANY of the promise phrases in rule 2, you MUST include the matching entry in tool_calls. Set done=false when you have tool_calls so the user sees your reply and then gets the follow-up turn with results.

Actions - for each place from tool results or nearby data, include buttons using its exact id/lat/lng:
  {{"type":"save","label":"Name","place_id":"id","place_name":"Name","lat":-27.5,"lng":153.0,"category":"cafe","description":"Brief vivid description."}}
  {{"type":"map","label":"Map · Name","place_id":"id","place_name":"Name","lat":-27.5,"lng":153.0,"category":"cafe"}}
  {{"type":"web","label":"Website","place_id":"id","place_name":"Name","url":"https://..."}}
  {{"type":"call","label":"Call","place_id":"id","place_name":"Name","tel":"0400..."}}

You can reply AND search simultaneously - set done=false with tool_calls to keep exploring while the user sees your message. After tools return you'll get another turn to share findings with action buttons.{(" " + location_hint) if location_hint else ""}"""

    return prompt


# ══════════════════════════════════════════════════════════════
# USER MESSAGE BUILDER
# ══════════════════════════════════════════════════════════════

_MAX_THREAD = 20
_MAX_TOOL_RESULTS = 4
_MAX_PLACES_PER_RESULT = 25


def _summarize_tool_result(tr: GuideToolResult) -> Dict[str, Any]:
    out: Dict[str, Any] = {"id": tr.id, "tool": tr.tool, "ok": tr.ok}
    if not tr.ok:
        out["error"] = str(tr.result.get("error", "?"))[:200]
        return out

    result = tr.result

    if tr.tool in ("places_search", "places_corridor"):
        raw_items = result.get("items", [])
        compact: List[Dict[str, Any]] = []
        for p in raw_items[:_MAX_PLACES_PER_RESULT]:
            entry: Dict[str, Any] = {
                "id": p.get("id", ""),
                "name": p.get("name", "?"),
                "cat": p.get("category", "?"),
                "lat": round(p.get("lat", 0), 4),
                "lng": round(p.get("lng", 0), 4),
            }
            extra = p.get("extra", {})
            if isinstance(extra, dict):
                tags = extra.get("tags", extra)
                suburb = (
                    tags.get("addr:suburb")
                    or tags.get("addr:city")
                    or tags.get("addr:town")
                )
                if suburb:
                    entry["suburb"] = str(suburb)[:40]
                if tags.get("opening_hours"):
                    entry["hours"] = str(tags["opening_hours"])[:60]
                phone = tags.get("phone") or tags.get("contact:phone")
                if phone:
                    entry["phone"] = str(phone)[:20]
                website = tags.get("website") or tags.get("contact:website")
                if website:
                    entry["website"] = str(website)[:100]
                fuel_types = [
                    k.replace("fuel:", "")
                    for k in tags
                    if k.startswith("fuel:") and tags[k] == "yes"
                ]
                if fuel_types:
                    entry["fuel_types"] = fuel_types[:5]
                if tags.get("socket:type2") or tags.get("socket:chademo"):
                    entry["ev_charging"] = True
                fee = tags.get("fee")
                if fee:
                    entry["fee"] = (
                        "free"
                        if fee == "no"
                        else ("paid" if fee == "yes" else str(fee)[:20])
                    )
                if tags.get("drinking_water"):
                    entry["water"] = tags["drinking_water"] == "yes"
                cuisine = tags.get("cuisine")
                if cuisine:
                    entry["cuisine"] = str(cuisine)[:40]
            compact.append(entry)
        out["total_found"] = len(raw_items)
        out["places"] = compact

    elif tr.tool == "places_suggest":
        clusters = result.get("clusters", [])
        out["clusters"] = [
            {
                "km_from_start": cl.get("km_from_start", 0),
                "total": len(cl.get("places", {}).get("items", [])),
                "highlights": [
                    {
                        "id": p.get("id", ""),
                        "name": p.get("name", "?"),
                        "cat": p.get("category", "?"),
                        "lat": round(p.get("lat", 0), 4),
                        "lng": round(p.get("lng", 0), 4),
                    }
                    for p in cl.get("places", {}).get("items", [])[:8]
                ],
            }
            for cl in clusters[:8]
        ]

    return out


def _build_user_message(req: GuideTurnRequest) -> str:
    parts: List[str] = []

    for m in req.thread[-_MAX_THREAD:]:
        role = "USER" if m.role == "user" else "GUIDE"
        parts.append(f"{role}: {m.content}")

    for tr in req.tool_results[-_MAX_TOOL_RESULTS:]:
        parts.append(
            f"\n[TOOL RESULT: {tr.tool}]\n{json.dumps(_summarize_tool_result(tr), separators=(',', ':'))}"
        )

    if req.preferred_categories:
        parts.append(
            f"\n[Category filter active: {', '.join(req.preferred_categories)}]"
        )

    return "\n".join(parts)


# ══════════════════════════════════════════════════════════════
# OUTPUT NORMALIZATION
# ══════════════════════════════════════════════════════════════


def _normalize_model_output(raw: Any) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        return {"assistant": "", "tool_calls": [], "actions": [], "done": False}
    raw.setdefault("assistant", "")
    # Strip a stray trailing markdown code fence the model sometimes appends
    # inside the assistant string itself (e.g. "...details for you.\n```json").
    asst = raw.get("assistant")
    if isinstance(asst, str):
        raw["assistant"] = re.sub(r"\s*```(?:json)?\s*$", "", asst).strip()
    raw.setdefault("tool_calls", [])
    raw.setdefault("actions", [])
    raw.setdefault("done", False)
    if not isinstance(raw.get("tool_calls"), list):
        raw["tool_calls"] = []
    if not isinstance(raw.get("actions"), list):
        raw["actions"] = []
    for i, tc in enumerate(raw["tool_calls"]):
        if isinstance(tc, dict):
            tc.setdefault("id", f"tc_{i}_{uuid.uuid4().hex[:8]}")
    return raw


# ══════════════════════════════════════════════════════════════
# TOOL REQUEST REPAIR & VALIDATION
# ══════════════════════════════════════════════════════════════

# Valid PlaceCategory values (must mirror contracts.PlaceCategory). The model
# sometimes invents categories like "seafood" or "fish_and_chips" that aren't in
# the enum, which fails validation and drops the whole search -> a broken
# promise. We normalise synonyms to valid values and drop the rest.
_VALID_PLACE_CATEGORIES = {
    "fuel",
    "ev_charging",
    "rest_area",
    "toilet",
    "water",
    "water_fill",
    "dump_point",
    "shower",
    "mechanic",
    "hospital",
    "pharmacy",
    "emergency_phone",
    "grocery",
    "town",
    "atm",
    "laundromat",
    "bakery",
    "cafe",
    "restaurant",
    "fast_food",
    "pub",
    "bar",
    "camp",
    "hotel",
    "motel",
    "hostel",
    "viewpoint",
    "waterfall",
    "swimming_hole",
    "beach",
    "national_park",
    "hiking",
    "picnic",
    "hot_spring",
    "cave",
    "fishing",
    "surf",
    "playground",
    "pool",
    "zoo",
    "theme_park",
    "dog_park",
    "golf",
    "cinema",
    "visitor_info",
    "museum",
    "gallery",
    "heritage",
    "winery",
    "brewery",
    "attraction",
    "market",
    "park",
    "library",
    "showground",
    "address",
    "place",
    "region",
}

_CATEGORY_SYNONYMS = {
    "fish_and_chips": "fast_food",
    "fish": "fast_food",
    "chips": "fast_food",
    "seafood": "restaurant",
    "takeaway": "fast_food",
    "take_away": "fast_food",
    "fastfood": "fast_food",
    "diner": "restaurant",
    "dining": "restaurant",
    "eatery": "restaurant",
    "food": "restaurant",
    "coffee": "cafe",
    "espresso": "cafe",
    "coffee_shop": "cafe",
    "petrol": "fuel",
    "gas": "fuel",
    "gas_station": "fuel",
    "servo": "fuel",
    "service_station": "fuel",
    "petrol_station": "fuel",
    "charger": "ev_charging",
    "ev": "ev_charging",
    "ev_charger": "ev_charging",
    "charging_station": "ev_charging",
    "supermarket": "grocery",
    "groceries": "grocery",
    "lookout": "viewpoint",
    "scenic": "viewpoint",
    "vista": "viewpoint",
    "swim": "swimming_hole",
    "swimming": "swimming_hole",
    "waterhole": "swimming_hole",
    "water_hole": "swimming_hole",
    "accommodation": "motel",
    "lodging": "motel",
    "stay": "motel",
    "campsite": "camp",
    "campground": "camp",
    "campground_": "camp",
    "caravan_park": "camp",
    "tourist": "attraction",
    "sightseeing": "attraction",
    "things_to_do": "attraction",
    "walk": "hiking",
    "walking": "hiking",
    "trail": "hiking",
    "track": "hiking",
    "bushwalk": "hiking",
    "chemist": "pharmacy",
    "drugstore": "pharmacy",
    "garage": "mechanic",
    "repair": "mechanic",
    "tyre": "mechanic",
    "restroom": "toilet",
    "bathroom": "toilet",
    "loo": "toilet",
    "dunny": "toilet",
    "drinking_water": "water",
    "potable_water": "water",
    "groceries_store": "grocery",
    "convenience": "grocery",
    "pub_food": "pub",
    "brewpub": "brewery",
    "cellar_door": "winery",
    "playground_": "playground",
}


def _normalize_categories(cats: Any) -> List[str]:
    """Lowercase, map synonyms, drop unknowns, dedupe - so a hallucinated
    category never fails the whole places_search."""
    if not isinstance(cats, list):
        return []
    out: List[str] = []
    for c in cats:
        if not c:
            continue
        key = str(c).lower().strip().replace(" ", "_").replace("-", "_")
        mapped = _CATEGORY_SYNONYMS.get(key, key)
        if mapped in _VALID_PLACE_CATEGORIES and mapped not in out:
            out.append(mapped)
    return out


def _repair_req(tool: str, req: Dict[str, Any], ctx: GuideContext) -> Dict[str, Any]:
    req = dict(req)
    if tool == "places_corridor":
        if not req.get("corridor_key") and ctx.corridor_key:
            req["corridor_key"] = ctx.corridor_key
        req.setdefault("limit", 30)
    elif tool == "places_suggest":
        if not req.get("geometry") and ctx.geometry:
            req["geometry"] = ctx.geometry
        req.setdefault("interval_km", 50)
        req.setdefault("radius_m", 10000)
        req.setdefault("limit_per_sample", 10)
    elif tool == "places_search":
        if "lat" in req and "lng" in req and "center" not in req:
            req["center"] = {"lat": req.pop("lat"), "lng": req.pop("lng")}
        # Salvage a centerless search by defaulting to the user's GPS. The model
        # often emits places_search for "X around here" without a center;
        # dropping it on validation is what produced the "promised to look, came
        # back with nothing" bug. Centering on the traveller is the right default.
        if "center" not in req and ctx.progress is not None:
            req["center"] = {
                "lat": ctx.progress.user_lat,
                "lng": ctx.progress.user_lng,
            }
        req.setdefault("limit", 20)
        if "center" in req:
            req.setdefault("radius_m", 15000)
    if "categories" in req:
        norm_cats = _normalize_categories(req.get("categories"))
        if norm_cats:
            req["categories"] = norm_cats
        else:
            # All categories were unknown/hallucinated - drop the filter and
            # search everything nearby rather than failing validation.
            req.pop("categories", None)
    return req


def _validate_tool_req(tool: str, req: Dict[str, Any]) -> Tuple[bool, str]:
    try:
        if tool == "places_search":
            PlacesRequest(**req)
        elif tool == "places_corridor":
            CorridorPlacesRequest(**req)
        elif tool == "places_suggest":
            PlacesSuggestRequest(**req)
        else:
            return False, f"Unknown tool: {tool}"
        return True, ""
    except (ValidationError, Exception) as e:
        return False, str(e)[:200]


# ══════════════════════════════════════════════════════════════
# WEB SEARCH RESULT FORMATTER
# ══════════════════════════════════════════════════════════════


def _format_search_results(results: List[Dict[str, str]]) -> str:
    if not results:
        return "(No results found.)"
    lines = []
    for i, r in enumerate(results, 1):
        lines.append(
            f"[{i}] {r.get('title', '')}\n{r.get('content', '')[:500]}\nSource: {r.get('url', '')}"
        )
    return "\n\n".join(lines)


def _extract_first_json_object(s: str) -> str | None:
    """Return the substring containing the first balanced {...} JSON object,
    or None if no balanced object is present. Respects double-quoted strings
    and backslash escapes so braces inside string values don't break the
    depth counter. Used by the guide LLM parser to recover when the model
    wraps its JSON in trailing prose."""
    start = s.find("{")
    if start == -1:
        return None
    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(s)):
        ch = s[i]
        if in_string:
            if escape:
                escape = False
                continue
            if ch == "\\":
                escape = True
                continue
            if ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return s[start : i + 1]
    return None


# ══════════════════════════════════════════════════════════════
# FORCING-FUNCTION GUARDS
# Deterministic post-processing overriding the LLM's recurring failure modes:
# (A) bluffing a far corridor-cache result as "nearest" instead of searching
# around the user's GPS; (B) promising to look something up with no tool_call;
# (C) answering current-info (hours/prices) from stale training data.
# ══════════════════════════════════════════════════════════════

_PROMISE_PHRASES = [
    "let me check",
    "i'll look",
    "i will look",
    "i'll search",
    "i will search",
    "looking that up",
    "look that up",
    "look it up",
    "give me a sec",
    "give me a moment",
    "one moment",
    "i'll find out",
    "i'll dig",
    "let me dig",
    "let me look",
    "let me find",
    "let me find out",
    "hang on while i",
    "i'll check",
    "i will check",
    "let me grab",
    "let me pull up",
    "let me pull",
    "let me get you",
    "let me get their",
    "i'll grab",
    "i'll pull up",
    "let me sort",
]

_PROXIMITY_TRIGGERS = [
    "nearest",
    "closest",
    "next servo",
    "next fuel",
    "next petrol",
    "next toilet",
    "next rest",
    "where's the",
    "wheres the",
    "where is the",
    "near me",
    "nearby",
    "close by",
    "around here",
    "find me",
    "find the",
    "how far to the next",
]

# Essentials only - categories where "nearest" is a literal navigational need and
# the corridor cache genuinely misses the user's current town. Food /
# recommendations excluded: there the model's knowledge beats a geometric nearest.
_PROX_CAT_KEYWORDS: List[Tuple[List[str], str, str]] = [
    (
        [
            "servo",
            "petrol",
            "fuel",
            "diesel",
            "gas station",
            "service station",
            "fill up",
            "refuel",
            "unleaded",
        ],
        "fuel",
        "servo",
    ),
    (
        ["ev charg", "charger", "charging station", "fast charg"],
        "ev_charging",
        "EV charger",
    ),
    (
        ["toilet", "dunny", "loo", "restroom", "public toilet", "amenities"],
        "toilet",
        "toilet",
    ),
    (
        ["drinking water", "water tap", "fill water", "refill water", "potable water"],
        "water",
        "water tap",
    ),
    (["rest area", "rest stop"], "rest_area", "rest area"),
    (
        ["mechanic", "tyre", "tire shop", "breakdown", "auto repair"],
        "mechanic",
        "mechanic",
    ),
    (["hospital", "emergency room", "urgent care"], "hospital", "hospital"),
    (["pharmacy", "chemist"], "pharmacy", "chemist"),
    (["atm", "cash out"], "atm", "ATM"),
    (
        [
            "supermarket",
            "woolies",
            "woolworths",
            "coles",
            "iga",
            "aldi",
            "groceries",
            "grocery",
        ],
        "grocery",
        "supermarket",
    ),
]

_PROX_INJECT_RADIUS_M = 10000
_PROX_STALE_KM = 5.0

# Current-info markers - time-sensitive answers that must come from a fresh web
# search, not training data. Narrow so "what time should we leave" does NOT match.
_CURRENT_INFO_MARKERS = [
    "open today",
    "open now",
    "opening hours",
    "are they open",
    "is it open",
    "is the cafe open",
    "is the cafe still",
    "still open",
    "closed today",
    "what time do they",
    "what time does it",
    "what time do they open",
    "what time do they close",
    "hours today",
    "trading hours",
    "take card",
    "take cards",
    "accept card",
    "accepts card",
    "eftpos",
    "cash only",
    "take cash",
    "do they take",
    "how much is",
    "how much does",
    "price of",
    "entry fee",
    "cost to get in",
    "do i need to book",
    "need a booking",
    "booked out",
]


def _last_user_msg(thread: List[GuideMsg] | None) -> str:
    if not thread:
        return ""
    return next((m.content for m in reversed(thread) if m.role == "user"), "")


def _is_proximity_query(text: str) -> bool:
    t = text.lower()
    return any(trig in t for trig in _PROXIMITY_TRIGGERS)


def _match_proximity_category(text: str) -> Tuple[str, str] | None:
    t = text.lower()
    for kws, cat, label in _PROX_CAT_KEYWORDS:
        if any(kw in t for kw in kws):
            return cat, label
    return None


def _nearest_cached_km(
    places: List[WirePlace], cat: str, user_lat: float, user_lng: float
) -> float | None:
    best: float | None = None
    for p in places:
        if p.category != cat:
            continue
        d = math.sqrt((p.lat - user_lat) ** 2 + (p.lng - user_lng) ** 2) * 111.0
        if best is None or d < best:
            best = d
    return best


def _is_current_info_query(text: str) -> bool:
    t = text.lower()
    return any(m in t for m in _CURRENT_INFO_MARKERS)


def _has_places_tool_call(norm: Dict[str, Any]) -> bool:
    return any(
        isinstance(tc, dict) and str(tc.get("tool", "")).startswith("places_")
        for tc in norm.get("tool_calls", []) or []
    )


def _has_web_search_call(norm: Dict[str, Any]) -> bool:
    return any(
        isinstance(tc, dict) and tc.get("tool") == "web_search"
        for tc in norm.get("tool_calls", []) or []
    )


def _has_promise_without_action(norm: Dict[str, Any]) -> bool:
    text = (norm.get("assistant") or "").lower()
    if not any(p in text for p in _PROMISE_PHRASES):
        return False
    return len(norm.get("tool_calls", []) or []) == 0


# Forced structured-output tool for the Anthropic native path. Forcing this tool
# guarantees valid JSON matching the guide turn schema - no fence stripping or
# brace-matching salvage needed.
_ANTHROPIC_EMIT_TOOL = {
    "name": "emit_guide_turn",
    "description": (
        "Emit the guide's reply for this turn. ALWAYS call this exactly once. "
        "Put the spoken reply in `assistant`, set `done`, and include any "
        "place-finding tool_calls and UI action buttons."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "assistant": {
                "type": "string",
                "description": "The spoken reply to the traveller.",
            },
            "done": {
                "type": "boolean",
                "description": "False if tool_calls is non-empty, else true.",
            },
            "tool_calls": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "tool": {
                            "type": "string",
                            "enum": [
                                "places_search",
                                "places_corridor",
                                "places_suggest",
                                "web_search",
                            ],
                        },
                        "req": {"type": "object"},
                    },
                    "required": ["tool", "req"],
                },
            },
            "actions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "type": {
                            "type": "string",
                            "enum": ["save", "map", "web", "call"],
                        },
                        "label": {"type": "string"},
                        "place_id": {"type": "string"},
                        "place_name": {"type": "string"},
                        "url": {"type": "string"},
                        "tel": {"type": "string"},
                        "lat": {"type": "number"},
                        "lng": {"type": "number"},
                        "category": {"type": "string"},
                        "description": {"type": "string"},
                    },
                    "required": ["type", "label"],
                },
            },
        },
        "required": ["assistant", "done"],
    },
}


# ══════════════════════════════════════════════════════════════
# SERVICE
# ══════════════════════════════════════════════════════════════


class GuideService:
    def __init__(self) -> None:
        # Resolve provider, falling back to whichever has a key set.
        self._provider, self._api_key, self._model, self._base = (
            self._resolve_provider()
        )
        self._timeout = httpx.Timeout(
            connect=10.0,
            read=float(settings.guide_timeout_s),
            write=10.0,
            pool=10.0,
        )
        logger.info(
            "GuideService provider=%s model=%s base=%s key=%s",
            self._provider,
            self._model,
            self._base,
            "set" if self._api_key else "MISSING",
        )

    @staticmethod
    def _resolve_provider() -> Tuple[str, str, str, str]:
        """Pick (provider, key, model, base_url) from settings, honouring
        guide_provider but falling back to any provider that has a key so a
        missing key never silently breaks the guide."""
        table = {
            "anthropic": (
                settings.anthropic_api_key,
                settings.anthropic_model,
                settings.anthropic_base_url,
            ),
            "openai": (
                settings.openai_api_key,
                settings.openai_model,
                settings.openai_base_url,
            ),
            "gemini": (
                settings.gemini_api_key,
                settings.gemini_model,
                settings.gemini_base_url,
            ),
            "deepseek": (
                settings.deepseek_api_key,
                settings.deepseek_model,
                settings.deepseek_base_url,
            ),
        }
        chosen = (settings.guide_provider or "anthropic").lower()
        if chosen in table and table[chosen][0]:
            key, model, base = table[chosen]
            return chosen, key, model, base.rstrip("/")
        for name in ("anthropic", "openai", "gemini", "deepseek"):
            key, model, base = table[name]
            if key:
                logger.warning(
                    "Guide provider '%s' has no key; falling back to '%s'", chosen, name
                )
                return name, key, model, base.rstrip("/")
        key, model, base = table.get(chosen, table["anthropic"])
        return chosen, key, model, base.rstrip("/")

    async def _call_llm(self, sys_prompt: str, user_msg: str) -> Dict[str, Any]:
        if self._provider == "anthropic":
            try:
                return await self._call_anthropic(sys_prompt, user_msg)
            except Exception as e:
                # Resilience: if Anthropic blips, fall back to DeepSeek so the
                # guide never hard-fails for a traveller (or an Apple reviewer).
                if settings.deepseek_api_key:
                    logger.warning(
                        "Guide: Anthropic call failed (%s); falling back to DeepSeek",
                        str(e)[:200],
                    )
                    return await self._call_openai_compatible(
                        sys_prompt,
                        user_msg,
                        model=settings.deepseek_model,
                        base=settings.deepseek_base_url.rstrip("/"),
                        api_key=settings.deepseek_api_key,
                        provider="deepseek-fallback",
                    )
                raise
        return await self._call_openai_compatible(sys_prompt, user_msg)

    async def _call_anthropic(self, sys_prompt: str, user_msg: str) -> Dict[str, Any]:
        """Native Anthropic Messages API with a forced structured-output tool.
        Returns the validated tool input directly as the norm dict. The system
        prompt is cache-controlled to cut cost on the large static prefix."""
        body = {
            "model": self._model,
            "max_tokens": settings.guide_max_output_tokens,
            "temperature": settings.guide_temperature,
            "system": [
                {
                    "type": "text",
                    "text": sys_prompt,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            "messages": [{"role": "user", "content": user_msg}],
            "tools": [_ANTHROPIC_EMIT_TOOL],
            "tool_choice": {"type": "tool", "name": "emit_guide_turn"},
        }
        url = f"{self._base}/messages"
        headers = {
            "x-api-key": self._api_key,
            "anthropic-version": settings.anthropic_version,
            "content-type": "application/json",
        }
        logger.info(
            "Guide LLM call (anthropic %s): sys=%d chars, user=%d chars",
            self._model,
            len(sys_prompt),
            len(user_msg),
        )
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            r = await client.post(url, headers=headers, json=body)
            if r.status_code >= 400:
                raise RuntimeError(f"Anthropic {r.status_code}: {r.text[:500]}")
            data = r.json()

        usage = data.get("usage", {})
        if usage:
            logger.info(
                "Guide LLM usage (anthropic): in=%s out=%s cache_read=%s cache_write=%s",
                usage.get("input_tokens", "?"),
                usage.get("output_tokens", "?"),
                usage.get("cache_read_input_tokens", "?"),
                usage.get("cache_creation_input_tokens", "?"),
            )

        for block in data.get("content", []):
            if (
                block.get("type") == "tool_use"
                and block.get("name") == "emit_guide_turn"
            ):
                return block.get("input", {}) or {}
        text_parts = [
            b.get("text", "")
            for b in data.get("content", [])
            if b.get("type") == "text"
        ]
        joined = " ".join(t for t in text_parts if t).strip()
        logger.warning(
            "Guide LLM (anthropic): no tool_use block, text fallback. raw=%s",
            json.dumps(data)[:400],
        )
        return {"assistant": joined, "actions": [], "tool_calls": [], "done": True}

    async def _call_openai_compatible(
        self,
        sys_prompt: str,
        user_msg: str,
        model: str | None = None,
        base: str | None = None,
        api_key: str | None = None,
        provider: str | None = None,
    ) -> Dict[str, Any]:
        # Credentials default to the resolved provider, but can be overridden
        # (used by the Anthropic->DeepSeek resilience fallback).
        model = model or self._model
        base = base or self._base
        api_key = api_key or self._api_key
        provider = provider or self._provider
        json_instruction = (
            "\n\nRespond ONLY with a valid JSON object: "
            '{"assistant": string, "done": boolean, "actions": array, "tool_calls": array}'
        )
        body = {
            "model": model,
            "messages": [
                {"role": "system", "content": sys_prompt + json_instruction},
                {"role": "user", "content": user_msg},
            ],
            "response_format": {"type": "json_object"},
            "temperature": settings.guide_temperature,
            "max_tokens": settings.guide_max_output_tokens,
        }
        url = f"{base}/chat/completions"
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

        # Log payload size for debugging context window issues
        sys_chars = len(body["messages"][0]["content"])  # type: ignore[index]
        usr_chars = len(body["messages"][1]["content"])  # type: ignore[index]
        est_tokens = (sys_chars + usr_chars) // 3  # rough char-to-token estimate
        logger.info(
            "Guide LLM call: sys=%d chars, user=%d chars, ~%d tokens input",
            sys_chars,
            usr_chars,
            est_tokens,
        )

        async with httpx.AsyncClient(timeout=self._timeout) as client:
            r = await client.post(url, headers=headers, json=body)
            if r.status_code >= 400:
                raise RuntimeError(f"{provider} {r.status_code}: {r.text[:500]}")
            data = r.json()

        # Log token usage from API response
        usage = data.get("usage", {})
        if usage:
            logger.info(
                "Guide LLM usage: prompt=%s completion=%s total=%s",
                usage.get("prompt_tokens", "?"),
                usage.get("completion_tokens", "?"),
                usage.get("total_tokens", "?"),
            )

        try:
            out_text: str = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as e:
            raise RuntimeError(
                f"Guide LLM: unexpected response: {e}. Raw: {json.dumps(data)[:500]}"
            )

        if not out_text:
            raise RuntimeError("Guide LLM: empty response")

        try:
            return json.loads(out_text)
        except Exception:
            pass
        # Strip ``` / ```json code fences and retry
        stripped = re.sub(r"^```(?:json)?\s*|\s*```$", "", out_text.strip())
        try:
            return json.loads(stripped)
        except Exception:
            pass
        # LLM sometimes returns valid JSON followed by trailing prose, or
        # wraps the JSON in unexpected leading text. Extract the first
        # balanced {...} block via brace-matching that respects quoted
        # strings + escapes (Tate 2026-05-28: "stop now" pill triggered
        # invalid JSON at char 355).
        extracted = _extract_first_json_object(stripped)
        if extracted is not None:
            try:
                return json.loads(extracted)
            except Exception:
                pass
        # Last resort: degrade gracefully rather than crash the chat. The
        # user sees the LLM's raw reply as the assistant message instead of
        # a red "Guide error" banner. Actions/tool_calls are empty so the
        # turn is non-destructive (no places added, no web search fired).
        logger.warning(
            "Guide LLM: unparseable JSON, falling back to plain-text reply. text=%r",
            out_text[:400],
        )
        return {
            "assistant": out_text.strip(),
            "actions": [],
            "tool_calls": [],
            "done": True,
        }

    async def turn(self, req: GuideTurnRequest) -> GuideTurnResponse:
        if not self._api_key:
            raise RuntimeError(f"Guide LLM key missing for provider '{self._provider}'")

        sys_prompt = _build_system_prompt(req.context, req.relevant_places, req.thread)
        user_msg = _build_user_message(req)

        # Step 1: Call LLM
        raw = await self._call_llm(sys_prompt, user_msg)
        norm = _normalize_model_output(raw)
        raw_tcs = norm.get("tool_calls", [])
        tc_summary = [
            {
                "tool": tc.get("tool"),
                "req_keys": list(tc.get("req", {}).keys())
                if isinstance(tc.get("req"), dict)
                else str(tc.get("req"))[:50],
            }
            for tc in raw_tcs
            if isinstance(tc, dict)
        ]
        logger.info(
            "Guide step 0: text=%d chars, actions=%d, tool_calls=%d (%s), done=%s",
            len(norm.get("assistant", "")),
            len(norm.get("actions", [])),
            len(raw_tcs),
            tc_summary,
            norm.get("done"),
        )

        # ── Guard A: proximity injection ────────────────────────────────
        # User asked for nearest/closest essential and the closest cached result
        # in that category is >5km from GPS (or absent) -> corridor cache is stale
        # relative to position. Inject a GPS-centred places_search and replace any
        # bluffed prose with an honest "finding now" (the Kawana/Landsborough bug).
        progress = req.context.progress
        last_user = _last_user_msg(req.thread)
        if (
            progress is not None
            and not _has_places_tool_call(norm)
            and _is_proximity_query(last_user)
        ):
            m = _match_proximity_category(last_user)
            if m is not None:
                cat, label = m
                nearest = _nearest_cached_km(
                    req.relevant_places, cat, progress.user_lat, progress.user_lng
                )
                if nearest is None or nearest > _PROX_STALE_KM:
                    norm.setdefault("tool_calls", [])
                    if not isinstance(norm["tool_calls"], list):
                        norm["tool_calls"] = []
                    norm["tool_calls"].append(
                        {
                            "id": f"tc_prox_{uuid.uuid4().hex[:8]}",
                            "tool": "places_search",
                            "req": {
                                "center": {
                                    "lat": progress.user_lat,
                                    "lng": progress.user_lng,
                                },
                                "radius_m": _PROX_INJECT_RADIUS_M,
                                "categories": [cat],
                                "limit": 12,
                            },
                        }
                    )
                    norm["assistant"] = (
                        f"On it - finding the closest {label} to you right now."
                    )
                    norm["actions"] = []
                    norm["done"] = False
                    logger.info(
                        "Guide guard A: proximity injection cat=%s nearest_cached=%s",
                        cat,
                        f"{nearest:.1f}km" if nearest is not None else "none",
                    )

        # ── Guard B: promise-without-delivery retry ─────────────────────
        # Model promised to look something up but emitted no tool_call -> force
        # ONE corrective retry (emit the tool, or answer honestly with no promise).
        if _has_promise_without_action(norm):
            logger.info("Guide guard B: promise-without-action, retrying once")
            correction = (
                "\n\n[SYSTEM CORRECTION] Your previous reply promised to look "
                "something up ('let me check' / 'I'll search' / 'let me find' / "
                "similar) but included NO tool_call, so nothing was actually looked "
                "up. That is a broken promise. Produce a NEW reply that EITHER (a) "
                "includes the correct tool_call - web_search for hours/prices/"
                "current-status, places_search for finding nearby places - OR (b) "
                "answers honestly from what you know, or plainly admits what you "
                "can't confirm, with NO promise wording. Never write 'let me check' "
                "or 'I'll look that up' unless tool_calls is non-empty."
            )
            raw_retry = await self._call_llm(sys_prompt, user_msg + correction)
            norm = _normalize_model_output(raw_retry)
            logger.info(
                "Guide guard B: post-retry text=%d chars, tool_calls=%d, done=%s",
                len(norm.get("assistant", "")),
                len(norm.get("tool_calls", []) or []),
                norm.get("done"),
            )

            # Deterministic floor: if the retry STILL bluffs a promise with no
            # tool_call, inject a GPS-centred places_search so a "find me X"
            # query never comes back empty (Tate's core complaint). Categories
            # inferred inline from the query, defaulting to a broad nearby mix.
            if _has_promise_without_action(norm) and progress is not None:
                lu = last_user.lower()
                if any(w in lu for w in ("coffee", "espresso", "latte", "flat white")):
                    cats = ["cafe", "bakery"]
                elif any(w in lu for w in ("camp", "campsite", "campground")):
                    cats = ["camp", "rest_area"]
                elif any(
                    w in lu
                    for w in (
                        "stay",
                        "sleep",
                        "accommodation",
                        "motel",
                        "hotel",
                        "cabin",
                    )
                ):
                    cats = ["hotel", "motel", "camp"]
                elif any(
                    w in lu for w in ("swim", "beach", "waterhole", "water hole", "dip")
                ):
                    cats = ["swimming_hole", "beach", "waterfall"]
                elif any(w in lu for w in ("pub", "beer", "drink", "bar")):
                    cats = ["pub", "bar"]
                elif any(
                    w in lu
                    for w in (
                        "see ",
                        "to do",
                        "lookout",
                        "view",
                        "walk",
                        "hike",
                        "attraction",
                        "sight",
                    )
                ):
                    cats = ["viewpoint", "attraction", "national_park", "hiking"]
                elif any(
                    w in lu
                    for w in (
                        "eat",
                        "food",
                        "feed",
                        "lunch",
                        "dinner",
                        "breakfast",
                        "hungry",
                        "meal",
                        "fish",
                        "chips",
                        "bakery",
                        "pie",
                        "burger",
                    )
                ):
                    cats = ["cafe", "restaurant", "fast_food", "bakery", "pub"]
                else:
                    _m2 = _match_proximity_category(last_user)
                    cats = (
                        [_m2[0]]
                        if _m2
                        else ["cafe", "restaurant", "fuel", "toilet", "viewpoint"]
                    )
                norm.setdefault("tool_calls", [])
                if not isinstance(norm["tool_calls"], list):
                    norm["tool_calls"] = []
                norm["tool_calls"].append(
                    {
                        "id": f"tc_b2_{uuid.uuid4().hex[:8]}",
                        "tool": "places_search",
                        "req": {
                            "center": {
                                "lat": progress.user_lat,
                                "lng": progress.user_lng,
                            },
                            "radius_m": 10000,
                            "categories": cats,
                            "limit": 12,
                        },
                    }
                )
                norm["assistant"] = "On it - pulling up what's nearby for you now."
                norm["actions"] = []
                norm["done"] = False
                logger.info("Guide guard B-floor: injected places_search cats=%s", cats)

        # ── Guard C: force web_search on current-info queries ───────────
        # Hours/prices/is-it-open/payment must come from a fresh search, never
        # stale training data. Inject a web_search the machinery runs inline.
        _search_available = bool(settings.tavily_api_key or settings.google_cse_api_key)
        if (
            _search_available
            and _is_current_info_query(last_user)
            and not _has_web_search_call(norm)
        ):
            norm.setdefault("tool_calls", [])
            if not isinstance(norm["tool_calls"], list):
                norm["tool_calls"] = []
            norm["tool_calls"].append(
                {
                    "id": f"tc_web_{uuid.uuid4().hex[:8]}",
                    "tool": "web_search",
                    "req": {"query": last_user.strip()[:200]},
                }
            )
            logger.info(
                "Guide guard C: current-info query, injected web_search query=%r",
                last_user.strip()[:80],
            )

        # Step 2: Handle web searches (max 1 internal round-trip)
        web_searched = False
        web_sources: List[GuideSource] = []
        tool_calls = norm.get("tool_calls") or []
        if tool_calls:
            web_searches = [
                tc
                for tc in tool_calls
                if isinstance(tc, dict) and tc.get("tool") == "web_search"
            ]
            non_web = [
                tc
                for tc in tool_calls
                if isinstance(tc, dict) and tc.get("tool") != "web_search"
            ]

            # Execute web searches (max 2) inline
            if web_searches:
                for ws in web_searches[:2]:
                    query = ws.get("req", {}).get("query", "")
                    if query:
                        results = await web_search(query)
                        web_searched = True
                        for rr in results[:5]:
                            u = rr.get("url", "")
                            if not u or any(s.url == u for s in web_sources):
                                continue
                            web_sources.append(
                                GuideSource(title=rr.get("title", "")[:120], url=u)
                            )
                        user_msg += f"\n\n=== WEB SEARCH: {query} ===\n{_format_search_results(results)}"

                if non_web:
                    # Have both web + places tools: web is done, pass places through
                    norm["tool_calls"] = non_web
                else:
                    # Only had web searches - make ONE more LLM call with results
                    raw2 = await self._call_llm(sys_prompt, user_msg)
                    norm = _normalize_model_output(raw2)
                    raw_tcs2 = norm.get("tool_calls", [])
                    tc_summary2 = [
                        {
                            "tool": tc.get("tool"),
                            "req_keys": list(tc.get("req", {}).keys())
                            if isinstance(tc.get("req"), dict)
                            else str(tc.get("req"))[:50],
                        }
                        for tc in raw_tcs2
                        if isinstance(tc, dict)
                    ]
                    logger.info(
                        "Guide step 1 (post-websearch): text=%d chars, actions=%d, tool_calls=%d (%s), done=%s",
                        len(norm.get("assistant", "")),
                        len(norm.get("actions", [])),
                        len(raw_tcs2),
                        tc_summary2,
                        norm.get("done"),
                    )
                    # Strip any further web_search calls - we only do one round
                    norm["tool_calls"] = [
                        tc
                        for tc in (norm.get("tool_calls") or [])
                        if isinstance(tc, dict) and tc.get("tool") != "web_search"
                    ]

        tool_calls = norm.get("tool_calls") or []
        validated_calls: List[Dict[str, Any]] = []

        for tc in tool_calls[:4]:
            if not isinstance(tc, dict):
                logger.warning("Guide: skipping non-dict tool_call: %s", type(tc))
                continue
            tool = tc.get("tool")
            req_obj = tc.get("req") if isinstance(tc.get("req"), dict) else {}
            tc_id = tc.get("id") or f"tc_{uuid.uuid4().hex[:8]}"

            logger.info(
                "Guide: raw tool_call: tool=%s req_keys=%s",
                tool,
                list(req_obj.keys()) if isinstance(req_obj, dict) else "N/A",
            )

            if tool not in ("places_search", "places_corridor", "places_suggest"):
                logger.warning(
                    "Guide: dropping unknown tool: %s (raw tc: %s)",
                    tool,
                    json.dumps(tc)[:300],
                )
            else:
                fixed_req = _repair_req(tool, req_obj, req.context)  # type: ignore[arg-type]
                ok, err = _validate_tool_req(tool, fixed_req)
                if ok:
                    validated_calls.append(
                        {"id": tc_id, "tool": tool, "req": fixed_req}
                    )
                else:
                    logger.warning(
                        "Guide: tool %s failed validation: %s (req: %s)",
                        tool,
                        err,
                        json.dumps(fixed_req)[:300],
                    )

        response_calls = [
            GuideToolCall(id=vc["id"], tool=vc["tool"], req=vc["req"])
            for vc in validated_calls
        ]

        response_actions: List[GuideAction] = []
        for a in norm.get("actions", []):
            try:
                response_actions.append(
                    GuideAction(
                        type=a.get("type", "web"),
                        label=a.get("label", ""),
                        place_id=a.get("place_id"),
                        place_name=a.get("place_name"),
                        url=a.get("url"),
                        tel=a.get("tel"),
                        lat=a.get("lat"),
                        lng=a.get("lng"),
                        category=a.get("category"),
                        description=a.get("description"),
                    )
                )
            except Exception:
                continue

        resp = GuideTurnResponse(
            assistant=norm.get("assistant", ""),
            tool_calls=response_calls,
            actions=response_actions,
            done=norm.get("done", not bool(response_calls)),
            web_searched=web_searched,
            sources=web_sources,
        )
        logger.info(
            "Guide response: text=%d chars, actions=%d, tool_calls=%d, done=%s",
            len(resp.assistant),
            len(resp.actions),
            len(resp.tool_calls),
            resp.done,
        )
        return resp
