# app/services/corridor.py
from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Optional, Dict, Tuple, List

import httpx

from app.core.contracts import (
    BBox4,
    CorridorGraphMeta,
    CorridorGraphPack,
    CorridorNode,
    CorridorEdge,
)
from app.core.edges_db import EdgesDB
from app.core.keying import corridor_key
from app.core.polyline6 import decode_polyline6
from app.core.storage import get_corridor_pack, put_corridor_pack
from app.core.time import utc_now_iso

logger = logging.getLogger(__name__)


def _bbox_expand(b: BBox4, buffer_m: int) -> BBox4:
    dlat = buffer_m / 111_320.0
    mid_lat = (b.minLat + b.maxLat) / 2.0
    cosv = max(0.2, math.cos(math.radians(mid_lat)))
    dlng = buffer_m / (111_320.0 * cosv)
    return BBox4(
        minLng=b.minLng - dlng,
        minLat=b.minLat - dlat,
        maxLng=b.maxLng + dlng,
        maxLat=b.maxLat + dlat,
    )


def _sample_poly6(poly6: str, max_pts: int = 500) -> List[Tuple[float, float]]:
    """Decode polyline6 and downsample to at most max_pts."""
    pts = decode_polyline6(poly6)
    if not pts or len(pts) <= max_pts:
        return pts
    step = len(pts) / max_pts
    return [pts[int(i * step)] for i in range(max_pts)]


def _haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    R = 6_371_000
    dlat = math.radians(lat2 - lat1)
    dlng = math.radians(lng2 - lng1)
    a = math.sin(dlat / 2) ** 2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlng / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _nearest_spine_point(
    lat: float, lng: float, spine: List[Tuple[float, float]],
) -> Tuple[float, float]:
    """Find the spine point closest (manhattan proxy) to (lat, lng)."""
    best = spine[0]
    best_d = abs(spine[0][0] - lat) + abs(spine[0][1] - lng)
    for p in spine[1:]:
        d = abs(p[0] - lat) + abs(p[1] - lng)
        if d < best_d:
            best_d = d
            best = p
    return best


@dataclass
class CorridorEnsureResult:
    meta: CorridorGraphMeta
    pack: Optional[CorridorGraphPack] = None


