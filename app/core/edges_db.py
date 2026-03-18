"""
app/core/edges_db.py

Unified interface for querying the road-network edges database.

Two backends:
  - EdgesDBSqlite   - local dev (R-Tree spatial index)
  - EdgesDBPostgres - production on Fly.io (PostGIS GIST index)

Factory function `create_edges_db()` auto-selects based on config.

EdgeRow fields match what corridor.py expects:
  row.from_id, row.to_id, row.from_lat, row.from_lng,
  row.to_lat, row.to_lng, row.dist_m, row.cost_s,
  row.toll, row.ferry, row.unsealed
"""

from __future__ import annotations

import logging
import os
import sqlite3
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import List, Optional

logger = logging.getLogger(__name__)


# ── Data ─────────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class EdgeRow:
    """Single road-network edge returned by a spatial query."""
    id: int
    from_id: int
    to_id: int
    from_lat: float
    from_lng: float
    to_lat: float
    to_lng: float
    dist_m: float
    cost_s: float
    toll: int          # 0 or 1
    ferry: int         # 0 or 1
    unsealed: int      # 0 or 1
    highway: Optional[str] = None
    name: Optional[str] = None
    osm_way_id: Optional[int] = None


# ── Abstract interface ───────────────────────────────────────────────

class EdgesDB(ABC):
    """Read-only spatial query interface for road edges."""

    @abstractmethod
    def query_bbox(
        self,
        min_lng: float,
        max_lng: float,
        min_lat: float,
        max_lat: float,
        max_edges: int = 500_000,
        highway_classes: Optional[List[str]] = None,
    ) -> List[EdgeRow]:
        ...

    def query_circles(
        self,
        centers: List[tuple[float, float]],
        radius_m: float,
        max_edges_per_circle: int = 50_000,
        highway_classes: Optional[List[str]] = None,
    ) -> List[EdgeRow]:
        """
        Query edges within a radius around each center point.
        Returns the union of all results (deduplicated by edge id).
        Default implementation: convert circles to bboxes and query each.
        """
        import math

        seen_ids: set[int] = set()
        results: List[EdgeRow] = []

        dlat = radius_m / 111_320.0

        for lat, lng in centers:
            cos_lat = max(0.2, math.cos(math.radians(lat)))
            dlng = radius_m / (111_320.0 * cos_lat)

            rows = self.query_bbox(
                min_lng=lng - dlng,
                max_lng=lng + dlng,
                min_lat=lat - dlat,
                max_lat=lat + dlat,
                max_edges=max_edges_per_circle,
                highway_classes=highway_classes,
            )
            for r in rows:
                if r.id not in seen_ids:
                    seen_ids.add(r.id)
                    results.append(r)

        return results

    def query_by_node_ids(self, node_ids: List[int]) -> List[EdgeRow]:
        """
        Return all edges where from_id OR to_id is in the given set.
        """
        raise NotImplementedError("Subclass must implement query_by_node_ids")

    def query_buffered_hull(
        self,
        points: List[tuple[float, float]],
        buffer_m: float,
        max_edges: int = 2_000_000,
    ) -> List[EdgeRow]:
        """
        Query edges within a buffered convex hull around a set of (lat, lng) points.
        Default implementation falls back to bbox. Postgres overrides with PostGIS.
        """
        if not points:
            return []
        import math
        lats = [p[0] for p in points]
        lngs = [p[1] for p in points]
        dlat = buffer_m / 111_320.0
        mid_lat = (min(lats) + max(lats)) / 2.0
        cosv = max(0.2, math.cos(math.radians(mid_lat)))
        dlng = buffer_m / (111_320.0 * cosv)
        return self.query_bbox(
            min_lng=min(lngs) - dlng,
            max_lng=max(lngs) + dlng,
            min_lat=min(lats) - dlat,
            max_lat=max(lats) + dlat,
            max_edges=max_edges,
        )

    def query_buffered_route(
        self,
        spine_points: List[tuple[float, float]],
        stop_points: List[tuple[float, float]],
        spine_buffer_m: float = 5000,
        stop_buffer_m: float = 10000,
        max_edges: int = 2_000_000,
    ) -> List[EdgeRow]:
        """
        Query edges within a buffered route corridor.
        The corridor is the UNION of:
          - The route spine linestring buffered by spine_buffer_m
          - Each stop point buffered by stop_buffer_m
        Default implementation falls back to bbox.
        Postgres overrides with a proper PostGIS spatial query.
        """
        all_pts = list(spine_points) + list(stop_points)
        return self.query_buffered_hull(all_pts, max(spine_buffer_m, stop_buffer_m), max_edges)

    @abstractmethod
    def count(self) -> int:
        ...

    @abstractmethod
    def close(self) -> None:
        ...


