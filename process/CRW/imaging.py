"""Render map images straight from NetCDF.

**Every image the archive serves is produced here, and none of it touches
ClickHouse.** That is what allowed the `by_date` projection — 66% of measured
storage — to be dropped from the schema.

Two entry points over one implementation:

- `render_date()` rewrites the buckets a single day contributes to. This is what
  `run` calls, after download and ingest.
- `render_range()` walks the closed buckets of a date range across a process
  pool. This is the bulk form, for history that `run` has already passed.

Both go through `bucket_mean()`, so a change to how a week is averaged cannot
apply to one and not the other. They used to be separate implementations in
separate services — this module and an `api/prerender.py` — which is exactly the
drift this arrangement removes.

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
import multiprocessing as mp
import os
import time

import numpy as np
from shared import fields
from shared.periods import PERIODS, Period, span, start_of
from shared.render import DEFAULT_WIDTH, cache_path, encode, write_cache

from .config import NC_DIR, scan

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
    floor = keep_after or retention_floor(date)
    removed = 0
    for nc in scan(nc_dir or NC_DIR):
        if nc.date < floor:
            nc.path.unlink(missing_ok=True)
            removed += 1
    if removed:
        log.info("pruned %d NetCDF file(s) older than %s", removed, floor)
    return removed


# --- Bulk rendering ---------------------------------------------------------
#
# `run` renders one date at a time, as it lands. Everything already ingested
# before that needs the same work done in bulk, which is what follows. It was an
# `api/prerender.py` until it moved here: it never queried ClickHouse, so it was
# a `process` job living in the API purely because it predated the switch from
# database-backed to NetCDF-backed rendering.


def archive_range(nc_dir=None) -> tuple[dt.date, dt.date]:
    """First and last date with a NetCDF on disk.

    Goes through `config.scan()` rather than globbing, so the one definition of
    what counts as a daily file — `.part` files excluded — serves both ingest
    and rendering.
    """
    dates = sorted(nc.date for nc in scan(nc_dir or NC_DIR))
    if not dates:
        raise ValueError(f"no CoralTemp files under {nc_dir or NC_DIR}")
    return dates[0], dates[-1]


def closed_buckets(period: Period, lo: dt.date, hi: dt.date) -> list[dt.date]:
    """Every *closed* bucket start of `period` between `lo` and `hi`.

    A week or month whose last day falls past `hi` is still filling up, and
    caching it would freeze a mean over however many days happen to be on disk.
    Those are left to `render_date()`, which rewrites them daily until they
    close. A bucket missing an *interior* day is closed and is rendered — it is
    as complete as it will ever be.
    """
    out: list[dt.date] = []
    cursor = start_of(lo, period)
    while cursor <= hi:
        first, last = span(cursor, period)
        if last <= hi:
            out.append(first)
        cursor = last + dt.timedelta(days=1)
    return out


def render_bucket(job: tuple[dt.date, Period, str, int]) -> tuple[dt.date, Period, str, int]:
    """Render one bucket and write it to its cache path; returns bytes written.

    Top-level and tuple-argumented because it is the pool worker and has to be
    picklable. Zero bytes means the bucket had no NetCDF at all.
    """
    date, period, variable, width = job
    result = bucket_mean(date, period, variable)
    if result is None:
        return date, period, variable, 0
    field, no_clim, _ = result
    payload = encode(field, width, variable, no_clim=no_clim)
    write_cache(cache_path(date, width, period, variable), payload)
    return date, period, variable, len(payload)


def default_workers() -> int:
    return max(1, (os.cpu_count() or 4) // 2)


def render_range(
    lo: dt.date,
    hi: dt.date,
    *,
    variables: tuple[str, ...] = VARIABLES,
    periods: tuple[Period, ...] = PERIODS,
    width: int = DEFAULT_WIDTH,
    workers: int | None = None,
    force: bool = False,
    limit: int | None = None,
    dry_run: bool = False,
    progress=None,
) -> dict:
    """Render every closed bucket in `lo..hi` across a process pool.

    The pool uses **`spawn`, not the default fork**: workers each open their own
    NetCDF handles, and HDF5 is not fork-safe once a file has been touched in the
    parent.

    Cost is dominated by lossless WebP encoding (~2.8 s a frame) rather than by
    reading NetCDF (~0.14 s a file), so this is CPU-bound and scales with
    `workers` — but it contends for the same `./data` mount as an ingest, which
    is disk-bound.
    """
    jobs: list[tuple[dt.date, Period, str, int]] = []
    skipped = 0
    for variable in variables:
        for period in periods:
            for date in closed_buckets(period, lo, hi):
                if not force and cache_path(date, width, period, variable).is_file():
                    skipped += 1
                    continue
                jobs.append((date, period, variable, width))
    if limit:
        jobs = jobs[:limit]

    counts = {"rendered": 0, "empty": 0, "skipped": skipped, "bytes": 0, "seconds": 0.0}
    if dry_run or not jobs:
        counts["pending"] = len(jobs)
        return counts

    started = time.time()
    done = 0
    with mp.get_context("spawn").Pool(workers or default_workers()) as pool:
        for _date, _period, _variable, size in pool.imap_unordered(
            render_bucket, jobs, chunksize=4
        ):
            done += 1
            counts["bytes"] += size
            if size:
                counts["rendered"] += 1
            else:
                counts["empty"] += 1
            if progress is not None and (done % 100 == 0 or done == len(jobs)):
                progress(done, len(jobs), counts["bytes"], time.time() - started)

    counts["seconds"] = time.time() - started
    counts["pending"] = len(jobs)
    return counts
