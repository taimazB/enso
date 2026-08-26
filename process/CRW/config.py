"""Paths and NetCDF filename conventions for the CoralTemp archive.

Filename parsing and the two directories; everything that opens a file and
applies the grid conventions lives in `shared/fields.py` instead, so `api` gets
the same behaviour without importing this package.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from pathlib import Path

from shared.fields import CLIM_DIR, DAILY_RE, NC_DIR  # noqa: F401 — re-exported

__all__ = ["NC_DIR", "CLIM_DIR", "NcFile", "parse_filename", "scan", "available_dates"]


@dataclass(frozen=True)
class NcFile:
    path: Path
    date: dt.date

    @property
    def filename(self) -> str:
        return self.path.name


def parse_filename(path: Path) -> NcFile | None:
    """Return an `NcFile` for a recognised CoralTemp daily file, else None."""
    match = DAILY_RE.match(path.name)
    if match is None:
        return None
    try:
        date = dt.datetime.strptime(match["date"], "%Y%m%d").date()
    except ValueError:
        return None
    return NcFile(path=path, date=date)


def scan(nc_dir: Path | None = None) -> list[NcFile]:
    """All recognised daily files, sorted by date.

    Unlike the OISST scan this had no preliminary/final pairing to resolve —
    CoralTemp publishes one filename per date and revises it in place, which is
    why staleness is tracked against the *remote* size/mtime in `ingest_status`
    rather than by comparing filenames.

    A `.part` file from an interrupted download is not matched by `DAILY_RE`, so
    a half-written archive member can never be picked up as complete.
    """
    nc_dir = nc_dir or NC_DIR
    files = [
        nc for nc in (parse_filename(p) for p in sorted(nc_dir.glob("*.nc"))) if nc
    ]
    return sorted(files, key=lambda f: f.date)


def available_dates(nc_dir: Path | None = None) -> set[dt.date]:
    return {nc.date for nc in scan(nc_dir)}
