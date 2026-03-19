from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional
from pydantic import BaseModel, Field


# ──────────────────────────────────────────────────────────────
# Shared
# ──────────────────────────────────────────────────────────────

class NavCoord(BaseModel):
    lat: float
    lng: float


class TripStop(BaseModel):
    id: Optional[str] = None
    type: Literal["start", "poi", "via", "end"] = "poi"
    name: Optional[str] = None
    lat: float
    lng: float
    arrive_at: Optional[str] = None   # ISO8601 local planned arrival
    depart_at: Optional[str] = None   # ISO8601 local planned departure


# High-level category groups - each maps to multiple PlaceCategory values
CategoryGroup = Literal[
    "essentials",     # fuel, ev_charging, rest_area, toilet, water, mechanic, hospital, pharmacy
    "food",           # bakery, cafe, restaurant, fast_food, pub, bar
    "accommodation",  # camp, hotel, motel, hostel
    "nature",         # viewpoint, waterfall, swimming_hole, beach, national_park, hiking, picnic, hot_spring, cave, fishing, surf
    "culture",        # visitor_info, museum, gallery, heritage, winery, brewery, attraction, market, library, showground
    "family",         # playground, pool, zoo, theme_park, dog_park, golf, cinema
    "supplies",       # grocery, town, atm, laundromat, dump_point
]


class TripPreferences(BaseModel):
    """User-facing trip preferences controlling enrichment."""
    stop_density: int = Field(default=3, ge=1, le=5)   # 1=bare minimum, 5=everything
    categories: Dict[str, bool] = Field(default_factory=lambda: {
        "essentials": True,
        "food": True,
        "accommodation": True,
        "nature": True,
        "culture": True,
        "family": True,
        "supplies": True,
    })


# Maps CategoryGroup → PlaceCategory values
CATEGORY_GROUP_MAP: Dict[str, List[str]] = {
    "essentials":     ["fuel", "ev_charging", "rest_area", "toilet", "water", "mechanic", "hospital", "pharmacy", "emergency_phone"],
    "food":           ["bakery", "cafe", "restaurant", "fast_food", "pub", "bar"],
    "accommodation":  ["camp", "hotel", "motel", "hostel"],
    "nature":         ["viewpoint", "waterfall", "swimming_hole", "beach", "national_park", "hiking", "picnic", "hot_spring", "cave", "fishing", "surf"],
    "culture":        ["visitor_info", "museum", "gallery", "heritage", "winery", "brewery", "attraction", "market", "library", "showground"],
    "family":         ["playground", "pool", "zoo", "theme_park", "dog_park", "golf", "cinema"],
    "supplies":       ["grocery", "town", "atm", "laundromat", "dump_point", "shower", "water_fill"],
}


def resolve_categories(prefs: Optional[TripPreferences] = None) -> List[str]:
    """Expand TripPreferences into a flat list of enabled PlaceCategory strings."""
    if prefs is None:
        # All categories enabled
        cats: List[str] = []
        for group_cats in CATEGORY_GROUP_MAP.values():
            cats.extend(group_cats)
        return sorted(set(cats))
    enabled: List[str] = []
    for group, group_cats in CATEGORY_GROUP_MAP.items():
        if prefs.categories.get(group, True):
            enabled.extend(group_cats)
    return sorted(set(enabled))


def density_budget_multiplier(density: int) -> float:
    """Map stop_density 1-5 to a budget multiplier for places searches.

    1 = 0.15  (bare minimum - fuel + rest only)
    2 = 0.45  (light - essentials + a few highlights)
    3 = 1.0   (balanced - default, current behaviour)
    4 = 1.5   (generous - more stops)
    5 = 2.0   (everything - maximum enrichment)
    """
    return {1: 0.15, 2: 0.45, 3: 1.0, 4: 1.5, 5: 2.0}.get(density, 1.0)


class BBox4(BaseModel):
    minLng: float
    minLat: float
    maxLng: float
    maxLat: float


# ──────────────────────────────────────────────────────────────
# Navigation - Maneuvers & Steps (turn-by-turn)
# ──────────────────────────────────────────────────────────────

ManeuverType = Literal[
    "turn", "depart", "arrive",
    "merge", "fork", "on ramp", "off ramp",
    "roundabout", "rotary", "exit roundabout",
    "new name", "continue", "end of road",
    "notification",
]

ManeuverModifier = Literal[
    "left", "right",
    "slight left", "slight right",
    "sharp left", "sharp right",
    "straight", "uturn",
]


class NavManeuver(BaseModel):
    type: ManeuverType = "turn"
    modifier: Optional[ManeuverModifier] = None
    location: List[float]           # [lng, lat] - OSRM convention
    bearing_before: int = 0
    bearing_after: int = 0
    exit: Optional[int] = None      # roundabout exit number


class NavStep(BaseModel):
    maneuver: NavManeuver
    name: str                       # road name ("Bruce Highway", "")
    ref: Optional[str] = None       # route reference ("M1", "A1")
    distance_m: float
    duration_s: float
    geometry: str                   # polyline6 for this step's segment
    mode: str = "driving"
    pronunciation: Optional[str] = None  # phonetic road name for TTS


# ──────────────────────────────────────────────────────────────
# Navigation - Core route models
# ──────────────────────────────────────────────────────────────

class AvoidZoneRequest(BaseModel):
    """A circular zone the router should try to avoid."""
    lat: float
    lng: float
    radius_km: float = 5.0


class NavRequest(BaseModel):
    profile: str = "drive"
    prefs: Dict[str, Any] = Field(default_factory=dict)
    stops: List[TripStop]
    avoid: List[str] = Field(default_factory=list)
    avoid_zones: List[AvoidZoneRequest] = Field(default_factory=list)
    depart_at: Optional[str] = None  # ISO8601 UTC recommended


class NavLeg(BaseModel):
    idx: int
    from_stop_id: Optional[str] = None
    to_stop_id: Optional[str] = None
    distance_m: int
    duration_s: int
    geometry: str                   # Polyline6 (this leg only)
    steps: List[NavStep] = Field(default_factory=list)