# ── SQLite backend (local dev) ───────────────────────────────────────

class EdgesDBSqlite(EdgesDB):
    """
    Queries a local SQLite DB with an R-Tree spatial index.
    Expects tables: `edges` + `edges_rtree` (R-Tree virtual table).
    Falls back to range scan if no R-Tree.
    """

    def __init__(self, db_path: str):
        self._path = db_path
        self._conn = sqlite3.connect(
            f"file:{db_path}?mode=ro",
            uri=True,
            check_same_thread=False,
        )
        self._conn.row_factory = sqlite3.Row
        self._has_rtree = self._check_rtree()
        n = self.count()
        logger.info("SQLite opened: %s (%d rows, rtree=%s)", db_path, n, self._has_rtree)

    def _check_rtree(self) -> bool:
        try:
            self._conn.execute("SELECT * FROM edges_rtree LIMIT 1")
            return True
        except sqlite3.OperationalError:
            return False

    def query_bbox(
        self,
        min_lng: float,
        max_lng: float,
        min_lat: float,
        max_lat: float,
        max_edges: int = 500_000,
        highway_classes: Optional[List[str]] = None,
    ) -> List[EdgeRow]:
        hw_clause = ""
        hw_params: tuple = ()
        if highway_classes:
            placeholders = ",".join("?" for _ in highway_classes)
            hw_clause = f" AND e.highway IN ({placeholders})"
            hw_params = tuple(highway_classes)

        if self._has_rtree:
            sql = f"""
                SELECT e.rowid AS _rowid, e.*
                FROM edges e
                JOIN edges_rtree r ON e.rowid = r.id
                WHERE r.min_lng <= ? AND r.max_lng >= ?
                  AND r.min_lat <= ? AND r.max_lat >= ?
                  {hw_clause}
                LIMIT ?
            """
            params = (max_lng, min_lng, max_lat, min_lat) + hw_params + (max_edges,)
        else:
            hw_clause_no_alias = hw_clause.replace("e.", "")
            sql = f"""
                SELECT rowid AS _rowid, *
                FROM edges
                WHERE ((from_lng BETWEEN ? AND ? AND from_lat BETWEEN ? AND ?)
                   OR (to_lng   BETWEEN ? AND ? AND to_lat   BETWEEN ? AND ?))
                  {hw_clause_no_alias}
                LIMIT ?
            """
            params = (
                min_lng, max_lng, min_lat, max_lat,
                min_lng, max_lng, min_lat, max_lat,
            ) + hw_params + (max_edges,)

        cur = self._conn.execute(sql, params)
        rows = cur.fetchall()
        return [self._row_to_edge(r) for r in rows]

    def query_by_node_ids(self, node_ids: List[int]) -> List[EdgeRow]:
        if not node_ids:
            return []
        # SQLite has a variable limit (~999), so batch in chunks
        results: List[EdgeRow] = []
        batch_size = 400  # half of 999 since we use IN twice
        for i in range(0, len(node_ids), batch_size):
            chunk = node_ids[i:i + batch_size]
            placeholders = ",".join("?" for _ in chunk)
            sql = f"""
                SELECT rowid AS _rowid, *
                FROM edges
                WHERE from_id IN ({placeholders})
                   OR to_id IN ({placeholders})
            """
            params = tuple(chunk) + tuple(chunk)
            cur = self._conn.execute(sql, params)
            results.extend(self._row_to_edge(r) for r in cur.fetchall())
        return results

    def count(self) -> int:
        cur = self._conn.execute("SELECT COUNT(*) FROM edges")
        return cur.fetchone()[0]

    def close(self) -> None:
        self._conn.close()

    @staticmethod
    def _row_to_edge(row: sqlite3.Row) -> EdgeRow:
        keys = row.keys()

        def g(col, default=None):
            return row[col] if col in keys else default

        return EdgeRow(
            id=g("_rowid") or g("rowid") or 0,
            from_id=g("from_id", 0),
            to_id=g("to_id", 0),
            from_lat=float(g("from_lat", 0.0)),
            from_lng=float(g("from_lng", 0.0)),
            to_lat=float(g("to_lat", 0.0)),
            to_lng=float(g("to_lng", 0.0)),
            dist_m=float(g("dist_m", 0.0)),
            cost_s=float(g("cost_s", 0.0)),
            toll=int(g("toll", 0) or 0),
            ferry=int(g("ferry", 0) or 0),
            unsealed=int(g("unsealed", 0) or 0),
            highway=g("highway"),
            name=g("name"),
            osm_way_id=g("osm_way_id"),
        )


