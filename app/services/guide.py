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

logger = logging.getLogger(__name__)

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
)
from app.services.guide_search import web_search


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
        lines.append(f"Weakest: {worst.get('name','?')} ({worst.get('score',0)}/10)")
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
        lines.append(f"  • {g.get('name','?')}: {g.get('severity','?')}{h_str} {trend}")
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
        lines.append("⚠ Twilight risk - kangaroos active at dawn/dusk along parts of route")
    return "\n".join(lines)


def _format_weather_summary(weather: Dict[str, Any] | None) -> str:
    if not weather:
        return ""
    lines = [f"## Weather Along Route"]
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
        lines.append(f"Twilight danger: {weather['twilight_danger_sections']} section(s)")
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
            parts.append(f"  • {s.get('type','event')}{' ['+sev.upper()+']' if sev and sev!='unknown' else ''}: {s.get('headline','')[:100]}")
    hs = ctx.hazards_summary
    if hs and hs.get("total", 0) > 0:
        parts.append(f"Hazards/Weather: {hs['total']} active warnings")
        for h in hs.get("sample", [])[:3]:
            parts.append(f"  • {h.get('kind','hazard')}: {h.get('headline','')[:100]}")
    return "\n".join(parts) if parts else "No active traffic or hazard alerts."


def _format_places(places: List[WirePlace]) -> str:
    if not places:
        return "  (none pre-loaded - use tools to search)"

    by_cat: Dict[str, List[WirePlace]] = {}
    for p in places:
        by_cat.setdefault(p.category, []).append(p)

    priority = [
        "fuel", "ev_charging", "rest_area", "water", "mechanic", "hospital",
        "bakery", "cafe", "restaurant", "fast_food", "pub",
        "camp", "hotel", "motel",
        "viewpoint", "waterfall", "swimming_hole", "beach", "national_park", "hiking",
    ]
    cats = [c for c in priority if c in by_cat] + [c for c in by_cat if c not in priority]

    lines: List[str] = []
    for cat in cats:
        lines.append(f"\n  [{cat.upper().replace('_',' ')}]")
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
                    parts.append("fires:yes" if p.fires_allowed is True else "fires:seasonal")
                if p.max_stay_days:
                    parts.append(f"max:{p.max_stay_days}d")
            lines.append(" | ".join(parts))
    return "\n".join(lines)


def _format_stops(stops: List[Dict[str, Any]], visited: set, current_idx: int) -> str:
    lines = []
    for i, s in enumerate(stops):
        sid = s.get("id", f"p{i}")
        marker = "✅" if sid in visited else ("📍" if i == current_idx else "⬜")
        line = f"  {marker} [{i}] {s.get('name','?')} ({s.get('type','poi')}) - {s.get('lat',0):.4f},{s.get('lng',0):.4f}"
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
        data_file = Path(__file__).resolve().parent.parent / "data" / "guide" / "towns.json"
        if data_file.exists():
            raw = json.loads(data_file.read_text(encoding="utf-8"))
            _towns_cache = {k: (v[0], v[1]) for k, v in raw.items()}
        else:
            _towns_cache = {}
    return _towns_cache