class NavRoute(BaseModel):
    route_key: str
    profile: str
    distance_m: int
    duration_s: int
    geometry: str                   # Polyline6 (full route)
    bbox: BBox4
    legs: List[NavLeg]
    provider: str                   # "osrm"
    created_at: str                 # ISO8601 UTC
    algo_version: str


class RouteAlternates(BaseModel):
    alternates: List[NavRoute] = Field(default_factory=list)


class NavPack(BaseModel):
    req: NavRequest
    primary: NavRoute
    alternates: RouteAlternates = Field(default_factory=RouteAlternates)
    warnings: List[str] = Field(default_factory=list)


# ──────────────────────────────────────────────────────────────
# Elevation profiles
# ──────────────────────────────────────────────────────────────

class ElevationRequest(BaseModel):
    geometry: str                   # polyline6
    sample_interval_m: int = 500    # sample every N metres
    route_key: Optional[str] = None


class ElevationSample(BaseModel):
    km_along: float
    elevation_m: float
    lat: float
    lng: float


class ElevationProfile(BaseModel):
    route_key: Optional[str] = None
    samples: List[ElevationSample]
    min_elevation_m: float
    max_elevation_m: float
    total_ascent_m: float
    total_descent_m: float
    created_at: str


class GradeSegment(BaseModel):
    """Derived segment for elevation-aware fuel analysis."""
    from_km: float
    to_km: float
    avg_grade_pct: float            # positive = uphill, negative = downhill
    elevation_change_m: float
    fuel_penalty_factor: float      # 1.0 = flat, 1.35 = steep uphill, 0.85 = steep downhill


# ──────────────────────────────────────────────────────────────
# Corridor graphs
# ──────────────────────────────────────────────────────────────

class CorridorGraphMeta(BaseModel):
    corridor_key: str
    route_key: str
    profile: str
    buffer_m: int
    max_edges: int
    algo_version: str
    created_at: str
    bytes: int
    # Optional inline pack - avoids a separate GET that may hit a different instance
    pack: Optional["CorridorGraphPack"] = None


class CorridorNode(BaseModel):
    id: int
    lat: float
    lng: float


class CorridorEdge(BaseModel):
    a: int
    b: int
    distance_m: int
    duration_s: int
    flags: int = 0


class CorridorGraphPack(BaseModel):
    corridor_key: str
    route_key: str
    profile: str
    algo_version: str
    bbox: BBox4
    nodes: List[CorridorNode] = Field(default_factory=list)
    edges: List[CorridorEdge] = Field(default_factory=list)


# Resolve forward reference for CorridorGraphMeta.pack
CorridorGraphMeta.model_rebuild()


# ──────────────────────────────────────────────────────────────
# Places - category taxonomy
# ──────────────────────────────────────────────────────────────
# Every category MUST map to at least one Overpass filter in
# places.py _FALLBACK_FILTERS.  Grouped by traveller need.
#
# ESSENTIALS & SAFETY - the "survive the outback" tier
#   fuel          Petrol/diesel stations
#   ev_charging   Electric vehicle charge points
#   rest_area     Roadside rest stops / driver reviver (fatigue!)
#   toilet        Public toilets (standalone, not inside a venue)
#   water         Drinking water taps, tanks, bores
#   water_fill    Caravan/RV potable water fill stations
#   dump_point    Caravan/RV grey/black water dump stations
#   mechanic      Car repair, tyre shops, NRMA/RACQ
#   hospital      Hospitals & emergency departments
#   pharmacy      Chemists
#
# SUPPLIES
#   grocery       Supermarkets, IGA, convenience stores
#   town          Towns, villages, hamlets (anchor points)
#   atm           ATMs & bank branches (cash-only outback shops)
#   laundromat    Laundromats / coin laundry (multi-day trips)
#
# FOOD & DRINK
#   bakery        Bakeries (THE Aussie road trip stop - pies!)
#   cafe          Cafés & coffee shops
#   restaurant    Sit-down restaurants
#   fast_food     Fast food / takeaway
#   pub           Pubs & taverns (counter meals)
#   bar           Bars & cocktail lounges
#
# ACCOMMODATION
#   camp          Camp sites, caravan parks, free camps
#   hotel         Hotels
#   motel         Motels (roadside, quintessential road trip)
#   hostel        Hostels / backpackers
#
# NATURE & OUTDOORS
#   viewpoint     Lookouts & scenic viewpoints
#   waterfall     Waterfalls
#   swimming_hole Natural swimming holes & rock pools
#   beach         Beaches
#   national_park National parks & nature reserves
#   hiking        Walking tracks, trails, trailheads
#   picnic        Picnic areas, BBQ spots, shelters
#   hot_spring    Hot springs & thermal pools
#   cave          Caves, show caves, caverns
#   fishing       Fishing spots, boat ramps, angling areas
#   surf          Surf breaks, surf spots
#
# FAMILY & RECREATION
#   playground    Playgrounds & skate parks
#   pool          Public swimming pools & aquatic centres
#   zoo           Zoos, wildlife parks, aquariums, sanctuaries
#   theme_park    Theme parks, water parks, mini golf, go-karts
#   dog_park      Off-leash dog parks & exercise areas
#   golf          Golf courses (country town staple)
#   cinema        Cinemas & drive-ins
#
# CULTURE & SIGHTSEEING
#   visitor_info  Visitor information centres / i-sites
#   museum        Museums
#   gallery       Art galleries
#   heritage      Heritage-listed sites, historic buildings, mines, wrecks
#   winery        Wineries & cellar doors
#   brewery       Breweries, distilleries, cideries
#   attraction    Generic tourist attractions, "Big Things", public art
#   market        Markets (farmers, craft, weekend), delis, farm stalls
#   park          Urban parks & gardens
#   library       Public libraries (rest day, free wifi, AC)
#   showground    Showgrounds, racecourses (events, sometimes free camping)
#
# GEOCODING (from Mapbox forward search - not Overpass)
#   address       Street address result
#   place         Named place / locality result
#   region        State / territory result
# ──────────────────────────────────────────────────────────────

