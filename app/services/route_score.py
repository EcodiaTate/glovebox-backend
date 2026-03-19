# app/services/route_score.py
"""
Route Intelligence Score - composite scoring from all overlays.

Weights:
  Safety     35%
  Conditions 25%
  Services   25%
  Weather    15%

Scoring is CONSERVATIVE: over-warn rather than under-warn.
If an overlay is unavailable, it is excluded from scoring with a data_warning.
"""
from __future__ import annotations

import logging
from typing import List, Optional

from app.core.contracts import (
    AirQualityOverlay,
    BushfireOverlay,
    CoverageOverlay,
    FloodOverlay,
    FuelOverlay,
    HazardOverlay,
    RestAreaOverlay,
    RouteIntelligenceScore,
    RouteScoreCategory,
    TrafficOverlay,
    WeatherOverlay,
    WildlifeOverlay,
)

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────
# Label helpers
# ──────────────────────────────────────────────────────────────

def _score_label(score: float) -> str:
    if score >= 8.0:
        return "Excellent"
    if score >= 6.0:
        return "Good"
    if score >= 4.0:
        return "Fair"
    if score >= 2.0:
        return "Poor"
    return "Dangerous"


def _score_advice(score: float) -> str:
    if score >= 8.0:
        return "Great conditions for travel"
    if score >= 6.0:
        return "Generally favourable, minor concerns noted"
    if score >= 4.0:
        return "Check conditions carefully before departing"
    if score >= 2.0:
        return "Significant concerns - plan carefully, carry supplies"
    return "Serious safety risks - consider delaying travel"


# ──────────────────────────────────────────────────────────────
# Safety score (0-10)
# ──────────────────────────────────────────────────────────────