def _location_hint(thread: List[GuideMsg], user_lat: float | None, user_lng: float | None) -> str:
    if not thread:
        return ""
    last_user = next((m.content.lower() for m in reversed(thread) if m.role == "user"), "")
    if not last_user:
        return ""

    towns = _get_towns()
    for town, (tlat, tlng) in sorted(towns.items(), key=lambda x: -len(x[0])):
        if town in last_user:
            if user_lat is not None and user_lng is not None:
                dist_km = math.sqrt((user_lat - tlat)**2 + (user_lng - tlng)**2) * 111.0
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
        stop_name = ctx.stops[progress.current_stop_idx].get("name", "?") if 0 <= progress.current_stop_idx < len(ctx.stops) else "?"
        pos = (
            f"({progress.user_lat:.5f}, {progress.user_lng:.5f}) ±{progress.user_accuracy_m:.0f}m"
            f"{', heading '+str(int(progress.user_heading))+'°' if progress.user_heading is not None else ''}"
            f", {_format_speed(progress.user_speed_mps)}\n"
            f"  Near stop [{progress.current_stop_idx}]: \"{stop_name}\"\n"
            f"  Progress: {progress.km_from_start:.0f}km done, {progress.km_remaining:.0f}km to next stop"
        )
        if total_km:
            pos += f" ({min(100,int(progress.km_from_start/total_km*100))}% of trip)"
        time_str = ""
        if progress.local_time_iso:
            try:
                dt = datetime.fromisoformat(progress.local_time_iso.replace("Z", "+00:00"))
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
                ds_parts.append(f"Driving: {ds['hours_since_rest']:.1f}h since last rest")
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
            ds_parts.append(f"Running slower than planned ({ds['speed_ratio']:.0%} of expected speed)")
        if ds_parts:
            driver_block = "\n## Driver State\n" + "\n".join(ds_parts)

    # Tool availability
    tool_notes: List[str] = []
    if ctx.corridor_key:
        tool_notes.append(f"✅ corridor_key: {ctx.corridor_key} - places_corridor available")
    else:
        tool_notes.append("⚠️ No corridor_key - use places_search instead of places_corridor")
    if ctx.geometry:
        tool_notes.append(f"✅ geometry available - places_suggest works")
    else:
        tool_notes.append("⚠️ No geometry - places_suggest unavailable")

    search_available = bool(settings.tavily_api_key or settings.google_cse_api_key)
    web_search_block = ""
    if search_available:
        web_search_block = (
            "\n  web_search - search the web for anything. Road conditions, closures, business hours, events, reviews, local tips."
            "\n    Use it aggressively - multiple searches per turn is fine. Better to search and know than to guess."
        )

    location_hint = _location_hint(thread or [], progress.user_lat if progress else None, progress.user_lng if progress else None)
    route_score_block = _format_route_score(ctx.route_score_summary)
    flood_block = _format_flood_summary(ctx.flood_summary)
    coverage_block = _format_coverage_summary(ctx.coverage_summary)
    wildlife_block = _format_wildlife_summary(ctx.wildlife_summary)
    weather_block = _format_weather_summary(ctx.weather_summary)

    prompt = f"""You are Roam Guide - the knowledgeable mate riding shotgun on an Aussie road trip. You know every highway, bakery, gorge pool, and pub from Broome to Byron.

Trust your knowledge of Australia. Share stories, tips, warnings, and opinions freely - you're a travel companion, not a search engine. Use your tools to get specifics and current info.

Be proactive: flag fuel gaps, fatigue risks, amazing nearby spots, weather, wildlife danger zones, flood warnings, and mobile coverage dead zones without being asked. Be vivid and specific. You have live overlay data - use it.

You have the full trip schedule with planned arrive/depart times for each stop. Use this to help plan the day - suggest when to leave, warn if they're running behind schedule, recommend lunch/coffee stops that fit the timeline, flag if they'll arrive at a campsite after dark, and help them make the most of their time at each stop. If a user asks "when should we leave?" or "will we make it by 3pm?", you can do the maths using their schedule, current position, and ETA.

Style: warm but not repetitive. Don't start every message with "G'day" - vary your openings naturally. Don't repeat info from earlier in the conversation. Get straight to the new stuff.

When you have a Route Intelligence Score, reference its specific warnings naturally in your advice - don't just list them, weave them into your response (e.g. "I see there's no fuel for 287km past Coober Pedy - you'll want a full tank before you leave town.").

═══ TRIP ═══
{ctx.label or 'Unnamed'} | {ctx.profile or 'drive'}{' | '+str(int(total_km))+'km' if total_km else ''}{' | ~'+str(int((ctx.total_duration_s or 0)/3600))+'h drive' if ctx.total_duration_s else ''}
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

═══ TOOLS ═══
To find places and produce action buttons, use these (they return structured place data with id/lat/lng):
{chr(10).join('  '+t for t in tool_notes)}
  places_search   {{"tool":"places_search","req":{{"center":{{"lat":-26.8,"lng":153.0}},"radius_m":15000,"categories":["cafe","bakery"],"limit":20}}}}
  places_corridor {{"tool":"places_corridor","req":{{"corridor_key":"auto","categories":["viewpoint","waterfall","swimming_hole"],"limit":30}}}}
  places_suggest  {{"tool":"places_suggest","req":{{"geometry":"auto","interval_km":50,"categories":["attraction"]}}}}

For current info (road conditions, events, hours, reviews):{web_search_block if web_search_block else chr(10)+"  web_search (unavailable - no API key configured)"}

IMPORTANT: To recommend places with action buttons, you MUST use places_search/places_corridor/places_suggest. Web search does NOT produce buttons. Use places tools first for finding stops, web_search for current conditions.

═══ OUTPUT ═══
Reply with JSON: {{"assistant":"text","done":bool,"actions":[...],"tool_calls":[...]}}

Actions - for each place from tool results or nearby data, include buttons using its exact id/lat/lng:
  {{"type":"save","label":"Name","place_id":"id","place_name":"Name","lat":-27.5,"lng":153.0,"category":"cafe","description":"Brief vivid description."}}
  {{"type":"map","label":"Map · Name","place_id":"id","place_name":"Name","lat":-27.5,"lng":153.0,"category":"cafe"}}
  {{"type":"web","label":"Website","place_id":"id","place_name":"Name","url":"https://..."}}
  {{"type":"call","label":"Call","place_id":"id","place_name":"Name","tel":"0400..."}}

You can reply AND search simultaneously - set done=false with tool_calls to keep exploring while the user sees your message. After tools return you'll get another turn to share findings with action buttons.{(' '+location_hint) if location_hint else ''}"""

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
                suburb = tags.get("addr:suburb") or tags.get("addr:city") or tags.get("addr:town")
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
                fuel_types = [k.replace("fuel:", "") for k in tags if k.startswith("fuel:") and tags[k] == "yes"]
                if fuel_types:
                    entry["fuel_types"] = fuel_types[:5]
                if tags.get("socket:type2") or tags.get("socket:chademo"):
                    entry["ev_charging"] = True
                fee = tags.get("fee")
                if fee:
                    entry["fee"] = "free" if fee == "no" else ("paid" if fee == "yes" else str(fee)[:20])
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
        parts.append(f"\n[TOOL RESULT: {tr.tool}]\n{json.dumps(_summarize_tool_result(tr), separators=(',',':'))}")

    if req.preferred_categories:
        parts.append(f"\n[Category filter active: {', '.join(req.preferred_categories)}]")

    return "\n".join(parts)