PlaceCategory = Literal[
    # Essentials & safety
    "fuel", "ev_charging", "rest_area", "toilet", "water", "water_fill",
    "dump_point", "shower", "mechanic", "hospital", "pharmacy",
    "emergency_phone",
    # Supplies
    "grocery", "town", "atm", "laundromat",
    # Food & drink
    "bakery", "cafe", "restaurant", "fast_food", "pub", "bar",
    # Accommodation
    "camp", "hotel", "motel", "hostel",
    # Nature & outdoors
    "viewpoint", "waterfall", "swimming_hole", "beach",
    "national_park", "hiking", "picnic", "hot_spring",
    "cave", "fishing", "surf",
    # Family & recreation
    "playground", "pool", "zoo", "theme_park",
    "dog_park", "golf", "cinema",
    # Culture & sightseeing
    "visitor_info", "museum", "gallery", "heritage",
    "winery", "brewery", "attraction", "market", "park",
    "library", "showground",
    # Geocoding (Mapbox)
    "address", "place", "region",
]


class PlacesRequest(BaseModel):
    bbox: Optional[BBox4] = None
    center: Optional[NavCoord] = None
    radius_m: Optional[int] = None
    categories: List[PlaceCategory] = Field(default_factory=list)
    query: Optional[str] = None
    limit: int = 50


class PlaceItem(BaseModel):
    id: str
    name: str
    lat: float
    lng: float
    category: PlaceCategory
    extra: Dict[str, Any] = Field(default_factory=dict)


class PlacesPack(BaseModel):
    places_key: str
    req: PlacesRequest
    items: List[PlaceItem]
    provider: str
    created_at: str
    algo_version: str


class CorridorPlacesRequest(BaseModel):
    corridor_key: str
    categories: Optional[List[PlaceCategory]] = None
    limit: Optional[int] = None
    # Route polyline for true corridor search
    geometry: Optional[str] = None          # Polyline6 of the route
    buffer_km: Optional[float] = 35.0       # Corridor buffer radius in km
    stop_density: int = Field(default=3, ge=1, le=5)  # 1=bare minimum, 5=everything


class PlacesSuggestRequest(BaseModel):
    geometry: str  # polyline6
    interval_km: int = 50
    radius_m: int = 15000
    categories: List[PlaceCategory] = Field(default_factory=list)
    limit_per_sample: int = 150
    stop_density: int = Field(default=3, ge=1, le=5)  # 1=bare minimum, 5=everything


class PlacesSuggestionCluster(BaseModel):
    idx: int
    lat: float
    lng: float
    km_from_start: float
    places: PlacesPack


class PlacesSuggestResponse(BaseModel):
    clusters: List[PlacesSuggestionCluster]


class StopSuggestionsRequest(BaseModel):
    """Request nearby POI suggestions for the trip stop list.

    Uses the trip bounding box for Overpass queries, then scores candidates
    by proximity to the route midpoint and category diversity vs existing stops.
    """
    bbox: BBox4                                   # bounding box of the full route
    midpoint: NavCoord                            # geographic midpoint of the route
    existing_categories: List[PlaceCategory] = Field(default_factory=list)
    limit: int = 4                                # max suggestions to return


class StopSuggestionItem(BaseModel):
    """A single suggested place to add as a trip stop."""
    id: str
    name: str
    lat: float
    lng: float
    category: PlaceCategory
    score: float
    extra: Dict[str, Any] = Field(default_factory=dict)


class StopSuggestionsResponse(BaseModel):
    suggestions: List[StopSuggestionItem]


# ──────────────────────────────────────────────────────────────
# Guide (LLM-driven companion)
# ──────────────────────────────────────────────────────────────

GuideToolName = Literal["places_search", "places_corridor", "places_suggest"]
GuideActionType = Literal["web", "call", "map", "save"]


class GuideMsg(BaseModel):
    role: Literal["user", "assistant"]
    content: str


class TripProgress(BaseModel):
    """Live position + progress telemetry sent from frontend."""
    user_lat: float
    user_lng: float
    user_accuracy_m: float = 0.0
    user_heading: Optional[float] = None
    user_speed_mps: Optional[float] = None

    current_stop_idx: int = 0
    current_leg_idx: int = 0
    visited_stop_ids: List[str] = Field(default_factory=list)

    km_from_start: float = 0.0
    km_remaining: float = 0.0
    total_km: float = 0.0

    local_time_iso: Optional[str] = None
    timezone: str = "Australia/Brisbane"
    updated_at: Optional[str] = None


class WirePlace(BaseModel):
    """
    Pre-filtered "relevant places" the server hands to the LLM so it
    can recommend without a tool call.  Includes contact info for
    action buttons.
    """
    id: str
    name: str
    lat: float
    lng: float
    category: str
    dist_km: Optional[float] = None
    ahead: bool = True
    locality: Optional[str] = None
    hours: Optional[str] = None
    phone: Optional[str] = None
    website: Optional[str] = None
    # Free camping fields - populated for camp + rest_area categories
    camp_type: Optional[str] = None
    free: Optional[bool] = None
    price_per_night_aud: Optional[float] = None
    overnight_allowed: Optional[str] = None   # "true" | "false" | "check" | "prohibited"
    overnight_max_hours: Optional[int] = None
    overnight_notes: Optional[str] = None
    has_toilets: Optional[bool] = None
    has_water: Optional[bool] = None
    has_showers: Optional[bool] = None
    has_bbq: Optional[bool] = None
    pets_allowed: Optional[str] = None        # "yes" | "leashed" | "no"
    fires_allowed: Optional[str] = None       # "true" | "false" | "seasonal"
    max_stay_days: Optional[float] = None
    accessible: Optional[bool] = None
    quality_score: float = 0.0