def _compute_safety(
    traffic: Optional[TrafficOverlay],
    hazards: Optional[HazardOverlay],
    flood: Optional[FloodOverlay],
    coverage: Optional[CoverageOverlay],
    wildlife: Optional[WildlifeOverlay],
    bushfire: Optional[BushfireOverlay] = None,
    weather: Optional[WeatherOverlay] = None,
) -> RouteScoreCategory:
    score = 10.0
    factors: List[str] = []

    # Traffic: tiered deductions by severity and type.
    # Philosophy: a "major" event means significant disruption (detour, delays),
    # not necessarily life-threatening. Multiple events of the same type exhibit
    # diminishing marginal impact - the 5th roadwork is less consequential than the 1st.
    if traffic:
        # Major closures/flooding: -2 for first, -1 for second, capped at -3 total
        major_blocks = [
            e for e in traffic.items
            if e.severity == "major" and e.type in ("closure", "flooding")
        ]
        if major_blocks:
            n = len(major_blocks)
            deduct = min(2 + max(0, n - 1), 3)  # 2 for 1st, +1 more for 2+, max -3
            score -= deduct
            factors.append(f"{n} major traffic event(s) blocking route")

        # Major/moderate roadworks & incidents: -0.5 each (max -2)
        roadworks_incidents = [
            e for e in traffic.items
            if e.type in ("roadworks", "incident")
            and e.severity in ("major", "moderate")
        ]
        if roadworks_incidents:
            deduct = min(0.5 * len(roadworks_incidents), 2)
            score -= deduct
            factors.append(f"{len(roadworks_incidents)} roadworks/incident(s) on route")

        # Minor/info roadworks: -0.25 each (max -1)
        minor_roadworks = [
            e for e in traffic.items
            if e.type == "roadworks" and e.severity in ("minor", "info")
        ]
        if minor_roadworks:
            deduct = min(0.25 * len(minor_roadworks), 1)
            score -= deduct
            factors.append(f"{len(minor_roadworks)} minor roadworks on route")

    # Hazards: -1.5 for first, diminishing for additional, max -3
    if hazards:
        high_hazards = [e for e in hazards.items if e.severity == "high"]
        if high_hazards:
            n = len(high_hazards)
            deduct = min(1.5 + 0.5 * max(0, n - 1), 3)
            score -= deduct
            factors.append(f"{n} high-severity hazard(s) on route")

    # Flood: -2 for first major rising gauge, -1 for additional, max -4
    if flood:
        major_rising = [
            g for g in flood.gauges
            if g.severity == "major" and g.trend == "rising"
        ]
        if major_rising:
            n = len(major_rising)
            deduct = min(2 + max(0, n - 1), 4)
            score -= deduct
            factors.append(f"{n} major rising flood gauge(s) on route")

    # Coverage: -2 for total no-coverage gap > 100km, -1 for > 50km
    if coverage:
        all_gaps = [g for g in coverage.gaps if g.carrier == "all"]
        max_gap = max((g.gap_km for g in all_gaps), default=0.0)
        if max_gap > 100:
            score -= 2
            factors.append(f"No mobile coverage for {max_gap:.0f}km")
        elif max_gap > 50:
            score -= 1
            factors.append(f"No mobile coverage for {max_gap:.0f}km")

    # Wildlife: -1 per high-risk twilight zone
    if wildlife:
        high_twilight = [z for z in wildlife.zones if z.risk_level == "high" and z.is_twilight_risk]
        if high_twilight:
            deduct = min(len(high_twilight), 3)
            score -= deduct
            factors.append(f"{len(high_twilight)} high-risk wildlife zone(s) at twilight hours")

    # Bushfire: emergency warnings are critical safety risks
    if bushfire:
        emergency_fires = [
            f for f in bushfire.incidents
            if f.alert_level and f.alert_level.lower() in ("emergency warning", "emergency")
            and (f.distance_from_route_km is None or f.distance_from_route_km < 50)
        ]
        if emergency_fires:
            deduct = min(4 * len(emergency_fires), 8)
            score -= deduct
            factors.append(f"{len(emergency_fires)} bushfire emergency warning(s) near route")
        watch_act = [
            f for f in bushfire.incidents
            if f.alert_level and "watch" in f.alert_level.lower()
            and (f.distance_from_route_km is None or f.distance_from_route_km < 50)
        ]
        if watch_act:
            deduct = min(2 * len(watch_act), 4)
            score -= deduct
            factors.append(f"{len(watch_act)} bushfire watch-and-act(s) near route")

    # ── Cross-overlay synergy: combined risks worse than sum of parts ──

    # Flood + no coverage = stranded with no way to call help
    has_flood_risk = flood and any(
        g.severity in ("major", "moderate") and g.trend == "rising"
        for g in flood.gauges
    )
    has_coverage_gap = coverage and any(
        g.gap_km > 50 for g in coverage.gaps if g.carrier == "all"
    )
    if has_flood_risk and has_coverage_gap:
        score -= 1.5
        factors.append("Flood risk in area with no mobile coverage - rescue access limited")

    # Hazard + no coverage = can't report or call for help
    has_high_hazard = hazards and any(e.severity == "high" for e in hazards.items)
    if has_high_hazard and has_coverage_gap:
        score -= 1.0
        factors.append("High-severity hazard in coverage dead zone")

    # Bushfire + no coverage = critical - can't receive emergency alerts
    has_bushfire_risk = bushfire and any(
        f.alert_level and f.alert_level.lower() in ("emergency warning", "emergency", "watch and act")
        and (f.distance_from_route_km is None or f.distance_from_route_km < 50)
        for f in bushfire.incidents
    )
    if has_bushfire_risk and has_coverage_gap:
        score -= 2.0
        factors.append("Active bushfire near route with no mobile coverage - cannot receive alerts")

    # Wildlife twilight + poor visibility (weather) = compounded animal strike risk
    has_wildlife_twilight = wildlife and any(
        z.risk_level == "high" and z.is_twilight_risk for z in wildlife.zones
    )
    has_poor_visibility = weather and any(
        p.visibility_m is not None and p.visibility_m < 1000 for p in weather.points
    )
    if has_wildlife_twilight and has_poor_visibility:
        score -= 1.0
        factors.append("High wildlife risk at twilight with poor visibility")

    score = max(0.0, score)
    return RouteScoreCategory(score=round(score, 1), label=_score_label(score), factors=factors)


