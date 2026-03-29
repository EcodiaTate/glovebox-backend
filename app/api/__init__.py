from __future__ import annotations

from fastapi import APIRouter

from .health import router as health_router
from .tiles import router as tiles_router
from .nav import router as nav_router
from .places import router as places_router
from .bundle import router as bundle_router
from .sync import router as sync_router
from .guide import router as guide_router
from .stripe import router as stripe_router
from .trips import router as trips_router
from .fuel import router as fuel_router
from .rest_areas import router as rest_areas_router
from .coverage import router as coverage_router
from .wildlife import router as wildlife_router
from .emergency_services import router as emergency_services_router
from .heritage import router as heritage_router
from .air_quality import router as air_quality_router
from .bushfire import router as bushfire_router
from .speed_cameras import router as speed_cameras_router
from .toilets import router as toilets_router
from .school_zones import router as school_zones_router
from .roadkill import router as roadkill_router
from .presence import router as presence_router
from .observations import router as observations_router
from .peer_sync import router as peer_sync_router
from .ai_trip import router as ai_trip_router
from .account import router as account_router

api_router = APIRouter()
api_router.include_router(health_router)
api_router.include_router(tiles_router)
api_router.include_router(guide_router)
api_router.include_router(nav_router)
api_router.include_router(places_router)
api_router.include_router(bundle_router)
api_router.include_router(sync_router)
api_router.include_router(stripe_router)
api_router.include_router(trips_router)
api_router.include_router(fuel_router)
api_router.include_router(rest_areas_router)
api_router.include_router(coverage_router)
api_router.include_router(wildlife_router)
api_router.include_router(emergency_services_router)
api_router.include_router(heritage_router)
api_router.include_router(air_quality_router)
api_router.include_router(bushfire_router)
api_router.include_router(speed_cameras_router)
api_router.include_router(toilets_router)
api_router.include_router(school_zones_router)
api_router.include_router(roadkill_router)
api_router.include_router(presence_router)
api_router.include_router(observations_router)
api_router.include_router(peer_sync_router)
api_router.include_router(ai_trip_router)
api_router.include_router(account_router)