class GuideContext(BaseModel):
    plan_id: Optional[str] = None
    label: Optional[str] = None

    profile: Optional[str] = None
    route_key: Optional[str] = None
    corridor_key: Optional[str] = None

    geometry: Optional[str] = None  # polyline6
    bbox: Optional[Dict[str, Any]] = None  # BBox4-ish dict

    stops: List[Dict[str, Any]] = Field(default_factory=list)
    total_distance_m: Optional[float] = None
    total_duration_s: Optional[float] = None

    manifest_route_key: Optional[str] = None
    offline_stale: Optional[bool] = None

    stop_density: Optional[int] = None  # 1-5, from trip prefs

    progress: Optional[TripProgress] = None

    traffic_summary: Optional[Dict[str, Any]] = None
    hazards_summary: Optional[Dict[str, Any]] = None
    route_score_summary: Optional[Dict[str, Any]] = None  # summarized RouteIntelligenceScore
    flood_summary: Optional[Dict[str, Any]] = None
    coverage_summary: Optional[Dict[str, Any]] = None
    wildlife_summary: Optional[Dict[str, Any]] = None
    weather_summary: Optional[Dict[str, Any]] = None
    fuel_benchmarks: Optional[Dict[str, Dict[str, float]]] = None  # city -> {"unleaded": 182.5, "diesel": 167.7}

    # Live driver state - fuel, fatigue, speed, night, temp, ETA
    driver_state: Optional[Dict[str, Any]] = None
    # Next challenge: most critical upcoming issue on route
    next_challenge: Optional[Dict[str, Any]] = None


class GuideAction(BaseModel):
    """Structured UI action rendered as a button/pill in the chat."""
    type: GuideActionType
    label: str
    place_id: Optional[str] = None
    place_name: Optional[str] = None
    url: Optional[str] = None
    tel: Optional[str] = None
    # For type="map" - lat/lng to center the map on
    lat: Optional[float] = None
    lng: Optional[float] = None
    category: Optional[str] = None
    # For type="save" - enriched place listing for the Found tab
    description: Optional[str] = None  # 1-2 sentence prose description


class GuideToolCall(BaseModel):
    id: Optional[str] = None
    tool: GuideToolName
    req: Dict[str, Any]


class GuideToolResult(BaseModel):
    id: str
    tool: GuideToolName
    ok: bool = True
    result: Dict[str, Any]


class GuideTurnRequest(BaseModel):
    context: GuideContext
    thread: List[GuideMsg] = Field(default_factory=list)
    tool_results: List[GuideToolResult] = Field(default_factory=list)
    preferred_categories: List[str] = Field(default_factory=list)
    relevant_places: List[WirePlace] = Field(default_factory=list)


class GuideTurnResponse(BaseModel):
    assistant: str = ""
    actions: List[GuideAction] = Field(default_factory=list)
    tool_calls: List[GuideToolCall] = Field(default_factory=list)
    done: bool = False


# ──────────────────────────────────────────────────────────────
# Traffic + Hazards overlays
# ──────────────────────────────────────────────────────────────

TrafficSeverity = Literal["info", "minor", "moderate", "major", "unknown"]
TrafficType = Literal["hazard", "closure", "congestion", "roadworks", "flooding", "incident", "unknown"]

HazardSeverity = Literal["low", "medium", "high", "unknown"]
HazardKind = Literal["flood", "cyclone", "storm", "fire", "wind", "heat", "marine", "weather_warning", "road_crash", "road_closure", "unknown"]

# CAP-AU urgency and certainty levels (used for composite severity scoring)
CapUrgency = Literal["immediate", "expected", "future", "past", "unknown"]
CapCertainty = Literal["observed", "likely", "possible", "unlikely", "unknown"]

# Route impact classification - computed client-side by intersecting alert
# geometry with the route polyline buffer.
RouteImpact = Literal[
    "blocks_route",     # closure/flood geometry intersects route within 500m
    "affects_route",    # hazard zone covers part of route, road may be passable
    "nearby",           # within corridor but not directly on route
    "informational",    # in the region but irrelevant to this specific route
]

GeoJSON = Dict[str, Any]


class TrafficEvent(BaseModel):
    id: str
    source: str
    feed: str
    type: TrafficType = "unknown"
    severity: TrafficSeverity = "unknown"
    headline: str
    description: Optional[str] = None
    url: Optional[str] = None
    last_updated: Optional[str] = None
    start_at: Optional[str] = None
    end_at: Optional[str] = None
    geometry: Optional[GeoJSON] = None
    bbox: Optional[List[float]] = None
    region: Optional[str] = None  # "qld", "nsw", "vic", etc.
    raw: Dict[str, Any] = Field(default_factory=dict)


class TrafficOverlay(BaseModel):
    traffic_key: str
    bbox: BBox4
    provider: str
    algo_version: str
    created_at: str
    items: List[TrafficEvent] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)


class HazardEvent(BaseModel):
    id: str
    source: str
    kind: HazardKind = "unknown"
    severity: HazardSeverity = "unknown"
    # CAP-AU composite scoring fields
    urgency: CapUrgency = "unknown"
    certainty: CapCertainty = "unknown"
    effective_priority: float = 0.0  # 0.0 (lowest) to 1.0 (highest)
    title: str
    description: Optional[str] = None
    url: Optional[str] = None
    issued_at: Optional[str] = None
    start_at: Optional[str] = None
    end_at: Optional[str] = None
    geometry: Optional[GeoJSON] = None
    bbox: Optional[List[float]] = None
    region: Optional[str] = None  # "qld", "nsw", "vic", etc.
    raw: Dict[str, Any] = Field(default_factory=dict)


class HazardOverlay(BaseModel):
    hazards_key: str
    bbox: BBox4
    provider: str
    algo_version: str
    created_at: str
    items: List[HazardEvent] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)


# ──────────────────────────────────────────────────────────────
# Fuel overlay
# ──────────────────────────────────────────────────────────────

FuelType = Literal[
    "diesel", "unleaded", "premium_unleaded_95", "premium_unleaded_98",
    "e10", "lpg", "adblue", "truck_diesel", "premium_diesel", "e85", "biodiesel",
]