# ──────────────────────────────────────────────────────────────
# Conditions score (0-10)
# ──────────────────────────────────────────────────────────────

def _compute_conditions(
    weather: Optional[WeatherOverlay],
    flood: Optional[FloodOverlay],
    traffic: Optional[TrafficOverlay] = None,
    air_quality: Optional[AirQualityOverlay] = None,
) -> RouteScoreCategory:
    score = 10.0
    factors: List[str] = []

    # Traffic: roadworks and closures degrade road conditions.
    # Keep deductions modest here - safety score already accounts for the main impact.
    if traffic:
        closures = [e for e in traffic.items if e.type == "closure"]
        roadworks = [e for e in traffic.items if e.type == "roadworks"]
        if closures:
            deduct = min(len(closures), 2)  # -1 per closure, max -2
            score -= deduct
            factors.append(f"{len(closures)} road closure(s) affecting conditions")
        if roadworks:
            deduct = min(0.5 * len(roadworks), 1.5)  # -0.5 per roadwork, max -1.5
            score -= deduct
            factors.append(f"{len(roadworks)} active roadworks on route")

    if weather and weather.points:
        pts = weather.points

        # -3 if any point has precip_probability > 90%
        extreme_rain_pts = [p for p in pts if p.precipitation_probability_pct > 90]
        if extreme_rain_pts:
            score -= 3
            factors.append(f"Extreme rain probability (>90%) at {len(extreme_rain_pts)} point(s)")

        # -2 for heavy rain: precip_prob > 70% for more than 3 consecutive points
        if not extreme_rain_pts:
            heavy_consecutive = 0
            max_consecutive = 0
            for p in pts:
                if p.precipitation_probability_pct > 70:
                    heavy_consecutive += 1
                    max_consecutive = max(max_consecutive, heavy_consecutive)
                else:
                    heavy_consecutive = 0
            if max_consecutive > 3:
                score -= 2
                factors.append(f"Heavy rain (>70%) across {max_consecutive} consecutive route points")

        # -2 for extreme wind gusts > 60km/h at any point
        extreme_wind_pts = [p for p in pts if (p.wind_gust_kmh or 0) > 60]
        if extreme_wind_pts:
            score -= 2
            max_gust = max(p.wind_gust_kmh or 0 for p in extreme_wind_pts)
            factors.append(f"Extreme wind gusts up to {max_gust:.0f}km/h")

        # -1 for strong wind > 40km/h at any point (only if not already hit extreme)
        elif any((p.wind_gust_kmh or 0) > 40 for p in pts):
            score -= 1
            max_gust = max(p.wind_gust_kmh or 0 for p in pts)
            factors.append(f"Strong wind gusts up to {max_gust:.0f}km/h")

        # -1 for extreme heat >42°C or cold <2°C
        extreme_temp_pts = [p for p in pts if p.temperature_c > 42 or p.temperature_c < 2]
        if extreme_temp_pts:
            temps = [p.temperature_c for p in extreme_temp_pts]
            max_t = max(temps)
            min_t = min(temps)
            desc = f"{max_t:.0f}°C" if max_t > 42 else f"{min_t:.0f}°C"
            score -= 1
            factors.append(f"Extreme temperature ({desc}) at {len(extreme_temp_pts)} point(s)")

        # -1 for poor visibility < 1000m
        vis_pts = [p for p in pts if p.visibility_m is not None and p.visibility_m < 1000]
        if vis_pts:
            min_vis = min(p.visibility_m for p in vis_pts)
            score -= 1
            factors.append(f"Poor visibility ({min_vis:.0f}m) at {len(vis_pts)} point(s)")

    # Flood: -1 for any moderate+rising gauge
    if flood:
        mod_rising = [
            g for g in flood.gauges
            if g.severity == "moderate" and g.trend == "rising"
        ]
        if mod_rising:
            score -= 1
            factors.append(f"{len(mod_rising)} moderate rising flood gauge(s)")

    # Air quality: deduct for poor/very poor AQI
    if air_quality:
        if air_quality.overall_aqi >= 5:
            score -= 3
            factors.append(f"Very poor air quality (AQI {air_quality.overall_aqi}) - avoid outdoor activities")
        elif air_quality.overall_aqi >= 4:
            score -= 2
            factors.append(f"Poor air quality (AQI {air_quality.overall_aqi}) - reduce outdoor exertion")
        elif air_quality.overall_aqi >= 3:
            score -= 1
            factors.append(f"Moderate air quality (AQI {air_quality.overall_aqi}) - sensitive groups affected")

    # ── Cross-overlay synergy: combined conditions ──

    # Extreme heat + poor AQI = severe health risk
    has_extreme_heat = weather and any(p.temperature_c > 38 for p in weather.points)
    has_poor_aqi = air_quality and air_quality.overall_aqi >= 4
    if has_extreme_heat and has_poor_aqi:
        score -= 1.5
        factors.append("Extreme heat combined with poor air quality - serious health risk")

    # Heavy rain + flood gauges rising = road likely impassable
    has_heavy_rain = weather and any(
        p.precipitation_probability_pct > 70 and p.precipitation_mm > 5
        for p in weather.points
    )
    has_rising_flood = flood and any(
        g.trend == "rising" and g.severity in ("moderate", "major")
        for g in flood.gauges
    )
    if has_heavy_rain and has_rising_flood:
        score -= 1.0
        factors.append("Heavy rain forecast with rising flood gauges - road conditions may worsen rapidly")

    score = max(0.0, score)
    return RouteScoreCategory(score=round(score, 1), label=_score_label(score), factors=factors)


