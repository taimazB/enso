"""Pre-render every map image into the on-disk cache.

    docker compose -f docker-compose.dev.yml --env-file .env.dev exec api \
        python prerender.py --workers 8

**Reads NetCDF, not ClickHouse.** Every frame is built from the daily files, so
this has one hard prerequisite: the archive must still be on disk. Run it before
`CRW.cli backfill --delete-nc` or before the daily retention prune has eaten the
range — afterwards, the source for these images is gone and `/image` can only
serve what is already cached.

Renders are keyed by (variable, period, bucket start, width), so a non-default
`--width` is a separate set of files; pass the same width the frontend requests.

Only *closed* buckets are written: a week or month whose last day is past the
end of the archive is still filling up, and caching it would freeze a mean over
however many days happen to be present. Buckets missing an interior day are
closed and so are cached.
"""

from __future__ import annotations

import argparse
import datetime as dt
import multiprocessing as mp
import os
import sys
import time

import numpy as np
from shared import fields
from shared.periods import PERIODS, Period, span, start_of
from shared.render import DEFAULT_WIDTH, cache_path, encode, write_cache

VARIABLES: tuple[str, ...] = ("sst", "anom")


def archive_range(nc_dir=None) -> tuple[dt.date, dt.date]:
    """First and last date with a NetCDF on disk."""
    dates = sorted(
        d
        for d in (
            _date_of(p.name) for p in (nc_dir or fields.NC_DIR).glob("coraltemp_v3.1_*.nc")
        )
        if d
    )
    if not dates:
        raise SystemExit(f"no CoralTemp files under {nc_dir or fields.NC_DIR}")
    return dates[0], dates[-1]


def _date_of(name: str) -> dt.date | None:
    m = fields.DAILY_RE.match(name)
    if not m:
        return None
    try:
        return dt.datetime.strptime(m["date"], "%Y%m%d").date()
    except ValueError:
        return None


def buckets(period: Period, lo: dt.date, hi: dt.date) -> list[dt.date]:
    """Every closed bucket start of `period` between `lo` and `hi`."""
    out: list[dt.date] = []
    cursor = start_of(lo, period)
    while cursor <= hi:
        first, last = span(cursor, period)
        if last <= hi:
            out.append(first)
        cursor = last + dt.timedelta(days=1)
    return out


def _render_one(job: tuple[dt.date, Period, int, str]) -> tuple[dt.date, Period, int]:
    """Render one bucket from NetCDF and write it to its cache path."""
    date, period, width, variable = job
    first, last = span(date, period)

    total = count = missing_any = None
    n_days = 0
    day = first
    while day <= last:
        try:
            raw = fields.read_daily_raw(day)
        except (FileNotFoundError, OSError):
            day += dt.timedelta(days=1)
            continue
        if variable == "sst":
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
        return date, period, 0

    with np.errstate(invalid="ignore"):
        mean = np.where(count > 0, total / np.maximum(count, 1), np.nan).astype("float32")
    nc_mask = (missing_any & (count == 0)) if variable == "anom" else None
    payload = encode(mean, width, variable, no_clim=nc_mask)
    write_cache(cache_path(date, width, period, variable), payload)
    return date, period, len(payload)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--period", action="append", choices=PERIODS,
                    help="repeatable; default all three")
    ap.add_argument("--variable", action="append", choices=VARIABLES,
                    help="repeatable; default both")
    ap.add_argument("--start", type=dt.date.fromisoformat)
    ap.add_argument("--end", type=dt.date.fromisoformat)
    ap.add_argument("--width", type=int, default=DEFAULT_WIDTH)
    ap.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 4) // 2))
    ap.add_argument("--force", action="store_true", help="re-render buckets already cached")
    ap.add_argument("--limit", type=int, help="stop after N renders (smoke test)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    lo, hi = archive_range()
    lo = max(lo, args.start) if args.start else lo
    hi = min(hi, args.end) if args.end else hi
    periods: list[Period] = args.period or list(PERIODS)
    vars_: list[str] = args.variable or list(VARIABLES)

    jobs: list[tuple[dt.date, Period, int, str]] = []
    skipped = 0
    for variable in vars_:
        for period in periods:
            for date in buckets(period, lo, hi):
                if not args.force and cache_path(date, args.width, period, variable).is_file():
                    skipped += 1
                    continue
                jobs.append((date, period, args.width, variable))
    if args.limit:
        jobs = jobs[: args.limit]

    print(f"archive {lo}..{hi} | variables {','.join(vars_)} | "
          f"periods {','.join(periods)} | width {args.width}")
    print(f"{len(jobs)} to render, {skipped} already cached")
    if args.dry_run or not jobs:
        return 0

    started = time.time()
    done = written = empty = 0
    # `spawn`, not the default fork: workers each open their own NetCDF handles,
    # and HDF5 is not fork-safe once a file has been touched in the parent.
    with mp.get_context("spawn").Pool(args.workers) as pool:
        for _date, _period, size in pool.imap_unordered(_render_one, jobs, chunksize=4):
            done += 1
            written += size
            empty += size == 0
            if done % 100 == 0 or done == len(jobs):
                rate = done / (time.time() - started)
                eta = (len(jobs) - done) / rate
                print(f"  {done}/{len(jobs)}  {rate:.1f}/s  eta {eta/60:.1f}m  "
                      f"{written/1e6:.0f} MB", flush=True)

    print(f"done in {(time.time()-started)/60:.1f}m — {done-empty} images, "
          f"{written/1e6:.0f} MB" + (f", {empty} empty buckets skipped" if empty else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
