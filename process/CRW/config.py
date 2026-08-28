"""Paths and NetCDF filename conventions for the CoralTemp archive.

Filename parsing and the two directories; everything that opens a file and
applies the grid conventions lives in `shared/fields.py` instead, so `api` gets
the same behaviour without importing this package.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from pathlib import Path

from shared.fields import (  # noqa: F401 — re-exported
    CLIM_DIR,
    DAILY_RE,
    MHW_DIR,
    MHW_RE,
    NC_DIR,
)

__all__ = [
    "NC_DIR",
    "CLIM_DIR",
    "MHW_DIR",
    "NcFile",
    "parse_filename",
    "scan",
    "available_dates",
    "scan_mhw",
    "available_mhw_dates",
]


@dataclass(frozen=True)
class NcFile:
    path: Path
    date: dt.date

    @property
    def filename(self) -> str:
        return self.path.name


def parse_filename(path: Path, pattern=DAILY_RE) -> NcFile | None:
    """Return an `NcFile` for a recognised daily file, else None.

    `pattern` selects the archive — `DAILY_RE` for CoralTemp, `MHW_RE` for the
    Marine Heatwave category. Both capture a `date` group in `%Y%m%d`, and
    neither matches a `.part`, so an interrupted download can never be picked up
    as complete in either archive.
    """
    match = pattern.match(path.name)
    if match is None:
        return None
    try:
        date = dt.datetime.strptime(match["date"], "%Y%m%d").date()
    except ValueError:
        return None
    return NcFile(path=path, date=date)


def _scan(nc_dir: Path, pattern) -> list[NcFile]:
    files = [
        nc for nc in (parse_filename(p, pattern) for p in sorted(nc_dir.glob("*.nc"))) if nc
    ]
    return sorted(files, key=lambda f: f.date)


def scan_mhw(nc_dir: Path | None = None) -> list[NcFile]:
    """All recognised Marine Heatwave category files, sorted by date.

    A separate archive with its own lifetime and its own publication latency —
    MHW is published about 90 minutes after CoralTemp — so it is scanned separately
    rather than paired with the SST archive at this level. Pairing happens where
    it means something: `run` processes a date's two products independently, and
    the retention prune keeps each directory's own window.
    """
    return _scan(nc_dir or MHW_DIR, MHW_RE)


def available_mhw_dates(nc_dir: Path | None = None) -> set[dt.date]:
    return {nc.date for nc in scan_mhw(nc_dir)}


def scan(nc_dir: Path | None = None) -> list[NcFile]:
    """All recognised daily files, sorted by date.

    Unlike the OISST scan this had no preliminary/final pairing to resolve —
    CoralTemp publishes one filename per date and revises it in place, which is
    why staleness is tracked against the *remote* size/mtime in `ingest_status`
    rather than by comparing filenames.

    A `.part` file from an interrupted download is not matched by `DAILY_RE`, so
    a half-written archive member can never be picked up as complete.
    """
    return _scan(nc_dir or NC_DIR, DAILY_RE)


def available_dates(nc_dir: Path | None = None) -> set[dt.date]:
    return {nc.date for nc in scan(nc_dir)}
