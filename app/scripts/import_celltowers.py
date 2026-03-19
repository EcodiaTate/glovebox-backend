"""
import_celltowers.py
───────────────────
Bootstrap script: download the OpenCelliD MCC-505 (Australia) bulk CSV and load
it into the cell_towers SQLite table used by the coverage overlay service.

Usage (standalone):
    python -m app.scripts.import_celltowers          # uses settings.OPENCELLID_TOKEN
    OPENCELLID_TOKEN=mytoken python -m app.scripts.import_celltowers

Called automatically by Coverage.along_route() when:
  - The cell_towers table is empty, OR
  - The most recent import is more than 7 days old.

Data source: OpenCelliD  https://opencellid.org  (CC BY-SA 4.0)
CSV columns: radio,mcc,net,area,cell,unit,lon,lat,range,samples,
             changeable,created,updated,averageSignal
"""
from __future__ import annotations

import csv
import gzip
import io
import logging
import sqlite3
import time
import urllib.request
from pathlib import Path

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────
# MNC → carrier name mapping (Australia MCC 505)
# ──────────────────────────────────────────────────────────────

_MNC_TO_CARRIER: dict[int, str] = {
    1:  "Telstra",
    2:  "Optus",
    3:  "Vodafone",
    6:  "Vodafone",
    12: "Telstra",
    14: "TPG/Vodafone",
    90: "Telstra",
}

# Only load towers belonging to these MNCs (ignore MVNOs we don't surface)
_KNOWN_MNCS = set(_MNC_TO_CARRIER.keys())

# Tile size for the spatial bucket index (degrees).  0.1° ≈ 11 km.
_BUCKET_SIZE = 0.1

# Max staleness before a re-import is triggered (7 days in seconds)
REFRESH_AFTER_S = 7 * 24 * 3600


def _bucket(coord: float) -> int:
    """Map a lat or lon degree value to an integer bucket index."""
    return int(coord / _BUCKET_SIZE)


def needs_import(conn: sqlite3.Connection) -> bool:
    """Return True if cell_towers is empty or data is older than REFRESH_AFTER_S."""
    from app.core.storage import get_cell_towers_meta
    meta = get_cell_towers_meta(conn)
    if meta is None:
        return True
    imported_at_str, row_count = meta
    if row_count == 0:
        return True
    try:
        from datetime import datetime, timezone
        imported_at = datetime.fromisoformat(imported_at_str.replace("Z", "+00:00"))
        age_s = (datetime.now(tz=timezone.utc) - imported_at).total_seconds()
        return age_s > REFRESH_AFTER_S
    except Exception:
        return True


def download_csv_gz(token: str, url_template: str) -> bytes:
    """Download the compressed CSV and return raw gzip bytes."""
    url = url_template.replace("{token}", token)
    logger.info("[celltowers] Downloading MCC-505 bulk CSV from OpenCelliD …")
    req = urllib.request.Request(url, headers={"User-Agent": "Roam/1.0"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        data = resp.read()
    logger.info("[celltowers] Downloaded %.1f MB", len(data) / 1_048_576)
    return data


def load_csv_into_db(conn: sqlite3.Connection, gz_bytes: bytes) -> int:
    """
    Parse the gzip CSV and bulk-insert into cell_towers.
    Returns the number of rows inserted.
    """
    from app.core.time import utc_now_iso
    from app.core.storage import set_cell_towers_meta

    logger.info("[celltowers] Parsing CSV …")
    t0 = time.monotonic()

    # OpenCelliD bulk CSVs are shipped without a header row.
    _FIELDNAMES = ["radio", "mcc", "net", "area", "cell", "unit", "lon", "lat",
                   "range", "samples", "changeable", "created", "updated", "averageSignal"]

    rows: list[tuple] = []
    with gzip.open(io.BytesIO(gz_bytes), "rt", encoding="utf-8") as f:
        reader = csv.DictReader(f, fieldnames=_FIELDNAMES)
        for record in reader:
            try:
                mnc = int(record["net"])
            except (KeyError, ValueError):
                continue
            if mnc not in _KNOWN_MNCS:
                continue

            try:
                lat = float(record["lat"])
                lon = float(record["lon"])
                range_m = int(float(record.get("range") or 0))
                samples = int(float(record.get("samples") or 0))
                radio = record.get("radio", "").strip()
                mcc = int(record.get("mcc", 505))
                updated = record.get("updated", "")
            except (KeyError, ValueError):
                continue

            # Basic sanity check - Australia bounding box
            if not (-44.0 <= lat <= -10.0 and 112.0 <= lon <= 155.0):
                continue

            carrier_name = _MNC_TO_CARRIER[mnc]
            lat_b = _bucket(lat)
            lon_b = _bucket(lon)

            rows.append((
                radio, mcc, mnc, carrier_name,
                lat, lon, range_m, samples, updated,
                lat_b, lon_b,
            ))

    logger.info("[celltowers] Parsed %d qualifying towers in %.1fs", len(rows), time.monotonic() - t0)

    # Atomic replace: clear then insert in a single transaction.
    t1 = time.monotonic()
    with conn:
        conn.execute("DELETE FROM cell_towers;")
        conn.executemany(
            """
            INSERT INTO cell_towers
              (radio, mcc, mnc, carrier_name, lat, lon, range_m, samples, updated_at,
               lat_bucket, lon_bucket)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """,
            rows,
        )

    set_cell_towers_meta(conn, imported_at=utc_now_iso(), row_count=len(rows))
    logger.info("[celltowers] Inserted %d rows in %.1fs", len(rows), time.monotonic() - t1)
    return len(rows)


def load_local_csv_gz(local_path: str) -> bytes:
    """Read gzip CSV bytes from a local file."""
    p = Path(local_path)
    if not p.exists():
        raise FileNotFoundError(f"Local cell tower CSV not found: {p}")
    logger.info("[celltowers] Reading local CSV from %s …", p)
    data = p.read_bytes()
    logger.info("[celltowers] Read %.1f MB from local file", len(data) / 1_048_576)
    return data


def run_import(conn: sqlite3.Connection, *, token: str = "", url_template: str = "", local_path: str = "") -> int:
    """
    Load cell tower data. Prefers local file if local_path is set and the file
    exists. Falls back to downloading from the API (legacy behaviour).
    Returns row count. Raises on failure.
    """
    if local_path:
        gz = load_local_csv_gz(local_path)
    else:
        gz = download_csv_gz(token, url_template)
    return load_csv_into_db(conn, gz)


# ──────────────────────────────────────────────────────────────
# CLI entry point
# ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    from pathlib import Path

    # Allow running from the /backend directory
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parents[2] / ".env")

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    from app.core.settings import settings
    from app.core.storage import connect_sqlite, ensure_schema

    conn = connect_sqlite(settings.cache_db_path)
    ensure_schema(conn)

    local_path = settings.opencellid_local_db_path
    if local_path and Path(local_path).exists():
        logger.info("[celltowers] Using local file: %s", local_path)
        count = run_import(conn, local_path=local_path)
    else:
        if not settings.opencellid_token:
            logger.error("OPENCELLID_TOKEN is not set and no local CSV found - aborting")
            sys.exit(1)
        logger.info("[celltowers] No local file found, downloading from API …")
        count = run_import(
            conn,
            token=settings.opencellid_token,
            url_template=settings.opencellid_download_url,
        )
    logger.info("[celltowers] Done. %d towers in DB.", count)
    conn.close()
