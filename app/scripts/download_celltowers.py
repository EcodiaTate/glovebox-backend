"""
download_celltowers.py
─────────────────────
Standalone script to download the OpenCelliD MCC-505 (Australia) bulk CSV
and store it locally for the coverage service to load at runtime.

Usage:
    python -m app.scripts.download_celltowers

The coverage service reads from OPENCELLID_LOCAL_DB_PATH (default:
data/celltowers/505.csv.gz). This script downloads from the OpenCelliD API
and writes to that path.

Run weekly via cron/task scheduler:
    0 3 * * 0  cd /path/to/backend && python -m app.scripts.download_celltowers
"""
from __future__ import annotations

import logging
import sys
import urllib.request
from pathlib import Path

logger = logging.getLogger(__name__)


def download(token: str, url_template: str, dest: Path) -> int:
    """Download the gzip CSV from OpenCelliD and write to dest. Returns file size in bytes."""
    url = url_template.replace("{token}", token)
    logger.info("[download_celltowers] Downloading MCC-505 bulk CSV …")
    logger.info("[download_celltowers] URL: %s", url.replace(token, "***"))

    req = urllib.request.Request(url, headers={"User-Agent": "Roam/1.0"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        data = resp.read()

    dest.parent.mkdir(parents=True, exist_ok=True)

    # Write to a temp file first, then rename for atomicity
    tmp = dest.with_suffix(".tmp")
    tmp.write_bytes(data)
    tmp.replace(dest)

    logger.info(
        "[download_celltowers] Saved %.1f MB to %s",
        len(data) / 1_048_576,
        dest,
    )
    return len(data)


if __name__ == "__main__":
    # Allow running from the /backend directory
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parents[2] / ".env")

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    from app.core.settings import settings

    if not settings.opencellid_token:
        logger.error("OPENCELLID_TOKEN is not set in .env - aborting")
        sys.exit(1)

    dest = Path(settings.opencellid_local_db_path)
    size = download(
        token=settings.opencellid_token,
        url_template=settings.opencellid_download_url,
        dest=dest,
    )
    logger.info("[download_celltowers] Done. %d bytes written.", size)