# ══════════════════════════════════════════════════════════════
# OUTPUT NORMALIZATION
# ══════════════════════════════════════════════════════════════

def _normalize_model_output(raw: Any) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        return {"assistant": "", "tool_calls": [], "actions": [], "done": False}
    raw.setdefault("assistant", "")
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
        req.setdefault("limit", 20)
        if "center" in req:
            req.setdefault("radius_m", 15000)
    if "categories" in req and isinstance(req["categories"], list):
        req["categories"] = [str(c).lower().strip() for c in req["categories"] if c]
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
        lines.append(f"[{i}] {r.get('title','')}\n{r.get('content','')[:500]}\nSource: {r.get('url','')}")
    return "\n\n".join(lines)


# ══════════════════════════════════════════════════════════════
# SERVICE
# ══════════════════════════════════════════════════════════════

class GuideService:
    def __init__(self) -> None:
        self._api_key = settings.deepseek_api_key
        self._model = settings.deepseek_model
        self._base = settings.deepseek_base_url.rstrip("/")
        self._timeout = httpx.Timeout(
            connect=10.0,
            read=float(settings.guide_timeout_s),
            write=10.0,
            pool=10.0,
        )

    async def _call_llm(self, sys_prompt: str, user_msg: str) -> Dict[str, Any]:
        json_instruction = (
            "\n\nRespond ONLY with a valid JSON object: "
            '{"assistant": string, "done": boolean, "actions": array, "tool_calls": array}'
        )
        body = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": sys_prompt + json_instruction},
                {"role": "user", "content": user_msg},
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0.7,
            "max_tokens": 4000,
        }
        url = f"{self._base}/chat/completions"
        headers = {"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"}

        # Log payload size for debugging context window issues
        sys_chars = len(body["messages"][0]["content"])
        usr_chars = len(body["messages"][1]["content"])
        est_tokens = (sys_chars + usr_chars) // 3  # rough char-to-token estimate
        logger.info("Guide LLM call: sys=%d chars, user=%d chars, ~%d tokens input", sys_chars, usr_chars, est_tokens)

        async with httpx.AsyncClient(timeout=self._timeout) as client:
            r = await client.post(url, headers=headers, json=body)
            if r.status_code >= 400:
                raise RuntimeError(f"DeepSeek {r.status_code}: {r.text[:500]}")
            data = r.json()

        # Log token usage from API response
        usage = data.get("usage", {})
        if usage:
            logger.info("Guide LLM usage: prompt=%s completion=%s total=%s",
                        usage.get("prompt_tokens", "?"), usage.get("completion_tokens", "?"), usage.get("total_tokens", "?"))

        try:
            out_text: str = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as e:
            raise RuntimeError(f"Guide LLM: unexpected response: {e}. Raw: {json.dumps(data)[:500]}")

        if not out_text:
            raise RuntimeError("Guide LLM: empty response")

        try:
            return json.loads(out_text)
        except Exception as e:
            stripped = re.sub(r"^```(?:json)?\s*|\s*```$", "", out_text.strip())
            try:
                return json.loads(stripped)
            except Exception:
                raise RuntimeError(f"Guide LLM: invalid JSON: {e}. text={out_text[:400]}")

    async def turn(self, req: GuideTurnRequest) -> GuideTurnResponse:
        if not self._api_key:
            raise RuntimeError("DEEPSEEK_API_KEY missing")

        sys_prompt = _build_system_prompt(req.context, req.relevant_places, req.thread)
        user_msg = _build_user_message(req)

        # Step 1: Call LLM
        raw = await self._call_llm(sys_prompt, user_msg)
        norm = _normalize_model_output(raw)
        raw_tcs = norm.get("tool_calls", [])
        tc_summary = [{"tool": tc.get("tool"), "req_keys": list(tc.get("req", {}).keys()) if isinstance(tc.get("req"), dict) else str(tc.get("req"))[:50]} for tc in raw_tcs if isinstance(tc, dict)]
        logger.info("Guide step 0: text=%d chars, actions=%d, tool_calls=%d (%s), done=%s",
                    len(norm.get("assistant", "")),
                    len(norm.get("actions", [])), len(raw_tcs), tc_summary,
                    norm.get("done"))

        # Step 2: Handle web searches (max 1 internal round-trip)
        tool_calls = norm.get("tool_calls") or []
        if tool_calls:
            web_searches = [tc for tc in tool_calls if isinstance(tc, dict) and tc.get("tool") == "web_search"]
            non_web = [tc for tc in tool_calls if isinstance(tc, dict) and tc.get("tool") != "web_search"]

            # Execute web searches (max 2) inline
            if web_searches:
                for ws in web_searches[:2]:
                    query = ws.get("req", {}).get("query", "")
                    if query:
                        results = await web_search(query)
                        user_msg += f"\n\n=== WEB SEARCH: {query} ===\n{_format_search_results(results)}"

                if non_web:
                    # Have both web + places tools: web is done, pass places through
                    norm["tool_calls"] = non_web
                else:
                    # Only had web searches - make ONE more LLM call with results
                    raw2 = await self._call_llm(sys_prompt, user_msg)
                    norm = _normalize_model_output(raw2)
                    raw_tcs2 = norm.get("tool_calls", [])
                    tc_summary2 = [{"tool": tc.get("tool"), "req_keys": list(tc.get("req", {}).keys()) if isinstance(tc.get("req"), dict) else str(tc.get("req"))[:50]} for tc in raw_tcs2 if isinstance(tc, dict)]
                    logger.info("Guide step 1 (post-websearch): text=%d chars, actions=%d, tool_calls=%d (%s), done=%s",
                                len(norm.get("assistant", "")),
                                len(norm.get("actions", [])), len(raw_tcs2), tc_summary2,
                                norm.get("done"))
                    # Strip any further web_search calls - we only do one round
                    norm["tool_calls"] = [tc for tc in (norm.get("tool_calls") or [])
                                          if isinstance(tc, dict) and tc.get("tool") != "web_search"]

        tool_calls = norm.get("tool_calls") or []
        validated_calls: List[Dict[str, Any]] = []

        for tc in tool_calls[:4]:
            if not isinstance(tc, dict):
                logger.warning("Guide: skipping non-dict tool_call: %s", type(tc))
                continue
            tool = tc.get("tool")
            req_obj = tc.get("req") if isinstance(tc.get("req"), dict) else {}
            tc_id = tc.get("id") or f"tc_{uuid.uuid4().hex[:8]}"

            logger.info("Guide: raw tool_call: tool=%s req_keys=%s", tool, list(req_obj.keys()) if isinstance(req_obj, dict) else "N/A")

            if tool not in ("places_search", "places_corridor", "places_suggest"):
                logger.warning("Guide: dropping unknown tool: %s (raw tc: %s)", tool, json.dumps(tc)[:300])
            else:
                fixed_req = _repair_req(tool, req_obj, req.context)
                ok, err = _validate_tool_req(tool, fixed_req)
                if ok:
                    validated_calls.append({"id": tc_id, "tool": tool, "req": fixed_req})
                else:
                    logger.warning("Guide: tool %s failed validation: %s (req: %s)", tool, err, json.dumps(fixed_req)[:300])

        response_calls = [GuideToolCall(id=vc["id"], tool=vc["tool"], req=vc["req"]) for vc in validated_calls]

        response_actions: List[GuideAction] = []
        for a in norm.get("actions", []):
            try:
                response_actions.append(GuideAction(
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
                ))
            except Exception:
                continue

        resp = GuideTurnResponse(
            assistant=norm.get("assistant", ""),
            tool_calls=response_calls,
            actions=response_actions,
            done=norm.get("done", not bool(response_calls)),
        )
        logger.info("Guide response: text=%d chars, actions=%d, tool_calls=%d, done=%s",
                     len(resp.assistant), len(resp.actions), len(resp.tool_calls), resp.done)
        return resp
