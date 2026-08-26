"""Pre-render every map image into the on-disk cache.

`/image` renders on demand and caches, so the archive warms itself up as people
click through it — but the first visitor to any bucket pays ~0.12s (daily) to
~0.3s (monthly) for it. This walks the whole coverage range up front.

    docker compose -f docker-compose.dev.yml --env-file .env.dev exec api \
        python prerender.py --workers 8

Renders are keyed by (period, bucket start, width), so a non-default `--width`
is a separate set of files; pass the same width the frontend requests.

Only *closed* buckets are written: a week or month whose last day is past the
end of the archive is still filling up, and caching it would freeze a mean over
however many days happen to be ingested. Buckets missing an interior day (OISST
itself has no 1986-03-18) are closed and so are cached — `render.render()`'s own
guard cannot tell that case from a filling one, which is why the write happens
here rather than through it.
"""

from __future__ import annotations

import argparse
import datetime as dt
import multiprocessing as mp
import os
import sys
import time

from modules import render
from modules.clickhouse_helpers import DATABASE, client
from modules.periods import PERIODS, Period, span, start_of


def coverage() -> tuple[dt.date, dt.date]:
    row = client().query(
        f"SELECT min(date), max(date) FROM {DATABASE}.sst_anom"
    ).result_rows
    if not row or row[0][0] is None:
        raise SystemExit("nothing ingested")
    return row[0][0], row[0][1]


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


def _render_one(job: tuple[dt.date, Period, int]) -> tuple[dt.date, Period, int]:
    """Render one bucket and write it to its cache path. Returns bytes written."""
    date, period, width = job
    payload = render.render(date, width=width, use_cache=False, period=period)
    if payload is None:
        return date, period, 0
    path = render.cache_path(date, width, period)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Write via a temp file so a half-written image is never served: the API
    # may well be answering requests out of this same directory.
    tmp = path.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_bytes(payload)
    tmp.replace(path)
    return date, period, len(payload)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--period", action="append", choices=PERIODS,
                    help="repeatable; default all three")
    ap.add_argument("--start", type=dt.date.fromisoformat)
    ap.add_argument("--end", type=dt.date.fromisoformat)
    ap.add_argument("--width", type=int, default=render.DEFAULT_WIDTH)
    ap.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 4) // 2))
    ap.add_argument("--force", action="store_true", help="re-render buckets already cached")
    ap.add_argument("--limit", type=int, help="stop after N renders (smoke test)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    lo, hi = coverage()
    lo = max(lo, args.start) if args.start else lo
    hi = min(hi, args.end) if args.end else hi
    periods: list[Period] = args.period or list(PERIODS)

    jobs: list[tuple[dt.date, Period, int]] = []
    skipped = 0
    for period in periods:
        for date in buckets(period, lo, hi):
            if not args.force and render.cache_path(date, args.width, period).is_file():
                skipped += 1
                continue
            jobs.append((date, period, args.width))
    if args.limit:
        jobs = jobs[: args.limit]

    print(f"coverage {lo}..{hi} | periods {','.join(periods)} | width {args.width}")
    print(f"{len(jobs)} to render, {skipped} already cached")
    if args.dry_run or not jobs:
        return 0

    started = time.time()
    done = written = empty = 0
    # `spawn`, not the default fork: a forked child inherits the parent's open
    # ClickHouse socket (opened by `coverage()` above) and several children then
    # read each other's responses off it — which surfaces as urllib3 BadStatusLine
    # on a body of compressed native-format garbage, not as a connection error.
    with mp.get_context("spawn").Pool(args.workers) as pool:
        for _date, _period, size in pool.imap_unordered(_render_one, jobs, chunksize=8):
            done += 1
            written += size
            empty += size == 0
            if done % 200 == 0 or done == len(jobs):
                rate = done / (time.time() - started)
                eta = (len(jobs) - done) / rate
                print(f"  {done}/{len(jobs)}  {rate:.1f}/s  eta {eta/60:.1f}m  "
                      f"{written/1e6:.0f} MB", flush=True)

    print(f"done in {(time.time()-started)/60:.1f}m — {done-empty} images, "
          f"{written/1e6:.0f} MB" + (f", {empty} empty buckets skipped" if empty else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