# ──────────────────────────────────────────────────────────────
# Services score (0-10)
# ──────────────────────────────────────────────────────────────

def _compute_services(
    fuel: Optional[FuelOverlay],
    rest: Optional[RestAreaOverlay],
) -> RouteScoreCategory:
    score = 10.0
    factors: List[str] = []

    # Fuel gap scoring: find the largest gap from warnings
    if fuel:
        max_fuel_gap = _extract_max_fuel_gap_km(fuel)
        if max_fuel_gap > 300:
            score -= 4
            factors.append(f"Fuel gap of {max_fuel_gap:.0f}km - very remote")
        elif max_fuel_gap > 250:
            score -= 3
            factors.append(f"Fuel gap of {max_fuel_gap:.0f}km - carry extra fuel")
        elif max_fuel_gap > 200:
            score -= 2
            factors.append(f"Fuel gap of {max_fuel_gap:.0f}km - plan fuel stops")
        elif max_fuel_gap > 150:
            score -= 1
            factors.append(f"Fuel gap of {max_fuel_gap:.0f}km")

    # Rest area gap scoring
    if rest:
        max_rest_gap = _extract_max_rest_gap_km(rest)
        if max_rest_gap > 250:
            score -= 3
            factors.append(f"Rest area gap of {max_rest_gap:.0f}km - fatigue risk")
        elif max_rest_gap > 200:
            score -= 2
            factors.append(f"Rest area gap of {max_rest_gap:.0f}km - plan rest stops")
        elif max_rest_gap > 150:
            score -= 1
            factors.append(f"Rest area gap of {max_rest_gap:.0f}km")

    score = max(0.0, score)
    return RouteScoreCategory(score=round(score, 1), label=_score_label(score), factors=factors)


