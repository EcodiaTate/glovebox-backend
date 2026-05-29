from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=None, extra="ignore")

    # Paths
    data_dir: str = Field(default="app/data", alias="DATA_DIR")
    cache_db_path: str = Field(default="app/data/roam_cache.db", alias="CACHE_DB_PATH")

    # Edges DB - Postgres+PostGIS (production). Takes priority over edges_db_path.
    edges_database_url: str | None = Field(default=None, alias="EDGES_DATABASE_URL")

    # Edges DB - SQLite fallback (local dev)
    edges_db_path: str = Field(
        default="app/data/edges_queensland.db", alias="EDGES_DB_PATH"
    )

    # OSRM
    osrm_base_url: str = Field(default="http://127.0.0.1:5000", alias="OSRM_BASE_URL")
    osrm_profile: str = Field(default="driving", alias="OSRM_PROFILE")
    mapbox_token: str = Field(default="", alias="ROAM_MAPBOX_TOKEN")
    mapbox_country: str = Field(default="au", alias="ROAM_MAPBOX_COUNTRY")
    mapbox_geocode_cache_seconds: int = Field(
        default=86400, alias="MAPBOX_GEOCODE_CACHE_SECONDS"
    )  # 24h

    # Versioning
    algo_version: str = Field(default="navpack.v1.osrm.mld", alias="ALGO_VERSION")
    corridor_algo_version: str = Field(
        default="corridor.v16.tree", alias="CORRIDOR_ALGO_VERSION"
    )
    places_algo_version: str = Field(
        default="places.v3.address.house_number", alias="PLACES_ALGO_VERSION"
    )

    # Corridor defaults
    corridor_buffer_m_default: int = Field(
        default=5000, alias="CORRIDOR_BUFFER_M_DEFAULT"
    )
    corridor_max_edges_default: int = Field(
        default=2000000, alias="CORRIDOR_MAX_EDGES_DEFAULT"
    )

    # Places (Overpass)
    overpass_url: str = Field(
        default="https://overpass-api.de/api/interpreter", alias="OVERPASS_URL"
    )
    overpass_fallback_urls: list[str] = Field(
        default=[
            "https://overpass.kumi.systems/api/interpreter",
            "https://overpass.private.coffee/api/interpreter",
        ],
        alias="OVERPASS_FALLBACK_URLS",
    )
    overpass_timeout_s: int = Field(default=25, alias="OVERPASS_TIMEOUT_S")
    overpass_throttle_s: float = Field(default=0.2, alias="OVERPASS_THROTTLE_S")
    overpass_retries: int = Field(default=4, alias="OVERPASS_RETRIES")
    overpass_retry_base_s: float = Field(default=0.75, alias="OVERPASS_RETRY_BASE_S")

    # Places engine controls
    places_tile_step_deg: float = Field(default=0.15, alias="PLACES_TILE_STEP_DEG")
    places_max_tiles: int = Field(default=64, alias="PLACES_MAX_TILES")
    places_hard_cap: int = Field(default=12000, alias="PLACES_HARD_CAP")
    places_local_satisfy_ratio: float = Field(
        default=0.70, alias="PLACES_LOCAL_SATISFY_RATIO"
    )
    places_tile_ttl_s: int = Field(
        default=60 * 60 * 24 * 14, alias="PLACES_TILE_TTL_S"
    )  # 14d
    places_time_budget_s: float = Field(default=10.0, alias="PLACES_TIME_BUDGET_S")
    places_max_overpass_tiles_per_req: int = Field(
        default=12, alias="PLACES_MAX_OVERPASS_TILES_PER_REQ"
    )

    # Supabase
    supa_url: str | None = Field(default=None, alias="SUPA_URL")
    supa_service_role_key: str | None = Field(
        default=None, alias="SUPA_SERVICE_ROLE_KEY"
    )
    supa_bucket: str = Field(default="roam-bundles", alias="SUPA_BUCKET")
    supa_enabled: bool = Field(default=False, alias="SUPA_ENABLED")

    # ──────────────────────────────────────────────────────────────
    # Overlays: Traffic + Hazards - shared config
    # ──────────────────────────────────────────────────────────────

    traffic_algo_version: str = Field(
        default="traffic.v4.multistate+wa_incidents",
        alias="TRAFFIC_ALGO_VERSION",
    )
    hazards_algo_version: str = Field(
        default="hazards.v6.multistate.cap.radar+parks_all",
        alias="HAZARDS_ALGO_VERSION",
    )

    overlays_cache_seconds: int = Field(default=600, alias="OVERLAYS_CACHE_SECONDS")
    overlays_timeout_s: float = Field(default=15.0, alias="OVERLAYS_TIMEOUT_S")

    # ──────────────────────────────────────────────────────────────
    # QLD Traffic (official v2 events + delta merge)
    # ──────────────────────────────────────────────────────────────

    qldtraffic_api_key: str = Field(default="", alias="QLDTRAFFIC_API_KEY")

    qldtraffic_events_url: str = Field(
        default="https://api.qldtraffic.qld.gov.au/v2/events",
        alias="QLDTRAFFIC_EVENTS_URL",
    )
    qldtraffic_events_delta_url: str = Field(
        default="https://api.qldtraffic.qld.gov.au/v2/events/past-one-hour",
        alias="QLDTRAFFIC_EVENTS_DELTA_URL",
    )

    qldtraffic_cache_seconds: int = Field(default=60, alias="QLDTRAFFIC_CACHE_SECONDS")
    qldtraffic_full_refresh_seconds: int = Field(
        default=900, alias="QLDTRAFFIC_FULL_REFRESH_SECONDS"
    )

    traffic_include_past_hours: int = Field(
        default=6, alias="NAV_TRAFFIC_INCLUDE_PAST_HOURS"
    )

    # Back-compat (optional QLD GeoJSON feed URLs)
    qldtraffic_incidents_url: str | None = Field(
        default=None, alias="QLDTRAFFIC_INCIDENTS_URL"
    )
    qldtraffic_roadworks_url: str | None = Field(
        default=None, alias="QLDTRAFFIC_ROADWORKS_URL"
    )
    qldtraffic_closures_url: str | None = Field(
        default=None, alias="QLDTRAFFIC_CLOSURES_URL"
    )
    qldtraffic_flooding_url: str | None = Field(
        default=None, alias="QLDTRAFFIC_FLOODING_URL"
    )

    # ──────────────────────────────────────────────────────────────
    # NSW Traffic - Live Traffic NSW (TfNSW Open Data)
    # GeoJSON feeds at api.transport.nsw.gov.au/v1/live/hazards/{type}
    # Types: incidents, fires, floods, alpine, roadworks, majorevent, planned
    # Auth: Authorization: apikey {key}
    # ──────────────────────────────────────────────────────────────

    # DISABLED: All api.transport.nsw.gov.au/v1/live/hazards/* endpoints
    # return 404 as of March 2026. Disable to avoid wasted requests that
    # contribute to Overpass 429 rate-limiting.
    nsw_traffic_enabled: bool = Field(default=False, alias="NSW_TRAFFIC_ENABLED")
    nsw_traffic_api_key: str = Field(default="", alias="NSW_TRAFFIC_API_KEY")
    nsw_traffic_base_url: str = Field(
        default="https://api.transport.nsw.gov.au/v1/live/hazards",
        alias="NSW_TRAFFIC_BASE_URL",
    )
    # Which hazard feeds to query (valid types: incidents, fires, roadworks, majorevent)
    # floods, alpine, planned return 404 from this API
    nsw_traffic_feeds: str = Field(
        default="incidents,fires,roadworks,majorevent",
        alias="NSW_TRAFFIC_FEEDS",
    )

    # ──────────────────────────────────────────────────────────────
    # VIC Traffic - VicRoads Data Exchange
    # JSON API at data-exchange.vicroads.vic.gov.au
    # Auth: KeyID header
    # ──────────────────────────────────────────────────────────────

    vic_traffic_enabled: bool = Field(default=True, alias="VIC_TRAFFIC_ENABLED")
    vic_traffic_api_key: str = Field(default="", alias="VIC_TRAFFIC_API_KEY")
    vic_traffic_unplanned_url: str = Field(
        default="https://data-exchange.vicroads.vic.gov.au/opendata/v2/unplanneddisruptions",
        alias="VIC_TRAFFIC_UNPLANNED_URL",
    )
    vic_traffic_planned_url: str = Field(
        default="https://data-exchange.vicroads.vic.gov.au/opendata/v1/planneddisruptions",
        alias="VIC_TRAFFIC_PLANNED_URL",
    )
    vic_traffic_closures_url: str = Field(
        default="https://data-exchange.vicroads.vic.gov.au/opendata/v1/emergencyroadclosures",
        alias="VIC_TRAFFIC_CLOSURES_URL",
    )

    # ──────────────────────────────────────────────────────────────
    # SA Traffic - SA GeoHub ArcGIS (trafficdata.geohub.sa.gov.au)
    # Replaces the dead data.sa.gov.au GeoJSON feed (404 as of Feb 2026).
    # Uses ArcGIS FeatureServer query endpoint; returns outSR=4326 WGS84.
    # ──────────────────────────────────────────────────────────────

    sa_traffic_enabled: bool = Field(default=True, alias="SA_TRAFFIC_ENABLED")
    sa_traffic_geohub_url: str = Field(
        default="https://trafficdata.geohub.sa.gov.au/MapServer/5/query",
        alias="SA_TRAFFIC_GEOHUB_URL",
    )

    # ──────────────────────────────────────────────────────────────
    # WA Traffic - Main Roads WA ArcGIS GeoJSON (CC-BY 4.0)
    # Road incidents via ArcGIS FeatureServer query endpoint.
    # No auth required. Returns GeoJSON FeatureCollection.
    # ──────────────────────────────────────────────────────────────

    wa_traffic_enabled: bool = Field(default=True, alias="WA_TRAFFIC_ENABLED")
    wa_traffic_arcgis_url: str = Field(
        default=(
            "https://services2.arcgis.com/cHGEnmsJ165IBJRM/arcgis/rest/services/"
            "WebEoc_RoadIncidents/FeatureServer/1/query"
            "?where=1%3D1&outFields=*&f=geojson"
        ),
        alias="WA_TRAFFIC_ARCGIS_URL",
    )

    # ──────────────────────────────────────────────────────────────
    # NT Traffic - NT Road Report (roadreport.nt.gov.au)
    # JSON array of obstructions with start/end coordinates.
    # No auth required. Also doubles as outback road conditions overlay.
    # ──────────────────────────────────────────────────────────────

    nt_traffic_enabled: bool = Field(default=True, alias="NT_TRAFFIC_ENABLED")
    nt_road_report_url: str = Field(
        default="https://roadreport.nt.gov.au/api/Obstruction/GetAll",
        alias="NT_ROAD_REPORT_URL",
    )
    nt_emergency_announcements_url: str = Field(
        default="https://roadreport.nt.gov.au/api/Announcement/GetEmergencyAnnouncements",
        alias="NT_EMERGENCY_ANNOUNCEMENTS_URL",
    )
    nt_map_icons_url: str = Field(
        default="https://roadreport.nt.gov.au/api/MapIcon/GetAll",
        alias="NT_MAP_ICONS_URL",
    )

    # ──────────────────────────────────────────────────────────────
    # Hazards feeds - BOM per-state RSS warnings (national coverage)
    # These are XML RSS feeds from the Bureau of Meteorology.
    # No auth required. Updated every few minutes.
    # ──────────────────────────────────────────────────────────────

    hazards_enable_bom_rss: bool = Field(default=True, alias="HAZARDS_ENABLE_BOM_RSS")

    bom_rss_qld_url: str = Field(
        default="https://www.bom.gov.au/fwo/IDZ00056.warnings_qld.xml",
        alias="BOM_RSS_QLD_URL",
    )
    bom_rss_nsw_url: str = Field(
        default="https://www.bom.gov.au/fwo/IDZ00054.warnings_nsw.xml",
        alias="BOM_RSS_NSW_URL",
    )
    bom_rss_vic_url: str = Field(
        default="https://www.bom.gov.au/fwo/IDZ00059.warnings_vic.xml",
        alias="BOM_RSS_VIC_URL",
    )
    bom_rss_sa_url: str = Field(
        default="https://www.bom.gov.au/fwo/IDZ00057.warnings_sa.xml",
        alias="BOM_RSS_SA_URL",
    )
    bom_rss_wa_url: str = Field(
        default="https://www.bom.gov.au/fwo/IDZ00058.warnings_wa.xml",
        alias="BOM_RSS_WA_URL",
    )
    bom_rss_nt_url: str = Field(
        default="https://www.bom.gov.au/fwo/IDZ00055.warnings_nt.xml",
        alias="BOM_RSS_NT_URL",
    )
    bom_rss_tas_url: str = Field(
        default="https://www.bom.gov.au/fwo/IDZ00060.warnings_tas.xml",
        alias="BOM_RSS_TAS_URL",
    )

    # ──────────────────────────────────────────────────────────────
    # CAP feeds - per-state emergency alerting (CAP-AU format)
    # ──────────────────────────────────────────────────────────────

    # QLD CAP feeds (existing)
    qld_disaster_cap_url: str = Field(
        default="https://publiccontent-qld-alerts.s3.ap-southeast-2.amazonaws.com/content/Feeds/StormFloodCycloneWarnings/StormWarnings_capau.xml",
        alias="QLD_DISASTER_CAP_URL",
    )
    qld_emergency_alerts_url: str = Field(
        default="https://publiccontent-qld-alerts.s3.ap-southeast-2.amazonaws.com/content/Feeds/QLDEmergencyAlerts/QLDEmergencyAlerts.xml",
        alias="QLD_EMERGENCY_ALERTS_URL",
    )

    # NSW emergency feeds
    # NOTE: NSW SES warnings XML confirmed 404/dead - removed.
    nsw_rfs_fires_url: str = Field(
        default="https://www.rfs.nsw.gov.au/feeds/majorIncidents.json",
        alias="NSW_RFS_FIRES_URL",
    )

    # VIC emergency feeds
    vic_emergency_url: str = Field(
        default="https://data.emergency.vic.gov.au/Show?pageId=getIncidentJSON",
        alias="VIC_EMERGENCY_URL",
    )

    # SA emergency feeds
    sa_cfs_url: str = Field(
        default="https://data.eso.sa.gov.au/prod/cfs/criimson/cfs_current_incidents.json",
        alias="SA_CFS_URL",
    )

    # ──────────────────────────────────────────────────────────────
    # WA DFES emergency feeds - api.emergency.wa.gov.au/v1/
    # Confirmed working: incidents + warnings endpoints.
    # No auth required.
    # ──────────────────────────────────────────────────────────────

    wa_dfes_enabled: bool = Field(default=True, alias="WA_DFES_ENABLED")
    wa_dfes_base_url: str = Field(
        default="https://api.emergency.wa.gov.au/v1",
        alias="WA_DFES_BASE_URL",
    )
    wa_dfes_feeds: str = Field(
        default="incidents,warnings",
        alias="WA_DFES_FEEDS",
    )

    # ──────────────────────────────────────────────────────────────
    # National DEA Fire Hotspots - satellite detection (CC-BY 4.0)
    # Geoscience Australia Digital Earth Australia.
    # GeoJSON FeatureCollection of all recent satellite-detected hotspots.
    # Covers ALL Australian states via MODIS, HIMAWARI-9, VIIRS, AQUA.
    # No auth required.
    # ──────────────────────────────────────────────────────────────

    dea_hotspots_enabled: bool = Field(default=True, alias="DEA_HOTSPOTS_ENABLED")
    dea_hotspots_url: str = Field(
        default="https://hotspots.dea.ga.gov.au/data/recent-hotspots.json",
        alias="DEA_HOTSPOTS_URL",
    )
    dea_hotspots_min_confidence: int = Field(
        default=50,
        alias="DEA_HOTSPOTS_MIN_CONFIDENCE",
    )
    dea_hotspots_max_hours: int = Field(
        default=72,
        alias="DEA_HOTSPOTS_MAX_HOURS",
    )

    # ──────────────────────────────────────────────────────────────
    # TAS Hazards - TheList ArcGIS (public, no auth)
    # Emergency Management layer from services.thelist.tas.gov.au.
    # ArcGIS JSON format (NOT GeoJSON - uses {"x": lng, "y": lat}).
    # ──────────────────────────────────────────────────────────────

    tas_hazards_enabled: bool = Field(default=True, alias="TAS_HAZARDS_ENABLED")
    tas_thelist_url: str = Field(
        default=(
            "https://services.thelist.tas.gov.au/arcgis/rest/services/Public/"
            "EmergencyManagementPublic/MapServer/72/query"
            "?where=1%3D1&outFields=*&f=json"
        ),
        alias="TAS_THELIST_URL",
    )

    # ──────────────────────────────────────────────────────────────
    # National: RADAR Roadworks / Closures (federal, all states)
    # ArcGIS FeatureServer - status='Active', outSR=4326
    # ──────────────────────────────────────────────────────────────

    radar_roadworks_enabled: bool = Field(default=True, alias="RADAR_ROADWORKS_ENABLED")
    radar_roadworks_url: str = Field(
        default=(
            "https://spatial.infrastructure.gov.au/server/rest/services/Hosted/"
            "RADAR_Curated_Prod_roadworks/FeatureServer/0/query"
        ),
        alias="RADAR_ROADWORKS_URL",
    )

    # ──────────────────────────────────────────────────────────────
    # State Park Alerts - QLD Parks, NSW NPWS, WA DBCA,
    #                     VIC Parks Victoria, SA DEW, NT Parks, TAS PWS
    # RSS 2.0 feeds for park closures and access alerts.
    # WA DBCA off by default (JSON endpoint not confirmed).
    # VIC/SA/NT/TAS off by default - no RSS endpoint confirmed reachable
    # as of March 2026; set enabled=True once a working URL is found.
    # ──────────────────────────────────────────────────────────────

    parks_qld_alerts_enabled: bool = Field(
        default=True, alias="PARKS_QLD_ALERTS_ENABLED"
    )
    parks_qld_alerts_url: str = Field(
        default="https://parks.qld.gov.au/xml/rss/parkalerts.xml",
        alias="PARKS_QLD_ALERTS_URL",
    )
    parks_nsw_alerts_enabled: bool = Field(
        default=True, alias="PARKS_NSW_ALERTS_ENABLED"
    )
    parks_nsw_alerts_url: str = Field(
        default="https://www.nationalparks.nsw.gov.au/api/rssfeed/get",
        alias="PARKS_NSW_ALERTS_URL",
    )
    parks_wa_alerts_enabled: bool = Field(
        default=False, alias="PARKS_WA_ALERTS_ENABLED"
    )
    parks_wa_alerts_url: str = Field(
        default="https://alerts.dbca.wa.gov.au/Home/map?atype=park-road-closures%2Cpark-closures%2Cpark-notification",
        alias="PARKS_WA_ALERTS_URL",
    )
    # VIC - Parks Victoria RSS: no reachable endpoint confirmed (checked Mar 2026).
    # parks.vic.gov.au/get-into-nature/park-alerts/rss returns HTML; API host refused.
    parks_vic_alerts_enabled: bool = Field(
        default=False, alias="PARKS_VIC_ALERTS_ENABLED"
    )
    parks_vic_alerts_url: str = Field(
        default="https://www.parks.vic.gov.au/get-into-nature/park-alerts/rss",
        alias="PARKS_VIC_ALERTS_URL",
    )
    # SA - DEW / parks.sa.gov.au RSS: /alerts/rss returns 404 (checked Mar 2026).
    parks_sa_alerts_enabled: bool = Field(
        default=False, alias="PARKS_SA_ALERTS_ENABLED"
    )
    parks_sa_alerts_url: str = Field(
        default="https://www.parks.sa.gov.au/alerts/rss",
        alias="PARKS_SA_ALERTS_URL",
    )
    # NT - Parks and Wildlife RSS: nt.gov.au and parksandwildlife.nt.gov.au
    # both return 404 for RSS paths (checked Mar 2026).
    parks_nt_alerts_enabled: bool = Field(
        default=False, alias="PARKS_NT_ALERTS_ENABLED"
    )
    parks_nt_alerts_url: str = Field(
        default="https://nt.gov.au/leisure/parks-reserves/park-alerts/rss",
        alias="PARKS_NT_ALERTS_URL",
    )
    # TAS - Parks and Wildlife Service RSS: parks.tas.gov.au RSS paths return
    # 404 (checked Mar 2026).
    parks_tas_alerts_enabled: bool = Field(
        default=False, alias="PARKS_TAS_ALERTS_ENABLED"
    )
    parks_tas_alerts_url: str = Field(
        default="https://parks.tas.gov.au/explore-our-parks/park-alerts/rss",
        alias="PARKS_TAS_ALERTS_URL",
    )

    # ──────────────────────────────────────────────────────────────
    # TAS Direct Alert Feed - TasALERT (pending email permission)
    # Richer data at alert.tas.gov.au but requires emailing
    # info@alert.tas.gov.au for API access.
    # Uncomment when permission is granted.
    # ──────────────────────────────────────────────────────────────

    # tas_alert_direct_enabled: bool = Field(default=False, alias="TAS_ALERT_DIRECT_ENABLED")
    # tas_alert_direct_url: str = Field(
    #     default="https://alert.tas.gov.au/feeds/alerts.json",
    #     alias="TAS_ALERT_DIRECT_URL",
    # )

    # ──────────────────────────────────────────────────────────────
    # Guide (LLM) - DeepSeek
    # Uses DeepSeek's OpenAI-compatible /chat/completions API.
    # deepseek-chat = DeepSeek-V3 (best quality, fast, cheap)
    # deepseek-reasoner = DeepSeek-R1 (slower, for complex reasoning)
    # ──────────────────────────────────────────────────────────────
    # Provider selection - "anthropic" (default, Claude Sonnet), "deepseek",
    # "openai", or "gemini". Non-anthropic providers speak the OpenAI-compatible
    # /chat/completions shape; anthropic uses the native Messages API with a
    # forced structured-output tool. If the selected provider has no key set,
    # GuideService falls back to whichever provider does.
    guide_provider: str = Field(default="anthropic", alias="GUIDE_PROVIDER")

    anthropic_api_key: str = Field(default="", alias="ANTHROPIC_API_KEY")
    anthropic_model: str = Field(default="claude-sonnet-4-6", alias="ANTHROPIC_MODEL")
    anthropic_base_url: str = Field(
        default="https://api.anthropic.com/v1", alias="ANTHROPIC_BASE_URL"
    )
    anthropic_version: str = Field(default="2023-06-01", alias="ANTHROPIC_VERSION")

    openai_api_key: str = Field(default="", alias="OPENAI_API_KEY")
    openai_model: str = Field(default="gpt-5.1", alias="OPENAI_MODEL")
    openai_base_url: str = Field(
        default="https://api.openai.com/v1", alias="OPENAI_BASE_URL"
    )

    gemini_api_key: str = Field(default="", alias="GEMINI_API_KEY")
    gemini_model: str = Field(default="gemini-3-pro", alias="GEMINI_MODEL")
    gemini_base_url: str = Field(
        default="https://generativelanguage.googleapis.com/v1beta/openai",
        alias="GEMINI_BASE_URL",
    )

    deepseek_api_key: str = Field(default="", alias="DEEPSEEK_API_KEY")
    deepseek_model: str = Field(default="deepseek-chat", alias="DEEPSEEK_MODEL")
    deepseek_base_url: str = Field(
        default="https://api.deepseek.com/v1", alias="DEEPSEEK_BASE_URL"
    )
    guide_max_steps: int = Field(default=4, alias="GUIDE_MAX_STEPS")
    guide_timeout_s: float = Field(default=90.0, alias="GUIDE_TIMEOUT_S")
    guide_temperature: float = Field(default=0.7, alias="GUIDE_TEMPERATURE")
    guide_max_output_tokens: int = Field(default=4000, alias="GUIDE_MAX_OUTPUT_TOKENS")

    # ──────────────────────────────────────────────────────────────
    # ──────────────────────────────────────────────────────────────
    # Stripe
    # ──────────────────────────────────────────────────────────────
    stripe_secret_key: str = Field(default="", alias="STRIPE_SECRET_KEY")
    stripe_webhook_secret: str = Field(default="", alias="STRIPE_WEBHOOK_SECRET")
    stripe_price_id: str = Field(default="", alias="STRIPE_PRICE_ID")

    # ──────────────────────────────────────────────────────────────
    # RevenueCat webhook
    # ──────────────────────────────────────────────────────────────
    revenuecat_webhook_secret: str = Field(
        default="", alias="REVENUECAT_WEBHOOK_SECRET"
    )

    # ──────────────────────────────────────────────────────────────
    # Weather overlay - Open-Meteo BOM ACCESS-G (self-hosted or public)
    # Self-hosted: set OPEN_METEO_BASE_URL to your instance (e.g. http://localhost:8080).
    # Self-hosted Open-Meteo (AGPLv3 engine) syncing ECMWF IFS 0.25° model.
    # Covers Australia at ~25km resolution. CC-BY 4.0 data attribution.
    # ──────────────────────────────────────────────────────────────
    weather_algo_version: str = Field(
        default="weather.v2.openmeteo.ecmwf_ifs025",
        alias="WEATHER_ALGO_VERSION",
    )
    weather_cache_seconds: int = Field(default=3600, alias="WEATHER_CACHE_SECONDS")
    weather_sample_interval_km: float = Field(
        default=50.0, alias="WEATHER_SAMPLE_INTERVAL_KM"
    )
    open_meteo_base_url: str = Field(
        default="https://api.open-meteo.com",
        alias="OPEN_METEO_BASE_URL",
    )
    open_meteo_api_key: str = Field(
        default="",
        alias="OPEN_METEO_API_KEY",
    )

    # ──────────────────────────────────────────────────────────────
    # Fuel overlay - NSW FuelCheck, WA FuelWatch, Open Charge Map
    # ──────────────────────────────────────────────────────────────

    fuel_algo_version: str = Field(default="fuel.v4.gov", alias="FUEL_ALGO_VERSION")
    fuel_cache_seconds: int = Field(default=1800, alias="FUEL_CACHE_SECONDS")  # 30min

    # NSW FuelCheck - https://api.onegov.nsw.gov.au/ (Swagger: /api/swagger/spec/22)
    # Migrated from api.nsw.gov.au (dead, 404) to api.onegov.nsw.gov.au in 2025/2026.
    # Auth: OAuth2 client_credentials - POST /oauth/client_credential/accesstoken
    #   with Basic(api_key:api_secret) → Bearer token.
    # V2 endpoints cover NSW + TAS.
    nsw_fuel_enabled: bool = Field(default=True, alias="NSW_FUEL_ENABLED")
    nsw_fuel_api_key: str = Field(default="", alias="NSW_FUEL_API_KEY")
    nsw_fuel_api_secret: str = Field(default="", alias="NSW_FUEL_API_SECRET")
    nsw_fuel_base_url: str = Field(
        default="https://api.onegov.nsw.gov.au",
        alias="NSW_FUEL_BASE_URL",
    )

    # WA FuelWatch RSS - https://www.fuelwatch.wa.gov.au/fuelwatch/fuelWatchRSS
    # Free, no auth required. Covers all of WA including remote outback.
    wa_fuel_enabled: bool = Field(default=True, alias="WA_FUEL_ENABLED")
    wa_fuelwatch_rss_url: str = Field(
        default="https://www.fuelwatch.wa.gov.au/fuelwatch/fuelWatchRSS",
        alias="WA_FUELWATCH_RSS_URL",
    )

    # Open Charge Map - https://api.openchargemap.io/v3/
    openchargemap_enabled: bool = Field(default=True, alias="OPENCHARGEMAP_ENABLED")
    openchargemap_api_key: str = Field(default="", alias="OPENCHARGEMAP_API_KEY")

    # PetrolSpy - DISABLED. IP concerns with scraping their internal webservice.
    # Kept as dead code for reference. Use NSW FuelCheck + WA FuelWatch instead.
    petrolspy_enabled: bool = Field(default=False, alias="PETROLSPY_ENABLED")

    # QLD Fuel Price Reporting - https://www.fuelpricesqld.com.au
    # Register at fuelpricesqld.com.au to obtain API token. Operated by Informed Sources.
    qld_fuel_enabled: bool = Field(default=False, alias="QLD_FUEL_ENABLED")
    qld_fuel_api_token: str = Field(default="", alias="QLD_FUEL_API_TOKEN")

    # VIC Servo Saver Public API - https://service.vic.gov.au
    # Apply for API Consumer ID at service.vic.gov.au. 24-hour data delay.
    vic_fuel_enabled: bool = Field(default=False, alias="VIC_FUEL_ENABLED")
    vic_fuel_consumer_id: str = Field(default="", alias="VIC_FUEL_CONSUMER_ID")

    # SA Fuel Pricing - via CBS / Informed Sources aggregator
    # Register as data publisher with Consumer and Business Services (CBS).
    sa_fuel_enabled: bool = Field(default=False, alias="SA_FUEL_ENABLED")
    sa_fuel_api_token: str = Field(default="", alias="SA_FUEL_API_TOKEN")

    # ──────────────────────────────────────────────────────────────
    # Flood gauge overlay - BOM KiWIS
    # Station list from bom.gov.au/waterdata (~8000 stations nationally).
    # Real-time readings via BOM KiWIS API (no auth required).
    # Attribution: DATA_OWNER_NAME must be displayed per station.
    # ──────────────────────────────────────────────────────────────

    flood_algo_version: str = Field(
        default="flood.v2.bom.kiwis+catchments+shapely", alias="FLOOD_ALGO_VERSION"
    )
    flood_cache_seconds: int = Field(default=1800, alias="FLOOD_CACHE_SECONDS")  # 30min
    flood_enabled: bool = Field(default=True, alias="FLOOD_ENABLED")
    flood_station_refresh_hours: int = Field(
        default=24, alias="FLOOD_STATION_REFRESH_HOURS"
    )
    bom_kiwis_base_url: str = Field(
        default="http://www.bom.gov.au/waterdata/services", alias="BOM_KIWIS_BASE_URL"
    )
    bom_station_data_url: str = Field(
        default="https://www.bom.gov.au/waterdata/data/stationdata.json",
        alias="BOM_STATION_DATA_URL",
    )
    bom_flood_catchments_url: str = Field(
        default="https://hosting.wsapi.cloud.bom.gov.au/arcgis/rest/services/flood/National_Flood_Gauge_Network/FeatureServer",
        alias="BOM_FLOOD_CATCHMENTS_URL",
    )

    # ──────────────────────────────────────────────────────────────
    # Rest Areas + Fatigue Management overlay (Overpass, static data)
    # 24h cache TTL - rest areas rarely change
    # ──────────────────────────────────────────────────────────────
    rest_algo_version: str = Field(
        default="rest_areas.v3.overpass+qld+wa+nsw", alias="REST_ALGO_VERSION"
    )
    rest_cache_seconds: int = Field(default=86400, alias="REST_CACHE_SECONDS")
    fatigue_max_gap_km: float = Field(default=180.0, alias="FATIGUE_MAX_GAP_KM")
    fatigue_rest_interval_km: float = Field(
        default=180.0, alias="FATIGUE_REST_INTERVAL_KM"
    )

    # NSW TfNSW Rest Areas (requires Open Data API key)
    # DISABLED: api.transport.nsw.gov.au returns 404 on spatial endpoint (Mar 2026)
    nsw_rest_areas_enabled: bool = Field(default=False, alias="NSW_REST_AREAS_ENABLED")
    nsw_rest_areas_api_key: str = Field(default="", alias="NSW_REST_AREAS_API_KEY")
    nsw_rest_areas_url: str = Field(
        default="https://api.transport.nsw.gov.au/v1/roads/spatial",
        alias="NSW_REST_AREAS_URL",
    )

    # ──────────────────────────────────────────────────────────────
    # Mobile Coverage overlay - OpenCelliD bulk CSV (MCC 505 = Australia)
    # Bulk download updated daily; cache for 24h (towers rarely move).
    # No API key required for the free bulk download tier.
    # ──────────────────────────────────────────────────────────────
    coverage_algo_version: str = Field(
        default="coverage.v1.opencellid", alias="COVERAGE_ALGO_VERSION"
    )
    coverage_cache_seconds: int = Field(
        default=86400, alias="COVERAGE_CACHE_SECONDS"
    )  # 24h
    coverage_enabled: bool = Field(default=True, alias="COVERAGE_ENABLED")
    opencellid_token: str = Field(default="", alias="OPENCELLID_TOKEN")
    opencellid_download_url: str = Field(
        default="https://opencellid.org/ocid/downloads?token={token}&type=mcc&file=505.csv.gz",
        alias="OPENCELLID_DOWNLOAD_URL",
    )
    coverage_no_signal_gap_km: float = Field(
        default=50.0, alias="COVERAGE_NO_SIGNAL_GAP_KM"
    )
    opencellid_local_db_path: str = Field(
        default="data/celltowers/505.csv.gz", alias="OPENCELLID_LOCAL_DB_PATH"
    )

    # Guide Web Search
    # Gives the guide live web search so it can answer about current
    # events, road conditions, new businesses, etc.
    # Tavily (tavily.com) is designed for LLM consumption - returns
    # clean extracted text, not HTML. Free tier: 1000 searches/month.
    # Google CSE is the fallback. Set provider to "none" to disable.
    # ──────────────────────────────────────────────────────────────
    guide_search_provider: str = Field(default="tavily", alias="GUIDE_SEARCH_PROVIDER")
    tavily_api_key: str = Field(default="", alias="TAVILY_API_KEY")
    tavily_max_results: int = Field(default=5, alias="TAVILY_MAX_RESULTS")
    google_cse_api_key: str = Field(default="", alias="GOOGLE_CSE_API_KEY")
    google_cse_cx: str = Field(default="", alias="GOOGLE_CSE_CX")
    guide_search_timeout_s: float = Field(default=10.0, alias="GUIDE_SEARCH_TIMEOUT_S")

    # ──────────────────────────────────────────────────────────────
    # Wildlife Hazard Overlay - iNaturalist Node API v1
    # Commercial-use CC licenses (cc0, cc-by) enforced at query time.
    # Rate limit: 60 req/min (iNaturalist public API cap).
    # ──────────────────────────────────────────────────────────────
    wildlife_algo_version: str = Field(
        default="wildlife.v2.inaturalist.cc", alias="WILDLIFE_ALGO_VERSION"
    )
    wildlife_cache_seconds: int = Field(
        default=604800, alias="WILDLIFE_CACHE_SECONDS"
    )  # 7 days
    wildlife_enabled: bool = Field(default=True, alias="WILDLIFE_ENABLED")
    wildlife_sample_interval_km: float = Field(
        default=50.0, alias="WILDLIFE_SAMPLE_INTERVAL_KM"
    )
    wildlife_radius_km: float = Field(default=25.0, alias="WILDLIFE_RADIUS_KM")
    wildlife_per_page: int = Field(default=50, alias="WILDLIFE_PER_PAGE")
    wildlife_rate_per_min: int = Field(default=60, alias="WILDLIFE_RATE_PER_MIN")
    wildlife_timeout_s: float = Field(default=15.0, alias="WILDLIFE_TIMEOUT_S")
    wildlife_photo_size: str = Field(default="medium", alias="WILDLIFE_PHOTO_SIZE")
    # High-occurrence threshold for "high" risk classification
    wildlife_high_risk_count: int = Field(default=10, alias="WILDLIFE_HIGH_RISK_COUNT")
    wildlife_medium_risk_count: int = Field(
        default=3, alias="WILDLIFE_MEDIUM_RISK_COUNT"
    )

    # ──────────────────────────────────────────────────────────────
    # Bushfire Overlay - NSW RFS + NASA FIRMS
    # NSW RFS: free, no auth. FIRMS: free, requires API key from
    # https://firms.modaps.eosdis.nasa.gov/api/area/
    # Cache TTL 15 min - fires are time-critical.
    # ──────────────────────────────────────────────────────────────
    firms_map_key: str = Field(default="", alias="FIRMS_MAP_KEY")

    # ──────────────────────────────────────────────────────────────
    # Emergency Services overlay - GA Emergency Management Facilities
    # ArcGIS MapServer (CC-BY 4.0, no auth required).
    # Layers: 0=Ambulance, 1=Other, 2=Police, 3=Metro Fire,
    #         4=Rural Fire, 5=SES
    # ──────────────────────────────────────────────────────────────
    emergency_algo_version: str = Field(
        default="emergency.v1.ga.facilities",
        alias="EMERGENCY_ALGO_VERSION",
    )
    emergency_cache_seconds: int = Field(
        default=86400, alias="EMERGENCY_CACHE_SECONDS"
    )  # 24h
    emergency_enabled: bool = Field(default=True, alias="EMERGENCY_ENABLED")
    ga_emergency_base_url: str = Field(
        default="http://services.ga.gov.au/gis/rest/services/Emergency_Management_Facilities/MapServer",
        alias="GA_EMERGENCY_BASE_URL",
    )

    # ──────────────────────────────────────────────────────────────
    # Heritage & Protected Areas overlay - DCCEEW GIS services
    # ArcGIS MapServer (CC-BY 3.0 AU, no auth required).
    # World Heritage, National Heritage, Commonwealth Heritage, CAPAD.
    # ──────────────────────────────────────────────────────────────
    heritage_algo_version: str = Field(
        default="heritage.v1.dcceew.capad",
        alias="HERITAGE_ALGO_VERSION",
    )
    heritage_cache_seconds: int = Field(
        default=604800, alias="HERITAGE_CACHE_SECONDS"
    )  # 7 days
    heritage_enabled: bool = Field(default=True, alias="HERITAGE_ENABLED")
    dcceew_gis_base_url: str = Field(
        default="https://gis.environment.gov.au/gispubmap/rest/services/ogc_services",
        alias="DCCEEW_GIS_BASE_URL",
    )

    # ──────────────────────────────────────────────────────────────
    # Air Quality overlay - OpenWeatherMap Air Pollution API
    # Free tier: 1,000,000 calls/month. Requires free API key.
    # ──────────────────────────────────────────────────────────────
    aqi_algo_version: str = Field(
        default="aqi.v1.owm",
        alias="AQI_ALGO_VERSION",
    )
    aqi_cache_seconds: int = Field(default=3600, alias="AQI_CACHE_SECONDS")  # 1h
    aqi_enabled: bool = Field(default=True, alias="AQI_ENABLED")
    owm_api_key: str = Field(default="", alias="OWM_API_KEY")
    aqi_sample_interval_km: float = Field(default=50.0, alias="AQI_SAMPLE_INTERVAL_KM")

    # ──────────────────────────────────────────────────────────────
    # Bushfire overlay - NSW RFS + NASA FIRMS
    # NSW RFS: free, no auth. FIRMS: free, requires MAP_KEY.
    # ──────────────────────────────────────────────────────────────
    bushfire_algo_version: str = Field(
        default="bushfire.v1.rfs+firms",
        alias="BUSHFIRE_ALGO_VERSION",
    )
    bushfire_cache_seconds: int = Field(
        default=900, alias="BUSHFIRE_CACHE_SECONDS"
    )  # 15min
    bushfire_enabled: bool = Field(default=True, alias="BUSHFIRE_ENABLED")
    nsw_rfs_url: str = Field(
        default="https://www.rfs.nsw.gov.au/feeds/majorIncidents.json",
        alias="NSW_RFS_BUSHFIRE_URL",
    )
    firms_url: str = Field(
        default="https://firms.modaps.eosdis.nasa.gov/api/country/csv/{map_key}/VIIRS_SNPP_NRT/AUS/1",
        alias="FIRMS_URL",
    )

    # ──────────────────────────────────────────────────────────────
    # Speed Cameras overlay - NSW TfNSW ArcGIS (CC-BY 3.0 AU)
    # + Brisbane Council road occupancies (CC-BY 4.0)
    # Both free, no auth required.
    # ──────────────────────────────────────────────────────────────
    cameras_algo_version: str = Field(
        default="cameras.v1.nsw+brisbane",
        alias="CAMERAS_ALGO_VERSION",
    )
    cameras_cache_seconds: int = Field(
        default=86400, alias="CAMERAS_CACHE_SECONDS"
    )  # 24h
    cameras_enabled: bool = Field(default=True, alias="CAMERAS_ENABLED")

    # ──────────────────────────────────────────────────────────────
    # Elevation overlay - OpenTopography SRTM30M (primary)
    #                   + Open-Elevation (fallback, no key required)
    # Free API key: https://portal.opentopography.org/requestService?service=api
    # Leave blank to use Open-Elevation fallback only.
    # ──────────────────────────────────────────────────────────────
    opentopography_api_key: str = Field(default="", alias="OPENTOPOGRAPHY_API_KEY")
    nsw_speed_cameras_url: str = Field(
        default=(
            "https://portal.data.nsw.gov.au/arcgis/rest/services/Hosted/"
            "TFNSW_Speed_Cameras_public/FeatureServer/0/query"
        ),
        alias="NSW_SPEED_CAMERAS_URL",
    )
    brisbane_road_occupancies_url: str = Field(
        default=(
            "https://data.brisbane.qld.gov.au/api/explore/v2.1/catalog/datasets/"
            "planned-temporary-road-occupancies/records"
        ),
        alias="BRISBANE_ROAD_OCCUPANCIES_URL",
    )


settings = Settings()
