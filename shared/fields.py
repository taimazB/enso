"""Read CoralTemp NetCDF into subset arrays on the project's own conventions.

This module exists so the two orientation rules are applied in exactly one
place. Both produce output that looks entirely plausible when they are wrong,
which is why they are asserted here rather than trusted at each call site:

  1. **Longitude.** Source files run -179.975..179.975. The project indexes on
     0-360 (`domain.yml`'s `lon0: 0.025`) so the Pacific box is one contiguous
     `gx` span instead of two wrapping ones. The conversion is a roll by half
     the grid: ``gx_project = (gx_file + 3600) % 7200``.

  2. **Latitude.** The DAILY files are south-up (`lat[0] = -89.975`), matching
     `lon0`'s companion `lat0`. The 366 CLIMATOLOGY files are **north-up**
     (`lat[0] = +89.975`) and must be flipped. Subtracting them unflipped gives
     an anomaly field in the +/-18 degC range rather than +/-5 — wrong in every
     cell, and it renders as a perfectly believable map.

Everything returned is already subset to `domain.yml`'s box, so row 0 is the
box's southern edge and column 0 its western edge.
"""

from __future__ import annotations

import datetime as dt
import logging
import os
import re
from pathlib import Path

import netCDF4
import numpy as np

from .domain import global_grid, subset, variable

log = logging.getLogger(__name__)

# Both archives live under the one ./data bind mount: the daily files in
# `sst/`, the 366 climatology files in `climatology/`. They are separate
# directories because they have opposite lifetimes — the dailies are pruned to
# a retention window, the climatology is kept forever.
NC_DIR = Path(os.environ.get("OISST_NC_DIR", "/opt/data/sst"))
CLIM_DIR = Path(os.environ.get("CRW_CLIM_DIR", "/opt/data/climatology"))

# coraltemp_v3.1_19850101.nc
DAILY_RE = re.compile(r"^coraltemp_v3\.1_(?P<date>\d{8})\.nc$")
# ct5km_v3.1_clim-sst-mean-daily-window-01day-01grid-source19912020_day0101.nc
CLIM_GLOB = "ct5km_v3.1_clim-sst-mean-daily-window-01day-01grid-source*_day{mmdd:04d}.nc"

VARIABLE_NAME = "analysed_sst"


def mmdd_of(date: dt.date) -> int:
    """The climatology key for a date: month*100 + day, e.g. 2026-08-24 -> 824.

    Leap days need no special case — CoralTemp ships a `day0229` file.
    """
    return date.month * 100 + date.day


def daily_path(date: dt.date, nc_dir: Path | None = None) -> Path:
    return (nc_dir or NC_DIR) / f"coraltemp_v3.1_{date:%Y%m%d}.nc"


def clim_path(mmdd: int, clim_dir: Path | None = None) -> Path:
    """The climatology file for an MMDD key.

    Globbed rather than formatted because the baseline years are embedded in the
    filename (`source19912020`); a future re-baselining should be picked up, not
    silently missed.
    """
    clim_dir = clim_dir or CLIM_DIR
    matches = sorted(clim_dir.glob(CLIM_GLOB.format(mmdd=mmdd)))
    if not matches:
        raise FileNotFoundError(f"no climatology file for mmdd {mmdd:04d} in {clim_dir}")
    if len(matches) > 1:
        log.warning("multiple climatology files for %04d, using %s", mmdd, matches[-1].name)
    return matches[-1]


def _subset_indices():
    """``(gy0, gy1, gx0, gx1)`` inclusive global indices of the configured box."""
    grid, box = global_grid(), subset()
    gy0, gy1 = box.gy_range(grid)
    gx0, gx1 = box.gx_range(grid)
    return gy0, gy1, gx0, gx1