def _extract_max_fuel_gap_km(fuel: FuelOverlay) -> float:
    """Parse fuel gap warnings to find the largest gap in km."""
    import re
    max_gap = 0.0
    for w in fuel.warnings:
        # Matches "No fuel station for NNN km ..."
        m = re.search(r"for\s+(\d+(?:\.\d+)?)\s*km", w, re.IGNORECASE)
        if m:
            gap = float(m.group(1))
            max_gap = max(max_gap, gap)
    return max_gap


def _extract_max_rest_gap_km(rest: RestAreaOverlay) -> float:
    """Extract the largest fatigue gap from rest area overlay."""
    max_gap = 0.0
    for fw in rest.fatigue_warnings:
        if fw.gap_km is not None:
            max_gap = max(max_gap, fw.gap_km)
    return max_gap


# ──────────────────────────────────────────────────────────────
# Weather comfort score (0-10)
# ──────────────────────────────────────────────────────────────

def _compute_weather_comfort(weather: Optional[WeatherOverlay]) -> RouteScoreCategory:
    factors: List[str] = []

    if not weather or not weather.points:
        return RouteScoreCategory(
            score=5.0,
            label=_score_label(5.0),
            factors=["No weather data available - score reflects uncertainty"],
        )

    pts = weather.points
    point_scores: List[float] = []

    for p in pts:
        ps = 10.0

        # Temperature comfort: ideal 18-25°C
        t = p.temperature_c
        if t > 42 or t < -5:
            ps -= 4
        elif t > 35 or t < 0:
            ps -= 2
        elif t > 30 or t < 5:
            ps -= 1

        # Rain: ideal < 20%
        rain = p.precipitation_probability_pct
        if rain > 80:
            ps -= 3
        elif rain > 50:
            ps -= 2
        elif rain > 20:
            ps -= 1

        # Wind: ideal < 20 km/h
        gust = p.wind_gust_kmh or p.wind_speed_kmh
        if gust > 60:
            ps -= 3
        elif gust > 40:
            ps -= 2
        elif gust > 20:
            ps -= 1

        # UV: ideal < 6
        uv = p.uv_index
        if uv >= 11:
            ps -= 2
        elif uv >= 8:
            ps -= 1

        point_scores.append(max(0.0, ps))

    avg = sum(point_scores) / len(point_scores)

    # Summarize what pulled the score down
    hot_pts = [p for p in pts if p.temperature_c > 35]
    cold_pts = [p for p in pts if p.temperature_c < 5]
    rainy_pts = [p for p in pts if p.precipitation_probability_pct > 50]
    windy_pts = [p for p in pts if (p.wind_gust_kmh or 0) > 40]
    uv_pts = [p for p in pts if p.uv_index >= 8]

    if hot_pts:
        factors.append(f"Hot conditions - up to {max(p.temperature_c for p in hot_pts):.0f}°C")
    if cold_pts:
        factors.append(f"Cold conditions - down to {min(p.temperature_c for p in cold_pts):.0f}°C")
    if rainy_pts:
        factors.append(f"Rain likely at {len(rainy_pts)} of {len(pts)} route sections")
    if windy_pts:
        factors.append(f"Strong winds (>{40}km/h) at {len(windy_pts)} section(s)")
    if uv_pts:
        factors.append(f"High UV (≥8) across {len(uv_pts)} section(s)")

    avg = round(max(0.0, avg), 1)
    return RouteScoreCategory(score=avg, label=_score_label(avg), factors=factors)


# ──────────────────────────────────────────────────────────────
# Summary generator
# ──────────────────────────────────────────────────────────────

