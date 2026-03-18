# app/main.py
from __future__ import annotations

import logging
import logging.config
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.gzip import GZipMiddleware

# Load /backend/.env before settings are parsed (main.py is /backend/app/main.py)
BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

from app.core.settings import settings
from app.core.storage import connect_sqlite, ensure_schema
from app.core.edges_db import create_edges_db, EdgesDB
from app.api import api_router

from app.services.corridor import Corridor
from app.services.bundle import Bundle
from app.services.places import Places
from app.services.places_store import PlacesStore
from app.services.rest_areas import RestAreas
from app.core.http_client import shutdown_http_client

# ──────────────────────────────────────────────────────────────
# Structured logging
# ──────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────
# Module-level references (populated by lifespan)
# ──────────────────────────────────────────────────────────────

import sqlite3

_cache_conn: Optional[sqlite3.Connection] = None
_edges_db: Optional[EdgesDB] = None
_places_store: Optional[PlacesStore] = None


def _cache_conn_ref() -> Optional[sqlite3.Connection]:
    """Used by health.py readiness probe."""
    return _cache_conn


# ──────────────────────────────────────────────────────────────
# Lifespan (replaces deprecated @app.on_event)
# ──────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _cache_conn, _edges_db, _places_store

    # ── Startup ──────────────────────────────────────────────
    logger.info("[app] Starting up")

    _cache_conn = connect_sqlite(settings.cache_db_path)
    ensure_schema(_cache_conn)
    logger.info("[app] Cache DB ready: %s", settings.cache_db_path)

    try:
        _edges_db = create_edges_db(
            database_url=settings.edges_database_url,
            sqlite_path=settings.edges_db_path if not settings.edges_database_url else None,
        )
    except FileNotFoundError as exc:
        # Non-fatal in development - corridor endpoints will fail gracefully
        logger.warning("[app] Edges DB unavailable: %s", exc)
        _edges_db = None

    _places_store = PlacesStore(_cache_conn)

    _register_dependencies(app)

    # Pre-warm government data caches in background (rest areas, etc.)
    # This runs in a daemon thread so it doesn't block startup.
    from app.services.rest_areas import _ensure_preload
    _ensure_preload()

    logger.info("[app] Startup complete")
    yield

    # ── Shutdown ─────────────────────────────────────────────
    logger.info("[app] Shutting down")
    await shutdown_http_client()
    if _edges_db is not None:
        try:
            _edges_db.close()
        except Exception as exc:
            logger.warning("[app] Error closing edges DB: %s", exc)
    if _cache_conn is not None:
        try:
            _cache_conn.close()
        except Exception:
            pass
    logger.info("[app] Shutdown complete")


# ──────────────────────────────────────────────────────────────
# App factory
# ──────────────────────────────────────────────────────────────

app = FastAPI(title="Roam Backend", version="1.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        # Capacitor / iOS
        "capacitor://localhost",
        "ionic://localhost",
        # Local web dev
        "http://localhost:3000",
        "http://www.localhost:3000",
        "http://127.0.0.1:3000",
        "http://localhost:3001",
        "http://www.localhost:3001",
        "http://127.0.0.1:3001",
        # Production
        "https://roam.ecodia.au",
    ],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Content-Range", "Accept-Ranges", "Content-Length"],
)

app.add_middleware(GZipMiddleware, minimum_size=1000)

app.include_router(api_router)


# ──────────────────────────────────────────────────────────────
# Dependency providers + overrides
# ──────────────────────────────────────────────────────────────

def _register_dependencies(app: FastAPI) -> None:
    from app.api import nav as nav_api
    from app.api import bundle as bundle_api
    from app.api import places as places_api
    from app.api import fuel as fuel_api
    from app.api import rest_areas as rest_areas_api
    from app.api import coverage as coverage_api
    from app.api import wildlife as wildlife_api
    from app.api import emergency_services as emergency_api
    from app.api import heritage as heritage_api
    from app.api import air_quality as aqi_api
    from app.api import bushfire as bushfire_api
    from app.api import speed_cameras as cameras_api
    from app.api import toilets as toilets_api
    from app.api import school_zones as school_zones_api
    from app.api import roadkill as roadkill_api

    def provide_cache_conn() -> sqlite3.Connection:
        if _cache_conn is None:
            raise RuntimeError("Cache DB not initialised")
        return _cache_conn

    def provide_corridor_service() -> Corridor:
        if _edges_db is None:
            from app.core.errors import service_unavailable
            service_unavailable("edges_db_unavailable", "Edges database is not available")
        return Corridor(
            cache_conn=_cache_conn,
            edges_db=_edges_db,
            algo_version=settings.corridor_algo_version,
            osrm_base_url=settings.osrm_base_url,
            osrm_profile="driving",
        )

    def provide_bundle_service() -> Bundle:
        return Bundle(conn=_cache_conn)

    def provide_places_service() -> Places:
        return Places(
            cache_conn=_cache_conn,
            algo_version=settings.places_algo_version,
            store=_places_store,
        )

    # Corridor
    app.dependency_overrides[nav_api.get_corridor_service] = provide_corridor_service
    app.dependency_overrides[bundle_api.get_corridor_service] = provide_corridor_service

    # Bundle
    app.dependency_overrides[bundle_api.get_bundle_service] = provide_bundle_service

    # Cache conn
    app.dependency_overrides[nav_api.get_cache_conn] = provide_cache_conn
    app.dependency_overrides[bundle_api.get_cache_conn] = provide_cache_conn

    # Places
    app.dependency_overrides[places_api.get_places_service] = provide_places_service
    app.dependency_overrides[places_api.get_corridor_service] = provide_corridor_service
    app.dependency_overrides[places_api.get_cache_conn] = provide_cache_conn
    app.dependency_overrides[bundle_api.get_places_service] = provide_places_service

    # Fuel
    app.dependency_overrides[fuel_api.get_cache_conn] = provide_cache_conn

    # Rest Areas
    def provide_rest_areas_service() -> RestAreas:
        return RestAreas(conn=_cache_conn)

    app.dependency_overrides[rest_areas_api.get_rest_areas_service] = provide_rest_areas_service

    # Coverage
    app.dependency_overrides[coverage_api.get_cache_conn] = provide_cache_conn

    # Wildlife
    app.dependency_overrides[wildlife_api.get_cache_conn] = provide_cache_conn

    # Emergency Services
    app.dependency_overrides[emergency_api.get_cache_conn] = provide_cache_conn

    # Heritage
    app.dependency_overrides[heritage_api.get_cache_conn] = provide_cache_conn

    # Air Quality
    app.dependency_overrides[aqi_api.get_cache_conn] = provide_cache_conn

    # Bushfire
    app.dependency_overrides[bushfire_api.get_cache_conn] = provide_cache_conn

    # Speed Cameras
    app.dependency_overrides[cameras_api.get_cache_conn] = provide_cache_conn

    # Toilets
    app.dependency_overrides[toilets_api.get_cache_conn] = provide_cache_conn

    # School Zones
    app.dependency_overrides[school_zones_api.get_cache_conn] = provide_cache_conn

    # Roadkill
    app.dependency_overrides[roadkill_api.get_cache_conn] = provide_cache_conn

    # Presence
    from app.api import presence as presence_api
    app.dependency_overrides[presence_api.get_cache_conn] = provide_cache_conn

    # Observations
    from app.api import observations as observations_api
    app.dependency_overrides[observations_api.get_cache_conn] = provide_cache_conn

    # Peer Sync
    from app.api import peer_sync as peer_sync_api
    app.dependency_overrides[peer_sync_api.get_cache_conn] = provide_cache_conn