class FuelPrice(BaseModel):
    fuel_type: str  # "E10", "Unleaded", "Diesel", "PULP 95", "PULP 98", "LPG", "Premium Diesel",
                    # "adblue", "truck_diesel", "premium_diesel", "e85", "biodiesel"
    price_cents: float
    last_updated: Optional[str] = None


class FuelStation(BaseModel):
    id: str
    source: str  # "petrolspy", "nsw_fuelcheck", "wa_fuelwatch"
    name: str
    brand: Optional[str] = None
    lat: float
    lng: float
    address: Optional[str] = None
    fuel_types: List[FuelPrice] = Field(default_factory=list)
    is_open: Optional[bool] = None
    open_hours: Optional[str] = None
    distance_km: Optional[float] = None  # From route
    extra: Dict[str, Any] = Field(default_factory=dict)


class EVConnector(BaseModel):
    type: str  # "Type 2", "CCS", "CHAdeMO", etc.
    power_kw: Optional[float] = None
    quantity: int = 1


class EVCharger(BaseModel):
    id: str
    source: str  # "openchargemap"
    name: str
    operator: Optional[str] = None
    lat: float
    lng: float
    address: Optional[str] = None
    connectors: List[EVConnector] = Field(default_factory=list)
    is_operational: Optional[bool] = None
    usage_cost: Optional[str] = None
    distance_km: Optional[float] = None  # From route


class FuelOverlay(BaseModel):
    fuel_key: str
    bbox: Optional[BBox4] = None
    algo_version: str
    created_at: str
    stations: List[FuelStation] = Field(default_factory=list)
    ev_chargers: List[EVCharger] = Field(default_factory=list)
    city_averages: Dict[str, Dict[str, float]] = Field(default_factory=dict)  # city -> {"unleaded": 182.5, "diesel": 167.7}
    warnings: List[str] = Field(default_factory=list)


# ──────────────────────────────────────────────────────────────
# Flood gauge overlay
# ──────────────────────────────────────────────────────────────

class FloodGauge(BaseModel):
    station_no: str
    station_name: str
    lat: float
    lng: float
    data_owner: str  # For attribution (DATA_OWNER_NAME from BOM station list)
    latest_height_m: Optional[float] = None
    reading_time_iso: Optional[str] = None
    trend: Literal["rising", "falling", "steady", "unknown"] = "unknown"
    severity: Literal["normal", "minor", "moderate", "major", "unknown"] = "unknown"
    distance_from_route_km: Optional[float] = None


class FloodCatchment(BaseModel):
    aac: str          # area code e.g. "QLD_FL004"
    dist_name: str    # e.g. "Brisbane River"
    level: str        # "watch" or "warning"
    geometry: GeoJSON


class FloodCamera(BaseModel):
    id: str
    source: str  # "qld_flood_cameras"
    name: Optional[str] = None
    lat: float
    lng: float
    image_url: Optional[str] = None
    road: Optional[str] = None
    distance_from_route_km: Optional[float] = None


class FloodOverlay(BaseModel):
    flood_key: str
    bbox: BBox4
    algo_version: str
    created_at: str
    gauges: List[FloodGauge] = Field(default_factory=list)
    catchments: List[FloodCatchment] = Field(default_factory=list)
    flood_cameras: List[FloodCamera] = Field(default_factory=list)
    attributions: List[str] = Field(default_factory=list)  # Unique DATA_OWNER_NAMEs
    warnings: List[str] = Field(default_factory=list)
    route_passes_through_warning: bool = False


# ──────────────────────────────────────────────────────────────
# Offline bundles
# ──────────────────────────────────────────────────────────────

AssetStatus = Literal["missing", "ready", "error"]


class OfflineBundleManifest(BaseModel):
    plan_id: str
    route_key: str

    tiles_id: str = "australia"
    styles: List[str] = Field(default_factory=list)

    navpack_status: AssetStatus = "missing"
    corridor_status: AssetStatus = "missing"
    places_status: AssetStatus = "missing"
    traffic_status: AssetStatus = "missing"
    hazards_status: AssetStatus = "missing"
    elevation_status: AssetStatus = "missing"
    flood_status: AssetStatus = "missing"
    weather_status: AssetStatus = "missing"
    fuel_status: AssetStatus = "missing"
    coverage_status: AssetStatus = "missing"
    wildlife_status: AssetStatus = "missing"

    corridor_key: Optional[str] = None
    places_key: Optional[str] = None
    traffic_key: Optional[str] = None
    hazards_key: Optional[str] = None
    flood_key: Optional[str] = None
    weather_key: Optional[str] = None
    fuel_key: Optional[str] = None
    coverage_key: Optional[str] = None
    wildlife_key: Optional[str] = None
    rest_key: Optional[str] = None

    rest_status: AssetStatus = "missing"
    score_status: AssetStatus = "missing"
    score_key: Optional[str] = None

    emergency_status: AssetStatus = "missing"
    emergency_key: Optional[str] = None
    heritage_status: AssetStatus = "missing"
    heritage_key: Optional[str] = None
    aqi_status: AssetStatus = "missing"
    aqi_key: Optional[str] = None
    bushfire_status: AssetStatus = "missing"
    bushfire_key: Optional[str] = None
    cameras_status: AssetStatus = "missing"
    cameras_key: Optional[str] = None
    toilets_status: AssetStatus = "missing"
    toilets_key: Optional[str] = None
    school_zones_status: AssetStatus = "missing"
    school_zones_key: Optional[str] = None
    roadkill_status: AssetStatus = "missing"
    roadkill_key: Optional[str] = None

    bytes_total: int = 0
    created_at: str


# ──────────────────────────────────────────────────────────────
# Weather overlay
# ──────────────────────────────────────────────────────────────