def _to_project_frame(raw: np.ndarray, *, flip_lat: bool) -> np.ndarray:
    """Roll a full-grid source array onto the project's axes and subset it."""
    grid, box = global_grid(), subset()
    if raw.shape != (grid.nlat, grid.nlon):
        raise ValueError(f"expected {(grid.nlat, grid.nlon)} grid, got {raw.shape}")

    if flip_lat:
        raw = raw[::-1]
    # -180..180 -> 0..360. `lon0` sits half a grid from the source's origin.
    raw = np.roll(raw, grid.nlon // 2, axis=1)

    gy0, gy1, gx0, gx1 = _subset_indices()
    out = raw[gy0 : gy1 + 1, gx0 : gx1 + 1]
    if out.shape != (box.nlat, box.nlon):
        raise ValueError(
            f"subset produced {out.shape}, domain.yml declares {(box.nlat, box.nlon)}"
        )
    return out


def _read_raw(path: Path, *, squeeze_time: bool) -> np.ndarray:
    with netCDF4.Dataset(path) as ds:
        var = ds.variables[VARIABLE_NAME]
        # Raw shorts, not the masked/scaled floats netCDF4 would hand back — the
        # scale factor is reapplied by the ALIAS columns in ClickHouse, and by
        # `as_celsius()` for rendering.
        var.set_auto_maskandscale(False)
        return np.asarray(var[0] if squeeze_time else var[:])


def read_daily_raw(date: dt.date, nc_dir: Path | None = None) -> np.ndarray:
    """One day's raw Int16 SST over the box. Already south-up; rolled to 0-360."""
    return _to_project_frame(
        _read_raw(daily_path(date, nc_dir), squeeze_time=True), flip_lat=False
    )


def read_clim_raw(mmdd: int, clim_dir: Path | None = None) -> np.ndarray:
    """One MMDD's raw Int16 climatology over the box, **flipped** to south-up."""
    return _to_project_frame(
        _read_raw(clim_path(mmdd, clim_dir), squeeze_time=False), flip_lat=True
    )


def valid_mask(raw: np.ndarray, variable_name: str = "sst") -> np.ndarray:
    return raw != variable(variable_name).fill_value


def as_celsius(raw: np.ndarray, variable_name: str = "sst") -> np.ndarray:
    """Raw counts -> float32 degC, with the fill value becoming NaN."""
    var = variable(variable_name)
    out = raw.astype("float32") * var.scale_factor + var.add_offset
    out[raw == var.fill_value] = np.nan
    return out


def check_orientation(daily_raw: np.ndarray, clim_raw: np.ndarray) -> None:
    """Assert the climatology's ocean is a subset of the day's ocean.

    This is the diagnostic that catches a latitude flip. Correctly oriented, the
    climatology covers a strict subset of the daily field (it omits the ice
    fringe); flipped, the overlap collapses — globally from 13.31M cells to
    9.17M, and the resulting anomaly spans about +/-18 degC instead of +/-5.

    Raises rather than warns: a wrong map is worse than no map.
    """
    dm = valid_mask(daily_raw)
    cm = valid_mask(clim_raw)
    orphans = int((cm & ~dm).sum())
    if orphans:
        raise ValueError(
            f"{orphans} climatology cells have no daily value — the climatology "
            "is expected to be a strict subset of the daily ocean mask. A "
            "latitude flip is the usual cause; see shared/fields.py."
        )


def anomaly(daily_raw: np.ndarray, clim_raw: np.ndarray) -> np.ndarray:
    """`daily - climatology` in degC, NaN where either side is absent.

    NaN therefore means two different things, which the renderer separates:
    land (no daily value) and ice-fringe ocean (daily but no climatology). Use
    `no_clim_mask()` to tell them apart.
    """
    check_orientation(daily_raw, clim_raw)
    out = as_celsius(daily_raw) - as_celsius(clim_raw)
    return out


def no_clim_mask(daily_raw: np.ndarray, clim_raw: np.ndarray) -> np.ndarray:
    """Ocean cells with SST but no climatology — anomaly undefined, not land."""
    return valid_mask(daily_raw) & ~valid_mask(clim_raw)
