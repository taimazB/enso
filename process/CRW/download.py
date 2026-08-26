"""Fetch daily CoralTemp files from NOAA Coral Reef Watch.

    .../5km/v3.1_op/nc/v1.0/daily/sst/{YYYY}/coraltemp_v3.1_{YYYYMMDD}.nc

Unlike OISST there is no preliminary/final filename pair. `v3.1_op` is the
operational near-real-time stream and a date's file can be **revised in place**,
so a revision changes the bytes at a URL that never changes. Since `run` deletes
the local NetCDF after ingesting, the only way to notice is to keep what the
server reported — `Content-Length` and `Last-Modified` — in `ingest_status` and
HEAD the URL again later. `head()` returns exactly that pair.
"""

from __future__ import annotations

import datetime as dt
import logging
from pathlib import Path

import httpx

from .config import NC_DIR, NcFile

log = logging.getLogger(__name__)

BASE_URL = (
    "https://www.star.nesdis.noaa.gov/pub/sod/mecb/crw/data/5km"
    "/v3.1_op/nc/v1.0/daily/sst"
)

# A daily file is ~10 MB and the server is not fast. Generous per-read timeout,
# no overall cap: a stalled read fails, a merely slow one does not.
TIMEOUT = httpx.Timeout(connect=30.0, read=180.0, write=30.0, pool=30.0)


def filename(date: dt.date) -> str:
    return f"coraltemp_v3.1_{date:%Y%m%d}.nc"


def url(date: dt.date) -> str:
    return f"{BASE_URL}/{date:%Y}/{filename(date)}"


def new_client() -> httpx.Client:
    return httpx.Client(timeout=TIMEOUT, follow_redirects=True)


def head(date: dt.date, client: httpx.Client | None = None) -> tuple[int, str] | None:
    """`(content_length, last_modified)` for a date, or None if not published.

    None is the normal answer for today and often for yesterday — CRW publishes
    at roughly one day's latency and not at a fixed hour.
    """
    owned = client is None
    client = client or new_client()
    try:
        response = client.head(url(date))
    except httpx.HTTPError as exc:
        log.warning("HEAD %s failed: %s", url(date), exc)
        return None
    finally:
        if owned:
            client.close()
    if response.status_code != 200:
        return None
    return (
        int(response.headers.get("content-length") or 0),
        response.headers.get("last-modified", ""),
    )


def fetch(
    date: dt.date,
    nc_dir: Path | None = None,
    client: httpx.Client | None = None,
) -> NcFile:
    """Download one date into `nc_dir` and return it as an `NcFile`.

    Streamed into a `.part` file and renamed on completion. A killed run must
    never leave a truncated `.nc` behind — `config.scan()` would not match the
    `.part` name, but it would happily match a truncated `.nc`, and a short
    NetCDF can fail in ways that look like a data problem rather than a
    transfer one.
    """
    nc_dir = nc_dir or NC_DIR
    nc_dir.mkdir(parents=True, exist_ok=True)

    target = nc_dir / filename(date)
    partial = target.with_suffix(target.suffix + ".part")
    source = url(date)

    owned = client is None
    client = client or new_client()
    try:
        log.info("downloading %s", source)
        with client.stream("GET", source) as response:
            response.raise_for_status()
            with partial.open("wb") as fh:
                for chunk in response.iter_bytes(chunk_size=1 << 20):
                    fh.write(chunk)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
    finally:
        if owned:
            client.close()

    partial.replace(target)
    log.info("downloaded %s (%.1f MB)", target.name, target.stat().st_size / 1e6)
    return NcFile(path=target, date=date)