class WeatherPoint(BaseModel):
    lat: float
    lng: float
    km_along: float
    eta_iso: str  # When the user will be at this point
    temperature_c: float
    apparent_temperature_c: float
    precipitation_probability_pct: int
    precipitation_mm: float
    weather_code: int  # WMO weather code
    weather_description: str  # Human readable from WMO code
    wind_speed_kmh: float
    wind_gust_kmh: Optional[float] = None
    wind_direction_deg: int
    uv_index: float
    cloud_cover_pct: int
    visibility_m: Optional[float] = None
    sunrise_iso: Optional[str] = None
    sunset_iso: Optional[str] = None
    civil_twilight_begin_iso: Optional[str] = None
    civil_twilight_end_iso: Optional[str] = None
    is_daylight: bool  # Whether ETA falls within sunrise-sunset
    is_twilight_danger: bool  # Dawn/dusk ±30min = wildlife risk


class WeatherOverlay(BaseModel):
    weather_key: str
    polyline6: str
    departure_iso: str
    algo_version: str
    created_at: str
    points: List[WeatherPoint] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)


# ──────────────────────────────────────────────────────────────
# Rest Areas + Fatigue Management overlay
# ──────────────────────────────────────────────────────────────

class RestFacilities(BaseModel):
    toilets: bool | None = None
    drinking_water: bool | None = None
    shower: bool | None = None
    bbq: bool | None = None
    picnic_table: bool | None = None
    power_supply: bool | None = None
    internet: bool | None = None
    lit: bool | None = None
    shelter: bool | None = None
    capacity: int | None = None


class RestArea(BaseModel):
    id: str
    name: str | None = None
    lat: float
    lng: float
    type: Literal["rest_area", "camp_site", "caravan_site", "service_station", "toilets"]
    km_along: float | None = None
    distance_from_route_km: float | None = None
    quality_score: int = 0
    facilities: RestFacilities = Field(default_factory=RestFacilities)
    opening_hours: str | None = None
    fee: bool | None = None
    source: str = "overpass"


class FatigueWarning(BaseModel):
    type: Literal["long_gap", "suggested_rest"]
    message: str
    km_from: float
    km_to: float | None = None
    gap_km: float | None = None
    suggested_stop: RestArea | None = None


class RestAreaOverlay(BaseModel):
    rest_key: str
    polyline6: str
    algo_version: str
    created_at: str
    rest_areas: List[RestArea] = Field(default_factory=list)
    fatigue_warnings: List[FatigueWarning] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)


# ──────────────────────────────────────────────────────────────
# Mobile coverage overlay
# ──────────────────────────────────────────────────────────────

CoverageLevel = Literal["reliable_4g", "voice_only", "weak", "no_coverage"]


class CoveragePoint(BaseModel):
    lat: float
    lng: float
    km_along: float
    telstra: CoverageLevel = "no_coverage"
    optus: CoverageLevel = "no_coverage"
    vodafone: CoverageLevel = "no_coverage"
    best_carrier: Optional[str] = None
    best_signal: CoverageLevel = "no_coverage"


class CoverageGap(BaseModel):
    km_from: float
    km_to: float
    gap_km: float
    carrier: str   # "Telstra", "Optus", "Vodafone", or "all"
    message: str


class CoverageOverlay(BaseModel):
    coverage_key: str
    polyline6: str
    algo_version: str
    created_at: str
    points: List[CoveragePoint] = Field(default_factory=list)
    gaps: List[CoverageGap] = Field(default_factory=list)
    best_carrier_overall: Optional[str] = None
    carrier_scores: Dict[str, float] = Field(default_factory=dict)  # carrier -> % route with 4G
    warnings: List[str] = Field(default_factory=list)


# ──────────────────────────────────────────────────────────────
# Wildlife Hazard Overlay
# ──────────────────────────────────────────────────────────────

class WildlifeZone(BaseModel):
    lat: float
    lng: float
    km_from: float
    km_to: float
    risk_level: Literal["low", "medium", "high", "none"] = "none"
    dominant_species: List[str] = Field(default_factory=list)
    occurrence_count: int = 0
    is_twilight_risk: bool = False
    message: Optional[str] = None
    # iNaturalist observation fields (populated when source=inaturalist)
    species_guess: Optional[str] = None
    photos: List[str] = Field(default_factory=list)
    attribution: Optional[str] = None
    observation_id: Optional[int] = None


class WildlifeOverlay(BaseModel):
    wildlife_key: str
    polyline6: str
    algo_version: str
    created_at: str
    zones: List[WildlifeZone] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)


# ──────────────────────────────────────────────────────────────
# Sync (minimal placeholder)
# ──────────────────────────────────────────────────────────────

class SyncOp(BaseModel):
    id: str
    type: str
    payload: Dict[str, Any]
    created_at: str


class SyncOpsRequest(BaseModel):
    ops: List[SyncOp]


class SyncOpsResponse(BaseModel):
    accepted: int


# ──────────────────────────────────────────────────────────────
# Route Intelligence Score
# ──────────────────────────────────────────────────────────────

class RouteScoreCategory(BaseModel):
    score: float  # 0.0-10.0
    label: str    # "Excellent", "Good", "Fair", "Poor", "Dangerous"
    factors: List[str] = Field(default_factory=list)  # Human-readable deduction reasons


class RouteIntelligenceScore(BaseModel):
    overall: float        # 0.0-10.0
    overall_label: str
    summary: str          # Actionable 1-2 sentence advice
    safety: RouteScoreCategory
    conditions: RouteScoreCategory
    services: RouteScoreCategory
    weather: RouteScoreCategory
    data_warnings: List[str] = Field(default_factory=list)  # Missing overlay warnings


# ──────────────────────────────────────────────────────────────
# Emergency Services overlay (GA Emergency Management Facilities)
# CC-BY 4.0, no auth required
# ──────────────────────────────────────────────────────────────

class EmergencyFacility(BaseModel):
    id: str
    name: str
    facility_type: str  # "hospital", "ambulance", "police", "fire", "ses"
    lat: float
    lng: float
    address: Optional[str] = None
    suburb: Optional[str] = None
    postcode: Optional[str] = None
    state: Optional[str] = None
    distance_from_route_km: Optional[float] = None