def _build_summary(
    safety: RouteScoreCategory,
    conditions: RouteScoreCategory,
    services: RouteScoreCategory,
    weather: RouteScoreCategory,
    overall: float,
) -> str:
    """
    Generate a 1-2 sentence actionable summary focused on the worst factors.
    Reads like advice from an experienced road tripper.
    """
    # Collect the worst factors from worst-scoring categories
    cats = [
        ("safety", safety),
        ("conditions", conditions),
        ("services", services),
        ("weather", weather),
    ]
    cats_sorted = sorted(cats, key=lambda x: x[1].score)

    top_factors: List[str] = []
    for _, cat in cats_sorted:
        for f in cat.factors[:2]:
            top_factors.append(f)
        if len(top_factors) >= 3:
            break

    if not top_factors:
        return _score_advice(overall) + "."

    if overall >= 8.0:
        return f"{_score_advice(overall)}. {top_factors[0]}." if top_factors else _score_advice(overall) + "."

    if len(top_factors) == 1:
        return f"{top_factors[0]}. {_score_advice(overall)}."
    if len(top_factors) == 2:
        return f"{top_factors[0]}. {top_factors[1]}."
    return f"{top_factors[0]}. {top_factors[1]}; {top_factors[2].lower()}."


# ──────────────────────────────────────────────────────────────
# Service class
# ──────────────────────────────────────────────────────────────

class RouteScore:
    """
    Computes a composite Route Intelligence Score from all available overlays.

    Instantiated per-request (same pattern as other services - no shared state).
    All overlay parameters are optional; missing overlays are excluded from
    scoring with a data_warning added to the result.
    """

    def compute(
        self,
        *,
        weather: Optional[WeatherOverlay] = None,
        fuel: Optional[FuelOverlay] = None,
        flood: Optional[FloodOverlay] = None,
        rest: Optional[RestAreaOverlay] = None,
        coverage: Optional[CoverageOverlay] = None,
        wildlife: Optional[WildlifeOverlay] = None,
        traffic: Optional[TrafficOverlay] = None,
        hazards: Optional[HazardOverlay] = None,
        bushfire: Optional[BushfireOverlay] = None,
        air_quality: Optional[AirQualityOverlay] = None,
    ) -> RouteIntelligenceScore:
        data_warnings: List[str] = []

        # Track which overlays are missing and note them
        if traffic is None:
            data_warnings.append("Traffic data unavailable - safety score may be incomplete")
        if hazards is None:
            data_warnings.append("Hazards data unavailable - safety score may be incomplete")
        if flood is None:
            data_warnings.append("Flood data unavailable - score may be incomplete")
        if weather is None:
            data_warnings.append("Weather data unavailable - conditions score may be incomplete")
        if fuel is None:
            data_warnings.append("Fuel data unavailable - services score may be incomplete")
        if rest is None:
            data_warnings.append("Rest area data unavailable - services score may be incomplete")
        if coverage is None:
            data_warnings.append("Coverage data unavailable - safety score may be incomplete")
        if wildlife is None:
            data_warnings.append("Wildlife data unavailable - safety score may be incomplete")

        safety_cat = _compute_safety(traffic, hazards, flood, coverage, wildlife, bushfire, weather)
        conditions_cat = _compute_conditions(weather, flood, traffic, air_quality)
        services_cat = _compute_services(fuel, rest)
        weather_cat = _compute_weather_comfort(weather)

        # Weighted average: Safety 35%, Conditions 25%, Services 25%, Weather 15%
        overall = (
            safety_cat.score * 0.35
            + conditions_cat.score * 0.25
            + services_cat.score * 0.25
            + weather_cat.score * 0.15
        )
        overall = round(max(0.0, min(10.0, overall)), 1)

        summary = _build_summary(safety_cat, conditions_cat, services_cat, weather_cat, overall)

        logger.info(
            "RouteScore: overall=%.1f safety=%.1f conditions=%.1f services=%.1f weather=%.1f",
            overall, safety_cat.score, conditions_cat.score, services_cat.score, weather_cat.score,
        )

        return RouteIntelligenceScore(
            overall=overall,
            overall_label=_score_label(overall),
            summary=summary,
            safety=safety_cat,
            conditions=conditions_cat,
            services=services_cat,
            weather=weather_cat,
            data_warnings=data_warnings,
        )
