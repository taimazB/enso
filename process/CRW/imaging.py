"""Render map images straight from NetCDF.

**Every image the archive serves is produced here, and none of it touches
ClickHouse.** That is what allowed the `by_date` projection — 66% of measured
storage — to be dropped from the schema.

Two entry points over one implementation:

- `render_date()` rewrites the buckets a single day contributes to. This is what
  `run` calls, after download and ingest.
- `render_range()` walks the closed buckets of a date range across a process
  pool. This is the bulk form, for history that `run` has already passed.

Both go through `shared.buckets.bucket_field()`, so a change to how a week is
reduced cannot apply to one and not the other. That function lives in `shared/`
rather than here because `api/modules/render.py` needs it too — it renders the
buckets still inside the retention window on demand — and it had drifted into a
second copy there. Two definitions of "what is a week" is one too many, and the
MHW variable made that concrete: it reduces a bucket by **max**, not mean.

A date contributes to three buckets (its day, its Monday-anchored week, its
calendar month) for each of three variables, so ingesting one day rewrites up to
nine images.

### Why the retention window exists

A weekly frame is the mean over seven days. When `run` processes day N it has
only day N's file, because N-1..N-6 were deleted after their own runs — so the
week cannot be rendered unless those files are still present. `retain_window()`
keeps exactly the files the open week and month still need (at most ~37 files,
~380 MB) and prunes the rest. Losing that window does not corrupt anything, but
it does leave weekly and monthly frames frozen at whatever was last rendered.

`prune()` walks **both** archives, for the same reason: the MHW weekly and
monthly frames are a max over the same span of days and need their own files
kept, and an MHW file is ~640 KB, so the second window is small.
"""

from __future__ import annotations

import datetime as dt
import logging
import multiprocessing as mp
import os
import time

from shared.buckets import bucket_field
from shared.periods import PERIODS, Period, span, start_of
from shared.render import DEFAULT_WIDTH, cache_path, encode, write_cache

from .config import MHW_DIR, NC_DIR, scan, scan_mhw

log = logging.getLogger(__name__)

VARIABLES: tuple[str, ...] = ("sst", "anom", "mhw")


def render_date(
    date: dt.date,
    *,
    width: int = DEFAULT_WIDTH,
    variables: tuple[str, ...] = VARIABLES,
    periods: tuple[Period, ...] = PERIODS,
    available: set[dt.date] | None = None,
    available_mhw: set[dt.date] | None = None,
) -> int:
    """Rewrite every cached image the day at `date` contributes to.

    Writes the cache file unconditionally rather than through a completeness
    guard: the open week and month are *legitimately* short every day until they
    close, and must be overwritten daily as days land.

    The two archives get **separate** availability sets. They are downloaded and
    pruned independently — MHW is published about 90 minutes after CoralTemp — so a
    date's SST file being on disk says nothing about its MHW file, and passing
    one set for both would silently skip real frames.
    """
    written = 0
    for period in periods:
        for name in variables:
            result = bucket_field(
                date, period, name,
                available_mhw if name == "mhw" else available,
            )
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


def prune(
    date: dt.date, nc_dir=None, keep_after: dt.date | None = None, mhw_dir=None
) -> int:
    """Delete daily NetCDF that has fallen out of `date`'s retention window.

    Walks **both** archives. An MHW weekly or monthly frame is a max over the
    same span of days as an SST one, so it needs its own files kept for exactly
    as long; pruning only `sst/` would freeze the MHW week at whatever was last
    rendered while the SST week kept updating. The second window is cheap — an
    MHW file is ~640 KB against CoralTemp's ~10 MB.
    """
    floor = keep_after or retention_floor(date)
    removed = 0
    for files in (scan(nc_dir or NC_DIR), scan_mhw(mhw_dir or MHW_DIR)):
        for nc in files:
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


def archive_range(
    nc_dir=None, variables: tuple[str, ...] = VARIABLES, mhw_dir=None
) -> tuple[dt.date, dt.date]:
    """First and last date with a NetCDF on disk, over the archives `variables` need.

    Goes through `config.scan()` rather than globbing, so the one definition of
    what counts as a daily file — `.part` files excluded — serves both ingest
    and rendering.

    `sst` and `anom` come from the CoralTemp archive and `mhw` from its own, so
    rendering only `mhw` must not be bounded by whatever CoralTemp files happen
    to be on disk — and rendering only `sst` must not be widened by MHW's extra
    day. The span returned is the union over the archives actually needed; a
    bucket with no file in it renders empty and is reported as such.
    """
    dates: list[dt.date] = []
    looked: list[str] = []
    if any(v in ("sst", "anom") for v in variables):
        dates += [nc.date for nc in scan(nc_dir or NC_DIR)]
        looked.append(str(nc_dir or NC_DIR))
    if "mhw" in variables:
        dates += [nc.date for nc in scan_mhw(mhw_dir or MHW_DIR)]
        looked.append(str(mhw_dir or MHW_DIR))
    if not dates:
        raise ValueError(f"no NetCDF files under {' or '.join(looked)}")
    return min(dates), max(dates)


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
    result = bucket_field(date, period, variable)
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
