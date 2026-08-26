"""Render a date's map images straight from NetCDF.

Every image the archive serves is produced here or by `prerender.py`, and
**neither touches ClickHouse**. That is what allowed the `by_date` projection —
66% of measured storage — to be dropped from the schema.

A date contributes to three buckets (its day, its Monday-anchored week, its
calendar month) for each of two variables, so ingesting one day rewrites up to
six images.

### Why the retention window exists

A weekly frame is the mean over seven days. When `run` processes day N it has
only day N's file, because N-1..N-6 were deleted after their own runs — so the
week cannot be rendered unless those files are still present. `retain_window()`
keeps exactly the files the open week and month still need (at most ~37 files,
~380 MB) and prunes the rest. Losing that window does not corrupt anything, but
it does leave weekly and monthly frames frozen at whatever was last rendered.
"""

from __future__ import annotations

import datetime as dt
import logging

import numpy as np
from shared import fields
from shared.periods import PERIODS, Period, span, start_of
from shared.render import DEFAULT_WIDTH, cache_path, encode, write_cache

from .config import NC_DIR

log = logging.getLogger(__name__)

VARIABLES: tuple[str, ...] = ("sst", "anom")


def bucket_mean(
    date: dt.date,
    period: Period,
    variable_name: str,
    available: set[dt.date] | None = None,
) -> tuple[np.ndarray, np.ndarray | None, int] | None:
    """Mean field over a bucket, read from whatever NetCDF is on disk.

    Returns `(field, no_clim_mask_or_None, n_days)`, or None if no day in the
    bucket is available. `n_days` lets the caller see how much of the bucket the
    mean actually rests on.

    Cells are averaged where present: a cell that is ocean on some days of the
    week and ice-masked on others still gets a mean over the days it had.
    """
    first, last = span(date, period)
    day = first
    total: np.ndarray | None = None
    count: np.ndarray | None = None
    missing_any: np.ndarray | None = None
    n_days = 0

    while day <= last:
        if available is not None and day not in available:
            day += dt.timedelta(days=1)
            continue
        try:
            raw = fields.read_daily_raw(day)
        except (FileNotFoundError, OSError):
            day += dt.timedelta(days=1)
            continue

        if variable_name == "sst":
            value = fields.as_celsius(raw)
            no_clim = None
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

    if total is None or n_days == 0:
        return None

    with np.errstate(invalid="ignore"):
        mean = np.where(count > 0, total / np.maximum(count, 1), np.nan).astype("float32")
    # A cell with no anomaly on any contributing day stays "no climatology";
    # one that had an anomaly on at least one day carries that day's mean.
    no_clim = None
    if variable_name == "anom":
        no_clim = missing_any & (count == 0)
    return mean, no_clim, n_days


def render_date(
    date: dt.date,
    *,
    width: int = DEFAULT_WIDTH,
    variables: tuple[str, ...] = VARIABLES,
    periods: tuple[Period, ...] = PERIODS,
    available: set[dt.date] | None = None,
) -> int:
    """Rewrite every cached image the day at `date` contributes to.

    Writes the cache file unconditionally rather than through a completeness
    guard: the open week and month are *legitimately* short every day until they
    close, and must be overwritten daily as days land.
    """
    written = 0
    for period in periods:
        for name in variables:
            result = bucket_mean(date, period, name, available)
            if result is None:
                log.warning("no data for %s %s bucket at %s", name, period, date)
                continue
            field, no_clim, n_days = result
            path = cache_path(date, width, period, name)
            write_cache(path, encode(field, width, name, no_clim=no_clim))
            written += 1
            log.debug("wrote %s (%d day(s))", path, n_days)
    log.info("rendered %d image(s) for %s", written, date)
    return written


def retention_floor(date: dt.date) -> dt.date:
    """Earliest date whose NetCDF is still needed to re-render `date`'s buckets.

    The open month's first day, or the open week's Monday — whichever is
    earlier, since a week straddling a month boundary reaches back further than
    the month does.
    """
    return min(start_of(date, "weekly"), start_of(date, "monthly"))


def prune(date: dt.date, nc_dir=None, keep_after: dt.date | None = None) -> int:
    """Delete daily NetCDF that has fallen out of `date`'s retention window."""
    from .config import scan

    floor = keep_after or retention_floor(date)
    removed = 0
    for nc in scan(nc_dir or NC_DIR):
        if nc.date < floor:
            nc.path.unlink(missing_ok=True)
            removed += 1
    if removed:
        log.info("pruned %d NetCDF file(s) older than %s", removed, floor)
    return removed
