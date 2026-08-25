"""Paths and NetCDF filename conventions for the OISST archive."""

from __future__ import annotations

import datetime as dt
import os
import re
from dataclasses import dataclass
from pathlib import Path

NC_DIR = Path(os.environ.get("OISST_NC_DIR", "/opt/data"))
IMAGE_DIR = Path(os.environ.get("OISST_IMAGE_DIR", "/opt/data/images"))

# oisst-avhrr-v02r01.19810901.nc
# oisst-avhrr-v02r01.20260824_preliminary.nc
FILENAME_RE = re.compile(
    r"^oisst-avhrr-v02r01\.(?P<date>\d{8})(?P<prelim>_preliminary)?\.nc$"
)


@dataclass(frozen=True)
class NcFile:
    path: Path
    date: dt.date
    is_preliminary: bool

    @property
    def filename(self) -> str:
        return self.path.name


def parse_filename(path: Path) -> NcFile | None:
    """Return an `NcFile` for a recognised OISST daily file, else None."""
    match = FILENAME_RE.match(path.name)
    if match is None:
        return None
    try:
        date = dt.datetime.strptime(match["date"], "%Y%m%d").date()
    except ValueError:
        return None
    return NcFile(path=path, date=date, is_preliminary=bool(match["prelim"]))


def scan(nc_dir: Path | None = None) -> list[NcFile]:
    """All recognised daily files, sorted by date.

    When both a preliminary and a final file exist for the same date the final
    one wins — NCEI replaces preliminary files about two weeks later, and the
    two can sit side by side on disk until the old one is cleaned up.
    """
    nc_dir = nc_dir or NC_DIR
    by_date: dict[dt.date, NcFile] = {}
    for path in sorted(nc_dir.glob("*.nc")):
        nc = parse_filename(path)
        if nc is None:
            continue
        existing = by_date.get(nc.date)
        if existing is None or (existing.is_preliminary and not nc.is_preliminary):
            by_date[nc.date] = nc
    return [by_date[d] for d in sorted(by_date)]