# ── Postgres + PostGIS backend (production) ──────────────────────────

class EdgesDBPostgres(EdgesDB):
    """
    Queries a Postgres+PostGIS database with a GIST spatial index.
    Uses a connection pool for concurrent requests.
    """

    # Column order must match _tuple_to_edge indices
    _SELECT_COLS = """
        id, from_id, to_id,
        from_lat, from_lng, to_lat, to_lng,
        dist_m, cost_s,
        toll, ferry, unsealed,
        highway, name, osm_way_id
    """

    def __init__(self, database_url: str, min_conn: int = 1, max_conn: int = 5):
        try:
            import psycopg2
            import psycopg2.pool
        except ImportError:
            raise RuntimeError(
                "psycopg2-binary is required for Postgres edges DB. "
                "Install: pip install psycopg2-binary"
            )

        self._database_url = database_url
        logger.info("Connecting to Postgres (pool %d-%d)...", min_conn, max_conn)

        self._pool = psycopg2.pool.ThreadedConnectionPool(
            min_conn, max_conn, database_url
        )

        # Verify connection + PostGIS
        conn = self._pool.getconn()
        try:
            cur = conn.cursor()
            cur.execute("SELECT PostGIS_Version()")
            postgis_ver = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM edges")
            n = cur.fetchone()[0]
            cur.close()
            logger.info("Postgres connected. PostGIS %s - edges table: %d rows", postgis_ver, n)
        finally:
            self._pool.putconn(conn)

    def query_bbox(
        self,
        min_lng: float,
        max_lng: float,
        min_lat: float,
        max_lat: float,
        max_edges: int = 500_000,
        highway_classes: Optional[List[str]] = None,
    ) -> List[EdgeRow]:
        hw_clause = ""
        params: list = [min_lng, min_lat, max_lng, max_lat]
        if highway_classes:
            placeholders = ",".join("%s" for _ in highway_classes)
            hw_clause = f" AND highway IN ({placeholders})"
            params.extend(highway_classes)
        params.append(max_edges)

        sql = f"""
            SELECT {self._SELECT_COLS}
            FROM edges
            WHERE geom && ST_MakeEnvelope(%s, %s, %s, %s, 4326)
            {hw_clause}
            LIMIT %s
        """

        conn = self._pool.getconn()
        try:
            cur = conn.cursor()
            cur.execute(sql, params)
            rows = cur.fetchall()
            cur.close()
            return [self._tuple_to_edge(r) for r in rows]
        finally:
            self._pool.putconn(conn)

    def query_by_node_ids(self, node_ids: List[int]) -> List[EdgeRow]:
        if not node_ids:
            return []
        # Use UNION of two index scans instead of OR — each branch uses
        # a single B-tree index (idx_edges_from_id / idx_edges_to_id)
        # which is much faster than a BitmapOr on large IN lists.
        results: List[EdgeRow] = []
        batch_size = 10_000
        conn = self._pool.getconn()
        try:
            cur = conn.cursor()
            for i in range(0, len(node_ids), batch_size):
                chunk = node_ids[i:i + batch_size]
                placeholders = ",".join("%s" for _ in chunk)
                sql = f"""
                    SELECT {self._SELECT_COLS} FROM edges
                    WHERE from_id IN ({placeholders})
                    UNION
                    SELECT {self._SELECT_COLS} FROM edges
                    WHERE to_id IN ({placeholders})
                """
                params = list(chunk) + list(chunk)
                cur.execute(sql, params)
                results.extend(self._tuple_to_edge(r) for r in cur.fetchall())
            cur.close()
            return results
        finally:
            self._pool.putconn(conn)

    def query_buffered_hull(
        self,
        points: List[tuple[float, float]],
        buffer_m: float,
        max_edges: int = 2_000_000,
    ) -> List[EdgeRow]:
        if not points:
            return []

        # Build a MULTIPOINT from all (lat,lng) → (lng,lat) for PostGIS
        point_wkts = [f"{lng} {lat}" for lat, lng in points]
        multipoint_wkt = f"MULTIPOINT({','.join(point_wkts)})"

        # ST_Buffer on geography type gives meters.
        # Build convex hull of all points, buffer it, then check edge intersection.
        sql = f"""
            SELECT {self._SELECT_COLS}
            FROM edges
            WHERE geom && ST_Buffer(
                ST_ConvexHull(ST_GeomFromText(%s, 4326))::geography,
                %s
            )::geometry
            LIMIT %s
        """
        params = [multipoint_wkt, buffer_m, max_edges]

        conn = self._pool.getconn()
        try:
            cur = conn.cursor()
            cur.execute(sql, params)
            rows = cur.fetchall()
            cur.close()
            return [self._tuple_to_edge(r) for r in rows]
        finally:
            self._pool.putconn(conn)

    def query_buffered_route(
        self,
        spine_points: List[tuple[float, float]],
        stop_points: List[tuple[float, float]],
        spine_buffer_m: float = 5000,
        stop_buffer_m: float = 10000,
        max_edges: int = 2_000_000,
    ) -> List[EdgeRow]:
        """
        PostGIS-optimized corridor query.
        Builds a narrow corridor as the UNION of:
          - The route spine LINESTRING buffered by spine_buffer_m (narrow tube)
          - All stops as a MULTIPOINT buffered by stop_buffer_m (circles that merge)
        Much tighter than a convex hull — follows the actual road shape.
        """
        if not spine_points:
            return []

        # Spine as LINESTRING
        spine_wkt = "LINESTRING(" + ", ".join(f"{lng} {lat}" for lat, lng in spine_points) + ")"

        if stop_points:
            # Stops as MULTIPOINT
            stops_wkt = "MULTIPOINT(" + ", ".join(f"{lng} {lat}" for lat, lng in stop_points) + ")"

            # Union of buffered spine + buffered stops convex hull
            # Use ST_ConvexHull on stops to merge overlapping circles efficiently
            # then buffer the hull. For stops that are isolated, the hull still
            # wraps tightly around them.
            sql = f"""
                SELECT {self._SELECT_COLS}
                FROM edges
                WHERE geom && ST_Union(
                    ST_Buffer(ST_GeomFromText(%s, 4326)::geography, %s)::geometry,
                    ST_Buffer(ST_GeomFromText(%s, 4326)::geography, %s)::geometry
                )
                LIMIT %s
            """
            params = [spine_wkt, spine_buffer_m, stops_wkt, stop_buffer_m, max_edges]
        else:
            sql = f"""
                SELECT {self._SELECT_COLS}
                FROM edges
                WHERE geom && ST_Buffer(
                    ST_GeomFromText(%s, 4326)::geography,
                    %s
                )::geometry
                LIMIT %s
            """
            params = [spine_wkt, spine_buffer_m, max_edges]

        conn = self._pool.getconn()
        try:
            cur = conn.cursor()
            cur.execute(sql, params)
            rows = cur.fetchall()
            cur.close()
            return [self._tuple_to_edge(r) for r in rows]
        finally:
            self._pool.putconn(conn)

    def count(self) -> int:
        conn = self._pool.getconn()
        try:
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) FROM edges")
            n = cur.fetchone()[0]
            cur.close()
            return n
        finally:
            self._pool.putconn(conn)

    def close(self) -> None:
        self._pool.closeall()

    @staticmethod
    def _tuple_to_edge(row: tuple) -> EdgeRow:
        return EdgeRow(
            id=row[0],
            from_id=row[1],
            to_id=row[2],
            from_lat=float(row[3] or 0),
            from_lng=float(row[4] or 0),
            to_lat=float(row[5] or 0),
            to_lng=float(row[6] or 0),
            dist_m=float(row[7] or 0),
            cost_s=float(row[8] or 0),
            toll=int(row[9] or 0),
            ferry=int(row[10] or 0),
            unsealed=int(row[11] or 0),
            highway=row[12],
            name=row[13],
            osm_way_id=row[14],
        )