class EmergencyServicesOverlay(BaseModel):
    emergency_key: str
    polyline6: str
    algo_version: str
    created_at: str
    facilities: List[EmergencyFacility] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)


# ──────────────────────────────────────────────────────────────
# Heritage & Protected Areas overlay (DCCEEW GIS, CC-BY 3.0 AU)
# ──────────────────────────────────────────────────────────────

class HeritageSite(BaseModel):
    id: str
    name: str
    site_type: str  # "world_heritage", "national_heritage", "commonwealth_heritage", "protected_area"
    classification: Optional[str] = None  # indigenous/natural/historic or IUCN category
    state: Optional[str] = None
    authority: Optional[str] = None
    lat: Optional[float] = None
    lng: Optional[float] = None


class HeritageOverlay(BaseModel):
    heritage_key: str
    polyline6: str
    algo_version: str
    created_at: str
    sites: List[HeritageSite] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)


# ──────────────────────────────────────────────────────────────
# Air Quality overlay (OpenWeatherMap Air Pollution API)
# ──────────────────────────────────────────────────────────────

class AirQualityPoint(BaseModel):
    lat: float
    lng: float
    km_along: float
    aqi: int  # 1-5 OWM scale
    aqi_label: str  # "Good", "Fair", "Moderate", "Poor", "Very Poor"
    pm25: Optional[float] = None
    pm10: Optional[float] = None
    co: Optional[float] = None
    no2: Optional[float] = None
    o3: Optional[float] = None
    so2: Optional[float] = None


class AirQualityOverlay(BaseModel):
    aqi_key: str
    polyline6: str
    algo_version: str
    created_at: str
    points: List[AirQualityPoint] = Field(default_factory=list)
    overall_aqi: int = 1
    overall_label: str = "Good"
    health_advice: str = ""
    warnings: List[str] = Field(default_factory=list)


# ──────────────────────────────────────────────────────────────
# Bushfire overlay (NSW RFS + NASA FIRMS)
# ──────────────────────────────────────────────────────────────

class BushfireIncident(BaseModel):
    id: str
    source: str  # "nsw_rfs" or "firms"
    title: str
    alert_level: Optional[str] = None
    status: Optional[str] = None
    fire_type: Optional[str] = None
    size_ha: Optional[float] = None
    lat: float
    lng: float
    geometry: Optional[GeoJSON] = None
    distance_from_route_km: Optional[float] = None
    pub_date: Optional[str] = None
    council_area: Optional[str] = None
    responsible_agency: Optional[str] = None


class FirmsHotspot(BaseModel):
    lat: float
    lng: float
    brightness: Optional[float] = None
    confidence: Optional[str] = None
    acq_date: Optional[str] = None
    acq_time: Optional[str] = None
    frp: Optional[float] = None
    distance_from_route_km: Optional[float] = None


class BushfireOverlay(BaseModel):
    bushfire_key: str
    polyline6: str
    algo_version: str
    created_at: str
    incidents: List[BushfireIncident] = Field(default_factory=list)
    hotspots: List[FirmsHotspot] = Field(default_factory=list)
    fires_near_route: int = 0
    max_alert_level: Optional[str] = None
    warnings: List[str] = Field(default_factory=list)


# ──────────────────────────────────────────────────────────────
# Speed Cameras overlay (NSW TfNSW + Brisbane Council, CC-BY)
# ──────────────────────────────────────────────────────────────

class SpeedCamera(BaseModel):
    id: str
    source: str  # "nsw_tfnsw"
    camera_type: str  # "fixed_speed", "red_light_speed", "school_zone"
    location_desc: str
    road: Optional[str] = None
    suburb: Optional[str] = None
    lat: float
    lng: float
    is_school_zone: bool = False
    distance_from_route_km: Optional[float] = None


class RoadOccupancy(BaseModel):
    id: str
    source: str  # "brisbane_council"
    road: str
    suburb: Optional[str] = None
    closure_type: Optional[str] = None
    traffic_impact: Optional[str] = None
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    hours: Optional[str] = None


class RoadBlackSpot(BaseModel):
    id: str
    source: str  # "qld_blackspots"
    road: Optional[str] = None
    location_desc: Optional[str] = None
    lat: float
    lng: float
    crash_count: Optional[int] = None
    distance_from_route_km: Optional[float] = None


class SpeedCamerasOverlay(BaseModel):
    cameras_key: str
    polyline6: str
    algo_version: str
    created_at: str
    cameras: List[SpeedCamera] = Field(default_factory=list)
    road_occupancies: List[RoadOccupancy] = Field(default_factory=list)
    black_spots: List[RoadBlackSpot] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)


# ──────────────────────────────────────────────────────────────
# Public Toilets + Dump Points overlay (Dept of Health, CC BY 3.0 AU)
# ──────────────────────────────────────────────────────────────

class PublicToilet(BaseModel):
    id: str
    name: Optional[str] = None
    lat: float
    lng: float
    address: Optional[str] = None
    suburb: Optional[str] = None
    state: Optional[str] = None
    toilet_type: Optional[str] = None
    is_accessible: bool = False
    has_baby_change: bool = False
    has_drinking_water: bool = False
    has_shower: bool = False
    is_dump_point: bool = False
    key_required: bool = False
    is_fee: bool = False
    opening_hours: Optional[str] = None
    has_parking: bool = False
    distance_from_route_km: Optional[float] = None


class ToiletsOverlay(BaseModel):
    toilets_key: str
    polyline6: str
    algo_version: str
    created_at: str
    toilets: List[PublicToilet] = Field(default_factory=list)
    dump_points: List[PublicToilet] = Field(default_factory=list)
    attribution: str = "© Commonwealth of Australia (Department of Health)"
    warnings: List[str] = Field(default_factory=list)


# ──────────────────────────────────────────────────────────────
# School Zones overlay (TfNSW, CC BY 3.0 AU)
# ──────────────────────────────────────────────────────────────

