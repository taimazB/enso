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
from dataclasses import dataclass
from pathlib import Path

import httpx

from .config import MHW_DIR, NC_DIR, NcFile

log = logging.getLogger(__name__)

BASE_URL = (
    "https://www.star.nesdis.noaa.gov/pub/sod/mecb/crw/data/5km"
    "/v3.1_op/nc/v1.0/daily/sst"
)

# The Marine Heatwave category lives in a **different product suite**, not under
# the CoralTemp v3.1_op tree — there is no `mhw/` beside `sst/`. It is derived
# from CoralTemp v3.1 but versioned and published separately, and it runs about
# 90 minutes behind: measured, 2026-08-27 was published at 14:52 UTC for SST and
# 15:20 UTC for MHW. A `run` landing between the two sees SST and no MHW, which is
# why it treats the two independently rather than pairing them.
MHW_BASE_URL = (
    "https://www.star.nesdis.noaa.gov/pub/sod/mecb/crw/data/marine_heatwave"
    "/v1.0.1/category/nc"
)

# A daily file is ~10 MB and the server is not fast. Generous per-read timeout,
# no overall cap: a stalled read fails, a merely slow one does not.
TIMEOUT = httpx.Timeout(connect=30.0, read=180.0, write=30.0, pool=30.0)


@dataclass(frozen=True)
class Product:
    """One daily archive: where its files come from and where they land.

    Two exist. They share this module because the mechanics are identical —
    stream to `.part`, rename, and track the server's `(size, last-modified)`
    against a URL that never changes — and they are separate values rather than
    a branch because everything above here has to keep them apart: separate
    directories, separate status tables, separate publication latency.
    """

    key: str
    base_url: str
    template: str
    directory: Path

    def filename(self, date: dt.date) -> str:
        return self.template.format(date=date)

    def url(self, date: dt.date) -> str:
        return f"{self.base_url}/{date:%Y}/{self.filename(date)}"


SST = Product("sst", BASE_URL, "coraltemp_v3.1_{date:%Y%m%d}.nc", NC_DIR)
MHW = Product("mhw", MHW_BASE_URL, "noaa-crw_mhw_v1.0.1_category_{date:%Y%m%d}.nc", MHW_DIR)


def filename(date: dt.date, product: Product = SST) -> str:
    return product.filename(date)


def url(date: dt.date, product: Product = SST) -> str:
    return product.url(date)


def new_client() -> httpx.Client:
    return httpx.Client(timeout=TIMEOUT, follow_redirects=True)


def head(
    date: dt.date, client: httpx.Client | None = None, product: Product = SST
) -> tuple[int, str] | None:
    """`(content_length, last_modified)` for a date, or None if not published.

    None is the normal answer for today and often for yesterday — CRW publishes
    at roughly one day's latency and not at a fixed hour. For `MHW` it is also
    the normal answer for a date CoralTemp already has, since that suite lands
    about 90 minutes later.
    """
    owned = client is None
    client = client or new_client()
    target = product.url(date)
    try:
        response = client.head(target)
    except httpx.HTTPError as exc:
        log.warning("HEAD %s failed: %s", target, exc)
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
    product: Product = SST,
) -> NcFile:
    """Download one date into `nc_dir` and return it as an `NcFile`.

    Streamed into a `.part` file and renamed on completion. A killed run must
    never leave a truncated `.nc` behind — `config.scan()` would not match the
    `.part` name, but it would happily match a truncated `.nc`, and a short
    NetCDF can fail in ways that look like a data problem rather than a
    transfer one.
    """
    nc_dir = nc_dir or product.directory
    nc_dir.mkdir(parents=True, exist_ok=True)

    target = nc_dir / product.filename(date)
    partial = target.with_suffix(target.suffix + ".part")
    source = product.url(date)

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