# ── Factory ──────────────────────────────────────────────────────────

def create_edges_db(
    *,
    database_url: str | None = None,
    sqlite_path: str | None = None,
) -> EdgesDB:
    """
    Auto-select edges database backend.

    Priority:
      1. database_url → Postgres+PostGIS
      2. sqlite_path  → local SQLite
      3. Fallback paths for legacy setups
    """
    if database_url:
        logger.info("Using Postgres backend")
        return EdgesDBPostgres(database_url)

    if sqlite_path and os.path.isfile(sqlite_path):
        logger.info("Using SQLite backend: %s", sqlite_path)
        return EdgesDBSqlite(sqlite_path)

    # Legacy fallback paths
    fallback_paths = [
        os.path.join(os.path.dirname(__file__), "..", "data", "edges_queensland.db"),
        "/cache/edges_queensland.db",
        "/tmp/edges_queensland.db",
    ]
    for path in fallback_paths:
        resolved = os.path.abspath(path)
        if os.path.isfile(resolved):
            logger.info("Using SQLite backend (fallback): %s", resolved)
            return EdgesDBSqlite(resolved)

    raise FileNotFoundError(
        "[edges] No edges database found. "
        "Set EDGES_DATABASE_URL for Postgres or "
        "EDGES_DB_PATH for local SQLite."
    )
