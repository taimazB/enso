"""Serving map imagery from the API.

The API **cannot** render an arbitrary historical bucket. Images are produced by
`process` from the daily NetCDF, which is deleted once its retention window
closes, and `sst_daily` deliberately carries no `by_date` projection — so
rebuilding a 2003 frame from the database would be a partition scan over
billions of rows. `/image` therefore serves the cache and 404s on a miss, rather
than hanging while it tries.

What it *can* do is render on demand for buckets whose NetCDF is still on disk:
the current day, week and month, plus anything a backfill has left behind. That
covers the head of the archive, where a partial bucket changes daily and a
stale cache would be visibly wrong.
"""

from __future__ import annotations

import datetime as dt
import logging
from pathlib import Path

import numpy as np
from shared import fields
from shared.periods import Period, span
from shared.render import (  # noqa: F401 — re-exported for call sites
    DEFAULT_WIDTH,
    IMAGE_DIR,
    MERCATOR_LAT_LIMIT,
    NO_CLIM_RGBA,
    bounds,
    cache_path,
    colorize,
    colormap_stops,
    encode,
    to_mercator,
    write_cache,
)

log = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_WIDTH",
    "IMAGE_DIR",
    "MERCATOR_LAT_LIMIT",
    "NO_CLIM_RGBA",
    "bounds",
    "cache_path",
    "colorize",
    "colormap_stops",
    "encode",
    "render",
    "to_mercator",
]


def _render_from_netcdf(
    date: dt.date, period: Period, variable_name: str, width: int
) -> bytes | None:
    """Render a bucket from whatever NetCDF is still on disk, or None."""
    first, last = span(date, period)
    total = count = None
    missing_any = None
    day = first
    n_days = 0

    while day <= last:
        try:
            raw = fields.read_daily_raw(day)
        except (FileNotFoundError, OSError):
            day += dt.timedelta(days=1)
            continue
        if variable_name == "sst":
            value, no_clim = fields.as_celsius(raw), None
        else:
            clim = fields.read_clim_raw(fields.mmdd_of(day))
            value = fields.anomaly(raw, clim)
            no_clim = fields.no_clim_mask(raw, clim)
        finite = np.isfinite(value)
        if total is None:
            total = np.zeros(value.shape, dtype="float64")
            count = np.zeros(value.shape, dtype="int32")
            missing_any = np.zeros(value.shape, dtype=bool)
        total[finite] += value[finite]
        count[finite] += 1
        if no_clim is not None:
            missing_any |= no_clim
        n_days += 1
        day += dt.timedelta(days=1)

    if not n_days:
        return None
    with np.errstate(invalid="ignore"):
        mean = np.where(count > 0, total / np.maximum(count, 1), np.nan).astype("float32")
    no_clim = (missing_any & (count == 0)) if variable_name == "anom" else None
    return encode(mean, width, variable_name, no_clim=no_clim)


def render(
    date: dt.date,
    width: int = DEFAULT_WIDTH,
    use_cache: bool = True,
    period: Period = "daily",
    variable_name: str = "sst",
) -> bytes | None:
    """WebP bytes for one bucket: cache first, NetCDF second, else None.

    A NetCDF-backed render is deliberately **not** written to the cache here.
    Only `process` decides when a bucket is settled enough to cache, because
    only it knows whether the retention window still holds days that have yet to
    be rendered into this bucket.
    """
    path: Path = cache_path(date, width, period, variable_name)
    if use_cache and path.is_file():
        return path.read_bytes()

    payload = _render_from_netcdf(date, period, variable_name, width)
    if payload is None and not use_cache and path.is_file():
        # `nocache=true` asked for a fresh render and there is no source for
        # one; the cached copy is better than nothing.
        return path.read_bytes()
    return payload