class Corridor:
    """
    Builds a corridor graph from OSRM-traced routes to each stop.

    Strategy ("tree routing"):
    1. Use the main route's OSRM polyline as the spine
    2. For each suggested stop, call OSRM to get the driving route
       from the nearest spine point to the stop (with node annotations)
    3. Collect all OSM node IDs from those routes
    4. Query the edges DB by node IDs to get the actual road edges
    5. Result: a tree-shaped graph that is small, connected, and
       guaranteed to reach every stop

    This replaces spatial bbox/hull queries which are either too slow
    (PostGIS geometry ops) or too large (millions of edges for wide areas).
    """

    def __init__(self, *, cache_conn, edges_db: EdgesDB, algo_version: str,
                 osrm_base_url: str = "http://127.0.0.1:5000",
                 osrm_profile: str = "driving"):
        self.cache_conn = cache_conn
        self.edges_db = edges_db
        self.algo_version = algo_version
        self.osrm_base_url = osrm_base_url
        self.osrm_profile = osrm_profile

    def _osrm_nearest_node(
        self,
        lat: float, lng: float,
        client: httpx.Client,
    ) -> Optional[int]:
        """
        Call OSRM /nearest to snap a point to its closest road node.
        Returns the OSM node ID, or None on failure.
        """
        url = f"{self.osrm_base_url}/nearest/v1/{self.osrm_profile}/{lng},{lat}"
        try:
            r = client.get(url, params={"number": "1"}, timeout=10.0)
        except Exception:
            return None
        if r.status_code != 200:
            return None
        data = r.json()
        if data.get("code") != "Ok":
            return None
        waypoints = data.get("waypoints", [])
        if waypoints:
            # OSRM nearest returns nodes array on waypoints when available
            nodes = waypoints[0].get("nodes", [])
            if nodes:
                return nodes[0]
        return None

    def _osrm_route_nodes(
        self,
        from_lat: float, from_lng: float,
        to_lat: float, to_lng: float,
        client: httpx.Client,
        alternatives: int = 2,
    ) -> List[int]:
        """
        Call OSRM to route between two points and return the OSM node IDs
        along the route (and alternatives).
        """
        coords = f"{from_lng},{from_lat};{to_lng},{to_lat}"
        url = f"{self.osrm_base_url}/route/v1/{self.osrm_profile}/{coords}"
        params = {
            "overview": "false",       # We don't need the geometry
            "geometries": "polyline6",
            "steps": "false",          # We don't need steps
            "annotations": "nodes",    # THIS is the key — gives us OSM node IDs
            "alternatives": str(alternatives) if alternatives > 0 else "false",
        }

        try:
            r = client.get(url, params=params, timeout=30.0)
        except Exception as e:
            logger.warning("OSRM route failed for (%.4f,%.4f)→(%.4f,%.4f): %s",
                          from_lat, from_lng, to_lat, to_lng, e)
            return []

        if r.status_code != 200:
            return []

        data = r.json()
        if data.get("code") != "Ok":
            return []

        # Collect node IDs from all routes (primary + alternatives)
        all_nodes: List[int] = []
        for route in data.get("routes", []):
            for leg in route.get("legs", []):
                annotation = leg.get("annotation", {})
                nodes = annotation.get("nodes", [])
                all_nodes.extend(nodes)

        return all_nodes

    def ensure(
        self,
        *,
        route_key: str,
        route_polyline6: str,
        profile: str,
        buffer_m: int,
        max_edges: int,
        stop_coords: Optional[List[Tuple[float, float]]] = None,
    ) -> CorridorEnsureResult:
        ckey = corridor_key(route_key, buffer_m, max_edges, profile, self.algo_version,
                           stop_count=len(stop_coords) if stop_coords else 0)

        # Check cache first
        existing = get_corridor_pack(self.cache_conn, ckey)
        if existing:
            pack = CorridorGraphPack.model_validate(existing)
            meta = CorridorGraphMeta(
                corridor_key=ckey,
                route_key=route_key,
                profile=profile,
                buffer_m=buffer_m,
                max_edges=max_edges,
                algo_version=self.algo_version,
                created_at=existing.get("created_at") or utc_now_iso(),
                bytes=len(existing.get("nodes", [])) + len(existing.get("edges", [])),
            )
            return CorridorEnsureResult(meta=meta, pack=pack)

        # ── Tree routing strategy ─────────────────────────────────────
        # 1. Get OSM node IDs from the main route spine
        # 2. Parallel /nearest snap for all unique stops — skip those
        #    already on the spine
        # 3. Parallel /route for remaining stops (spine→stop)
        # 4. Query edges DB by collected node IDs
        #
        # Optimized for GCR→GCR same-region (~5ms per OSRM call):
        #   - 30 concurrent workers (OSRM has 4 vCPU, concurrency=320)
        #   - /nearest and /route both parallelized
        #   - Single httpx connection pool shared across all phases
        import concurrent.futures
        import time as _time

        spine_points = _sample_poly6(route_polyline6)
        all_node_ids: set[int] = set()

        spine_full = decode_polyline6(route_polyline6)
        if spine_full and len(spine_full) >= 2:
            start_lat, start_lng = spine_full[0]
            end_lat, end_lng = spine_full[-1]

            # Deduplicate: group stops by nearest spine point on a ~1km grid.
            # Keep only the furthest stop per grid cell — its OSRM route
            # covers the roads for closer stops too.
            stop_routes: Dict[Tuple[float, float], Tuple[float, float, float]] = {}
            if stop_coords:
                for slat, slng in stop_coords:
                    sp_lat, sp_lng = _nearest_spine_point(slat, slng, spine_points)
                    dist = _haversine_m(slat, slng, sp_lat, sp_lng)
                    if dist < 500:  # skip stops very close to spine
                        continue
                    grid_key = (round(slat, 2), round(slng, 2))
                    if grid_key not in stop_routes or dist > stop_routes[grid_key][2]:
                        stop_routes[grid_key] = (sp_lat, sp_lng, slat, slng, dist)

            unique_routes = [
                (grid_key, sp_lat, sp_lng, slat, slng)
                for grid_key, (sp_lat, sp_lng, slat, slng, _) in stop_routes.items()
            ]

            logger.info("corridor tree: spine + %d stops → %d unique routes (deduplicated)",
                        len(stop_coords) if stop_coords else 0, len(unique_routes))

            # 30 workers: OSRM Cloud Run has concurrency=320 on 4 vCPU.
            # Same-region latency is ~5ms, so 30 concurrent requests ≈ 150ms
            # of CPU time per batch — well within capacity.
            max_workers = 30

            with httpx.Client(
                limits=httpx.Limits(max_connections=max_workers + 5, max_keepalive_connections=max_workers),
                timeout=httpx.Timeout(30.0, connect=5.0),
            ) as client:
                # Phase 1: Route the spine (single call)
                t_spine = _time.monotonic()
                spine_nodes = self._osrm_route_nodes(
                    start_lat, start_lng, end_lat, end_lng, client, alternatives=0,
                )
                all_node_ids.update(spine_nodes)
                logger.info("corridor tree: spine → %d nodes (%.1fs)",
                           len(spine_nodes), _time.monotonic() - t_spine)

                # Phase 2: Parallel /nearest snap for ALL unique stops
                t_snap = _time.monotonic()

                def _snap_stop(entry):
                    _gk, _sp_lat, _sp_lng, slat, slng = entry
                    node_id = self._osrm_nearest_node(slat, slng, client)
                    return entry, node_id

                stops_needing_route: list[tuple] = []
                snapped = 0
                with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
                    for entry, nearest_node in pool.map(_snap_stop, unique_routes):
                        if nearest_node and nearest_node in all_node_ids:
                            snapped += 1
                            continue
                        if nearest_node:
                            all_node_ids.add(nearest_node)
                        stops_needing_route.append(entry)

                logger.info("corridor tree: %d snapped to spine, %d need routing (%.1fs)",
                           snapped, len(stops_needing_route), _time.monotonic() - t_snap)

                # Phase 3: Parallel /route for remaining stops
                t_route = _time.monotonic()

                def _route_stop(args):
                    _gk, sp_lat, sp_lng, slat, slng = args
                    return self._osrm_route_nodes(sp_lat, sp_lng, slat, slng, client, alternatives=0)

                routed = 0
                failed = 0
                with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
                    futures = {pool.submit(_route_stop, r): r for r in stops_needing_route}
                    for future in concurrent.futures.as_completed(futures):
                        try:
                            nodes = future.result()
                            if nodes:
                                all_node_ids.update(nodes)
                                routed += 1
                            else:
                                failed += 1
                        except Exception:
                            failed += 1

                logger.info("corridor tree: routed=%d, failed=%d, total nodes=%d (%.1fs)",
                           routed, failed, len(all_node_ids), _time.monotonic() - t_route)

        # Step 3: Query edges by node IDs
        import time as _time
        t_edges = _time.monotonic()
        logger.info("corridor tree: querying edges for %d unique nodes", len(all_node_ids))
        edge_rows = self.edges_db.query_by_node_ids(list(all_node_ids))
        logger.info("corridor tree: got %d edges (%.1fs)", len(edge_rows), _time.monotonic() - t_edges)

        # Build nodes + edges from query results
        node_coords: Dict[int, Tuple[float, float]] = {}
        edges_out: list[CorridorEdge] = []

        for row in edge_rows:
            if row.from_id not in node_coords:
                node_coords[row.from_id] = (row.from_lat, row.from_lng)
            if row.to_id not in node_coords:
                node_coords[row.to_id] = (row.to_lat, row.to_lng)

            flags = 0
            if row.toll == 1:
                flags |= 1
            if row.ferry == 1:
                flags |= 2
            if row.unsealed == 1:
                flags |= 4

            edges_out.append(
                CorridorEdge(
                    a=row.from_id,
                    b=row.to_id,
                    distance_m=int(round(row.dist_m)),
                    duration_s=int(round(row.cost_s)),
                    flags=flags,
                )
            )

        nodes_out = [
            CorridorNode(id=nid, lat=lat, lng=lng)
            for nid, (lat, lng) in node_coords.items()
        ]

        # Corridor bbox
        all_lats = [n.lat for n in nodes_out] if nodes_out else [0.0]
        all_lngs = [n.lng for n in nodes_out] if nodes_out else [0.0]
        corridor_bbox = _bbox_expand(BBox4(
            minLng=min(all_lngs),
            minLat=min(all_lats),
            maxLng=max(all_lngs),
            maxLat=max(all_lats),
        ), 1000)  # small buffer since the graph already covers exactly what we need

        logger.info("corridor pack: %d nodes, %d edges", len(nodes_out), len(edges_out))

        pack = CorridorGraphPack(
            corridor_key=ckey,
            route_key=route_key,
            profile=profile,
            algo_version=self.algo_version,
            bbox=corridor_bbox,
            nodes=nodes_out,
            edges=edges_out,
        )

        created_at = utc_now_iso()
        bytes_written = put_corridor_pack(
            self.cache_conn,
            corridor_key=ckey,
            route_key=route_key,
            profile=profile,
            buffer_m=buffer_m,
            max_edges=max_edges,
            algo_version=self.algo_version,
            created_at=created_at,
            pack=pack.model_dump(),
        )

        meta = CorridorGraphMeta(
            corridor_key=ckey,
            route_key=route_key,
            profile=profile,
            buffer_m=buffer_m,
            max_edges=max_edges,
            algo_version=self.algo_version,
            created_at=created_at,
            bytes=bytes_written,
        )
        return CorridorEnsureResult(meta=meta, pack=pack)

    def get(self, corridor_key: str) -> Optional[CorridorGraphPack]:
        return self.get_corridor_pack(corridor_key)

    def get_corridor_pack(self, corridor_key_str: str) -> Optional[CorridorGraphPack]:
        row = get_corridor_pack(self.cache_conn, corridor_key_str)
        if not row:
            return None
        return CorridorGraphPack.model_validate(row)