class SchoolZone(BaseModel):
    id: str
    school_name: Optional[str] = None
    lat: float
    lng: float
    road: Optional[str] = None
    suburb: Optional[str] = None
    state: Optional[str] = None
    speed_limit_active_kmh: int = 40
    is_currently_active: bool = False
    active_session: Optional[str] = None  # "morning" | "afternoon"
    distance_from_route_km: Optional[float] = None


class SchoolZonesOverlay(BaseModel):
    school_zones_key: str
    polyline6: str
    algo_version: str
    created_at: str
    checked_at_local: Optional[str] = None  # AEST/AEDT time of active-hour check
    zones: List[SchoolZone] = Field(default_factory=list)
    active_count: int = 0
    attribution: str = "© Transport for NSW (CC BY 3.0 AU)"
    warnings: List[str] = Field(default_factory=list)


# ──────────────────────────────────────────────────────────────
# Roadkill hotspots overlay (NSW BioNet, CC BY 3.0 AU)
# ──────────────────────────────────────────────────────────────

class RoadkillHotspot(BaseModel):
    id: str
    lat: float
    lng: float
    observation_count: int
    risk_level: str  # "low" | "medium" | "high"
    species: List[str] = Field(default_factory=list)
    road: Optional[str] = None
    locality: Optional[str] = None
    distance_from_route_km: Optional[float] = None
    latest_sighting: Optional[str] = None


class RoadkillOverlay(BaseModel):
    roadkill_key: str
    polyline6: str
    algo_version: str
    created_at: str
    hotspots: List[RoadkillHotspot] = Field(default_factory=list)
    total_observations: int = 0
    coverage_note: str = "NSW only - data from NSW BioNet Animal Vehicle Strike dataset"
    attribution: str = "© NSW BioNet (CC BY 3.0 AU)"
    warnings: List[str] = Field(default_factory=list)


# ──────────────────────────────────────────────────────────────
# Presence (dead-reckoning proximity awareness)
# ──────────────────────────────────────────────────────────────

class PresencePingRequest(BaseModel):
    lat: float
    lng: float
    speed_kmh: float = 0.0
    heading_deg: float = 0.0

class PresencePingResponse(BaseModel):
    ok: bool = True

class NearbyRoamer(BaseModel):
    """A projected roamer position returned to the querying user."""
    user_id: str
    predicted_lat: float
    predicted_lng: float
    speed_kmh: float
    heading_deg: float
    last_pinged_at: str              # ISO8601 UTC
    predicted_at: str                # ISO8601 UTC - when prediction was computed
    distance_km: float               # from querying user's position
    confidence: Literal["high", "medium", "low"]  # degrades with Δt

class NearbyQuery(BaseModel):
    lat: float
    lng: float
    radius_km: float = 50.0         # default search radius

class NearbyResponse(BaseModel):
    roamers: List[NearbyRoamer] = Field(default_factory=list)


# ──────────────────────────────────────────────────────────────
# User Observations (crowd-sourced road intelligence)
# ──────────────────────────────────────────────────────────────

ObservationType = Literal[
    "road_condition",     # corrugated, pothole, washed_out, flooded, smooth
    "road_closure",       # road closed, gate locked, bridge out
    "hazard",             # fallen tree, animal on road, debris
    "fuel_price",         # observed fuel price at a station
    "speed_trap",         # speed camera / police speed check
    "weather",            # fog, dust storm, black ice, heavy rain
    "campsite",           # free camp condition, water available
    "general",            # anything else
]

ObservationSeverity = Literal["info", "caution", "warning", "danger"]


class UserObservation(BaseModel):
    id: str
    user_id: str
    type: ObservationType
    severity: ObservationSeverity = "info"
    lat: float
    lng: float
    heading_deg: Optional[float] = None
    message: Optional[str] = None
    value: Optional[str] = None      # e.g. "189.9" for fuel_price, "corrugated" for road_condition
    created_at: str                   # ISO8601 UTC
    expires_at: Optional[str] = None  # ISO8601 UTC - auto-expire after TTL


class ObservationSubmitRequest(BaseModel):
    type: ObservationType
    severity: ObservationSeverity = "info"
    lat: float
    lng: float
    heading_deg: Optional[float] = None
    message: Optional[str] = None
    value: Optional[str] = None


class ObservationSubmitResponse(BaseModel):
    id: str
    ok: bool = True


class NearbyObservationsQuery(BaseModel):
    lat: float
    lng: float
    radius_km: float = 50.0
    types: Optional[List[ObservationType]] = None
    since_iso: Optional[str] = None  # only obs newer than this


class AggregatedObservation(BaseModel):
    """Multiple user observations clustered into a single consensus point."""
    type: ObservationType
    severity: ObservationSeverity
    lat: float
    lng: float
    message: Optional[str] = None
    value: Optional[str] = None
    report_count: int = 1
    first_reported_at: str
    last_reported_at: str
    reporters: int = 1               # distinct users
    confidence: float = 0.5          # 0.0-1.0, higher = more trustworthy
    is_recent: bool = False          # last report within 30 minutes


class NearbyObservationsResponse(BaseModel):
    observations: List[AggregatedObservation] = Field(default_factory=list)


# ──────────────────────────────────────────────────────────────
# Peer Sync (overlay delta exchange between roamers)
# ──────────────────────────────────────────────────────────────

class PeerSyncRequest(BaseModel):
    """Request a delta of overlay data newer than the caller's timestamps."""
    lat: float
    lng: float
    radius_km: float = 200.0         # corridor radius to include data for
    overlay_timestamps: Dict[str, str] = Field(default_factory=dict)
    # e.g. {"traffic": "2026-03-16T10:00:00Z", "hazards": "2026-03-16T09:00:00Z"}


class PeerSyncDelta(BaseModel):
    """Delta payload: only overlay items newer than the caller's timestamps."""
    observations: List[AggregatedObservation] = Field(default_factory=list)
    traffic_events: List[TrafficEvent] = Field(default_factory=list)
    hazard_events: List[HazardEvent] = Field(default_factory=list)
    fuel_updates: List[FuelStation] = Field(default_factory=list)
    generated_at: str                 # ISO8601 UTC
