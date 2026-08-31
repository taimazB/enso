"""Command-line entry point for the CoralTemp pipeline.

    python -m CRW.cli init                          # schema + climatology + region means
    python -m CRW.cli scan     [--limit N]          # what is on disk vs. ingested
    python -m CRW.cli backfill [--start/--end]      # ingest local files
    python -m CRW.cli render   [--start/--end]      # render images in bulk
    python -m CRW.cli rollup   [--start/--end]      # build region_daily
    python -m CRW.cli run      [--date ...]         # download + ingest + render
    python -m CRW.cli status   [--date ...]

`run` is the daily job: for each date it downloads, ingests, then renders that
date's buckets. `backfill` and `render` are its one-time counterparts over an
archive already on disk — the same two halves of `run`, split so each can
saturate a different resource. Ingest is disk-bound at ~3 s a day; rendering is
CPU-bound at ~2.8 s a frame, and `render` runs it across a pool.

`backfill` tolerates a partial archive — the bulk download can still be in
flight; re-running picks up whatever has since arrived. So does `render`.

`rollup` is the third bulk command and runs **after** both archives are ingested:
it reduces `sst_daily` + `mhw_daily` into `region_daily`, the per-region daily
area means the API's named-region endpoint reads instead of aggregating billions
of rows per request. `run` keeps it current one date at a time.

**`render` must run before the NetCDF goes.** Images are built from the daily
files, never from ClickHouse, so `backfill --delete-nc` or a retention prune
over a range destroys the only source those frames can come from.
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import sys

import httpx
from shared.ch import DATABASE, ensure_schema, get_client
from shared.domain import regions
from shared.periods import PERIODS
from shared.render import DEFAULT_WIDTH

from . import (
    climatology,
    config,
    download,
    imaging,
    ingest,
    regions as regions_mod,
    status as status_mod,
)

log = logging.getLogger(__name__)


def _parse_date(value: str) -> dt.date:
    try:
        return dt.datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected YYYY-MM-DD, got {value!r}") from None


def _select(args, files: list[config.NcFile] | None = None) -> list[config.NcFile]:
    files = config.scan() if files is None else files
    if getattr(args, "date", None):
        files = [f for f in files if f.date == args.date]
    if getattr(args, "start", None):
        files = [f for f in files if f.date >= args.start]
    if getattr(args, "end", None):
        files = [f for f in files if f.date <= args.end]
    if getattr(args, "limit", None):
        files = files[: args.limit]
    return files


def cmd_init(args) -> int:
    ensure_schema()
    print(f"schema ready in database {DATABASE!r}")

    missing = climatology.missing_files()
    if missing:
        print(f"WARNING: {len(missing)} climatology file(s) absent, e.g. {missing[:5]}")
        if not args.allow_partial_climatology:
            print("refusing to load a partial climatology; pass --allow-partial-climatology")
            return 1

    if args.skip_climatology:
        print("skipping climatology load")
        return 0

    with get_client() as client:
        counts = climatology.load_climatology(client, force=args.force)
        print(
            "climatology: loaded {loaded} day(s), {rows:,} rows; skipped {skipped}".format(
                **counts
            )
        )
        n = climatology.build_region_clim(client)
        print(f"region_clim: {n} rows")
    return 0


def _report_scan(label, directory, files, done) -> None:
    print(f"\n{label}")
    if not files:
        print(f"  directory    : {directory}")
        print("  files on disk: 0")
        return
    stale = [f for f in files if f.date not in done]
    print(f"  directory    : {directory}")
    print(f"  files on disk: {len(files)}  ({files[0].date} .. {files[-1].date})")
    print(f"  already loaded: {len(files) - len(stale)}")
    print(f"  to ingest    : {len(stale)}")
    if stale:
        shown = ", ".join(str(f.date) for f in stale[:5])
        more = f" ... (+{len(stale) - 5} more)" if len(stale) > 5 else ""
        print(f"    next       : {shown}{more}")


def cmd_verify_clim(args) -> int:
    """Full-read every climatology file and report the ones that fail.

    Separate from `init` rather than folded into it because it is the *recovery*
    tool: the symptom that sends you here — one MMDD failing to ingest across
    many years — shows up long after `init` has succeeded.
    """
    bad = climatology.verify_files()
    if not bad:
        print(f"climatology: all {len(climatology.MMDD_KEYS)} file(s) read OK")
        return 0
    print(f"climatology: {len(bad)} file(s) unreadable")
    for mmdd, reason in bad:
        print(f"  {mmdd:04d}  {reason}")
    print("\nre-download these from")
    print("  .../crw/data/5km/v3.1-clim19912020-v1/climatology/nc/")
    print("then re-run: backfill --product sst  (the affected dates are marked failed)")
    return 1


def cmd_scan(args) -> int:
    """Report each archive separately — they progress independently."""
    sst = _select(args)
    mhw = _select(args, config.scan_mhw())
    if not sst and not mhw:
        print(f"no NetCDF files found under {config.NC_DIR} or {config.MHW_DIR}")
        return 1

    with get_client() as client:
        done_sst = status_mod.ingested_dates(client, status_mod.SST_TABLE)
        done_mhw = status_mod.ingested_dates(client, status_mod.MHW_TABLE)

    _report_scan("CoralTemp SST", config.NC_DIR, sst, done_sst)
    _report_scan("Marine heatwave category", config.MHW_DIR, mhw, done_mhw)
    return 0


def cmd_backfill(args) -> int:
    ensure_schema()

    # Which archives this invocation covers. Both by default: MHW is part of the
    # pipeline, not a side job, so the ordinary command loads it.
    products = tuple(args.product or ("sst", "mhw"))

    if args.fresh:
        # Data table and status table together, always. The status table
        # describes what is in the data table; emptying one without the other
        # makes every day look already-ingested, and `ingest_files` would then
        # issue an `ALTER ... DELETE` mutation per day against rows that do not
        # exist. Only the selected products are truncated — a `--fresh` MHW
        # reload must not throw away 113 billion rows of SST.
        tables = []
        if "sst" in products:
            tables += ["sst_daily", "ingest_status"]
        if "mhw" in products:
            tables += ["mhw_daily", "mhw_status"]
        with get_client() as client:
            for table in tables:
                client.command(f"TRUNCATE TABLE IF EXISTS {DATABASE}.{table}")
        print("truncated " + ", ".join(tables))

    def committed(nc: config.NcFile) -> None:
        if args.delete_nc:
            nc.path.unlink(missing_ok=True)

    failed = 0
    for product in products:
        target = ingest.MHW_TARGET if product == "mhw" else ingest.SST_TARGET
        files = _select(args, config.scan_mhw() if product == "mhw" else None)
        if args.reverse:
            files = list(reversed(files))
        if not files:
            print(f"{product}: nothing to do")
            continue

        print(
            f"{product}: backfilling {len(files)} day(s), "
            f"{files[0].date} .. {files[-1].date}"
            + (" (deleting NetCDF as it goes)" if args.delete_nc else "")
        )
        with get_client() as client:
            counts = ingest.ingest_files(
                client,
                files,
                force=args.force,
                batch_days=args.batch,
                on_committed=committed,
                target=target,
            )

        print(
            f"{product}: ingested {{ingested}} day(s), {{rows:,}} rows; "
            "skipped {skipped}; failed {failed}".format(**counts)
        )
        failed += counts["failed"]
    return 1 if failed else 0


def cmd_render(args) -> int:
    """Render every closed bucket in the range, in parallel.

    Touches neither ClickHouse nor the network — it reads the NetCDF archive and
    writes the image cache, so it is safe to run against a half-finished
    backfill and needs no schema.
    """
    variables = tuple(args.variable or imaging.VARIABLES)
    try:
        # Bounded by the archives the requested variables actually read, so
        # `--variable mhw` is not clipped to whatever CoralTemp files happen to
        # be on disk (and vice versa).
        lo, hi = imaging.archive_range(variables=variables)
    except ValueError as exc:
        print(exc)
        return 1
    if args.start:
        lo = max(lo, args.start)
    if args.end:
        hi = min(hi, args.end)
    if args.date:
        lo = hi = args.date
    if lo > hi:
        print(f"empty range: {lo} .. {hi}")
        return 1

    periods = tuple(args.period or PERIODS)

    def progress(done, total, written, elapsed):
        rate = done / elapsed if elapsed else 0.0
        eta = (total - done) / rate if rate else 0.0
        print(f"  {done}/{total}  {rate:.1f}/s  eta {eta / 60:.1f}m  "
              f"{written / 1e6:.0f} MB", flush=True)

    print(f"archive {lo} .. {hi} | variables {','.join(variables)} | "
          f"periods {','.join(periods)} | width {args.width}")

    counts = imaging.render_range(
        lo, hi,
        variables=variables,
        periods=periods,
        width=args.width,
        workers=args.workers,
        force=args.force,
        limit=args.limit,
        dry_run=args.dry_run,
        progress=progress,
    )

    print(f"{counts['pending']} to render, {counts['skipped']} already cached")
    if args.dry_run:
        return 0
    print(
        "rendered {rendered} image(s), {mb:.0f} MB in {mins:.1f}m".format(
            rendered=counts["rendered"],
            mb=counts["bytes"] / 1e6,
            mins=counts["seconds"] / 60,
        )
        + (f"; {counts['empty']} bucket(s) had no NetCDF" if counts["empty"] else "")
    )
    return 0


def cmd_rollup(args) -> int:
    """Build `region_daily`, the per-region daily area means the API reads.

    Not folded into `init` — `init` runs against an empty `sst_daily` and there
    would be nothing to roll up. This is the counterpart to `backfill`: a one-off
    pass over an archive already ingested, after which `run` keeps it current one
    date at a time.

    **`backfill` deliberately does not do this per date.** One pass per region
    over a whole range reduces 113.77 billion rows once; doing it per ingested
    date would re-run eight aggregations 15,212 times. That is the same split
    `render` already has from `backfill` — bulk work gets its own command — and
    it is why this exists rather than a hook in the ingest loop. `run` is the
    exception: it appends a single date, which is eight small key-range reads.

    Ordering matters against a fresh archive. `mean_mhw` divides a sparse
    numerator by an `sst_daily` denominator, so rolling up a range whose MHW
    ingest has not finished writes a confident 0 for every date it has not
    reached — the same trap `/coverage`'s `mhw.complete` exists to gate, but
    frozen into a rollup where it is harder to see. Roll up after both archives
    are in.
    """
    ensure_schema()
    with get_client() as client:
        if args.fresh:
            regions_mod.truncate(client, args.region)
        counts = regions_mod.build_region_daily(
            client, keys=args.region, start=args.start, end=args.end
        )
        regions_mod.optimize(client)

    total = sum(counts.values())
    for key, n in sorted(counts.items()):
        print(f"  {key:<20} {n:>7,} row(s)")
    print(f"region_daily: {total:,} row(s) across {len(counts)} region(s)")
    return 0


# The two daily archives, paired with everything that differs between them.
# `run` walks this list per date rather than branching, so adding a third product
# later is a tuple, not a second copy of the download/ingest/status dance.
PRODUCTS = (
    ("sst", download.SST, ingest.SST_TARGET),
    ("mhw", download.MHW, ingest.MHW_TARGET),
)


def _process_product(client, http, date, product, target, *, force) -> str:
    """Download and ingest one date of one archive. Returns an outcome word.

    Rendering is deliberately **not** here: a date's frames are rewritten once,
    after both archives have had their turn, so a single MHW-only day does not
    re-encode the SST images it did not change.
    """
    remote = download.head(date, client=http, product=product)
    if remote is None:
        log.info("%s: %s not published yet", date, product.key)
        return "unpublished"

    existing = status_mod.load(client, target.status_table).get(date)
    if not force and status_mod.is_current(existing, remote):
        log.info("%s: %s already ingested and unrevised", date, product.key)
        return "skipped"

    nc = download.fetch(date, client=http, product=product)
    source = download.url(date, product)
    counts = ingest.ingest_files(
        client, [nc], force=True, batch_days=1, source_url=source, target=target
    )
    if counts["failed"]:
        return "failed"

    status_mod.record(
        client,
        nc,
        status_mod.STATUS_SUCCESS,
        n_rows=counts["rows"],
        source_url=source,
        remote_size=remote[0],
        remote_modified=remote[1],
        table=target.status_table,
    )
    return "ingested"


def _process_date(client, http, date, *, force, keep_nc, width) -> str:
    """Download, ingest and render one date across both archives.

    The two products are handled **independently**: NOAA publishes MHW about a
    about 90 minutes after CoralTemp on the same day, so a run landing between the
    two sees a date with SST and no MHW. One being unpublished is not a failure of the other, and the
    date's frames are still rendered for whatever did land.

    The date's overall outcome is the worst of the two, so a failure is never
    hidden by the other product's success.
    """
    outcomes = [
        _process_product(client, http, date, product, target, force=force)
        for _name, product, target in PRODUCTS
    ]

    if "ingested" in outcomes:
        imaging.render_date(
            date,
            width=width,
            available=config.available_dates(),
            available_mhw=config.available_mhw_dates(),
        )
        # The third thing a date has to keep in step, after the two daily tables.
        # Rebuilt for this date alone — eight small key-range reads — and rebuilt
        # unconditionally rather than only when MHW landed, because an SST-only
        # date writes a mean_mhw of 0 that the next run has to correct once the
        # heatwave file arrives ~90 minutes later.
        regions_mod.build_region_daily(client, start=date, end=date)
        if not keep_nc:
            imaging.prune(date)

    for worst in ("failed", "ingested", "skipped"):
        if worst in outcomes:
            return worst
    return "unpublished"


def cmd_run(args) -> int:
    ensure_schema()

    with get_client() as client, download.new_client() as http:
        if args.date:
            targets = [args.date]
        else:
            yesterday = dt.datetime.now(dt.UTC).date() - dt.timedelta(days=1)
            # The EARLIER of the two archives' last ingested day, so a date that
            # is SST-done but still MHW-pending — which happens whenever a run
            # lands in the ~90 minutes between the two publications — is revisited
            # on the next run instead of being stranded behind the SST watermark.
            # Revisiting a complete date costs two HEAD requests and nothing else.
            watermarks = [
                w for w in (
                    status_mod.last_ingested(client, target.status_table)
                    for _name, _product, target in PRODUCTS
                ) if w
            ]
            last = min(watermarks) if len(watermarks) == len(PRODUCTS) else None
            # From the day after the last ingested through yesterday — one code
            # path covering normal daily operation, a missed cron run, and the
            # tail of a bulk download that has outrun the ingest.
            start = (last + dt.timedelta(days=1)) if last else yesterday
            targets = []
            day = min(start, yesterday)
            while day <= yesterday:
                targets.append(day)
                day += dt.timedelta(days=1)
            if args.max_days:
                targets = targets[: args.max_days]

            # Then re-check the recent tail for in-place revisions.
            recheck = [
                yesterday - dt.timedelta(days=i)
                for i in range(1, args.recheck_days + 1)
            ]
            targets.extend(d for d in recheck if d not in targets and (not last or d <= last))

        outcomes: dict[str, int] = {}
        failed = 0
        for date in sorted(set(targets)):
            try:
                outcome = _process_date(
                    client, http, date, force=args.force, keep_nc=args.keep_nc,
                    width=args.width,
                )
            except Exception:  # noqa: BLE001 — one bad date must not stop the run
                log.exception("failed to process %s", date)
                outcome = "failed"
            outcomes[outcome] = outcomes.get(outcome, 0) + 1
            failed += outcome == "failed"

    print(
        "run: " + ", ".join(f"{n} {word}" for word, n in sorted(outcomes.items()))
        + f" (of {len(set(targets))} target date(s))"
    )
    return 1 if failed else 0


def cmd_status(args) -> int:
    with get_client() as client:
        def statuses(table):
            return client.query(
                f"""
                SELECT status, count() AS days, sum(n_rows) AS rows,
                       min(date) AS first, max(date) AS last
                FROM {DATABASE}.{table} FINAL
                GROUP BY status ORDER BY status
                """
            ).result_rows

        def rowcount(table):
            return client.query(
                f"SELECT count(), min(date), max(date) FROM {DATABASE}.{table}"
            ).result_rows[0]

        archives = [
            ("CoralTemp SST", status_mod.SST_TABLE, "sst_daily"),
            ("Marine heatwave", status_mod.MHW_TABLE, "mhw_daily"),
        ]
        report = [(label, statuses(status), rowcount(data), data) for label, status, data in archives]
        clim = client.query(
            f"SELECT count(), uniqExact(mmdd) FROM {DATABASE}.sst_clim"
        ).result_rows[0]

    for label, rows, daily, table in report:
        print(f"\n{label}")
        if not rows:
            print("  no ingest recorded yet")
        for st, days, n_rows, first, last in rows:
            print(f"  {st:<18} {days:>6} day(s)  {n_rows or 0:>15,} rows  {first} .. {last}")
        print(f"  {table}: {daily[0]:,} rows, {daily[1]} .. {daily[2]}")
    print(f"\nsst_clim : {clim[0]:,} rows, {clim[1]} of 366 mmdd keys")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="CRW.cli", description=__doc__)
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    def with_selection(p):
        p.add_argument("--date", type=_parse_date, help="a single day, YYYY-MM-DD")
        p.add_argument("--start", type=_parse_date, help="first day, inclusive")
        p.add_argument("--end", type=_parse_date, help="last day, inclusive")
        p.add_argument("--limit", type=int, help="stop after N files")
        return p

    p_init = sub.add_parser("init", help="create tables, load climatology, build region means")
    p_init.add_argument("--force", action="store_true", help="reload the climatology")
    p_init.add_argument("--skip-climatology", action="store_true")
    p_init.add_argument("--allow-partial-climatology", action="store_true")
    p_init.set_defaults(func=cmd_init)

    sub.add_parser(
        "verify-clim", help="full-read every climatology file, reporting unreadable ones"
    ).set_defaults(func=cmd_verify_clim)

    with_selection(sub.add_parser("scan", help="report disk vs. ingested")).set_defaults(
        func=cmd_scan
    )

    p_back = with_selection(sub.add_parser("backfill", help="ingest the local archive"))
    p_back.add_argument("--force", action="store_true", help="re-ingest loaded days")
    p_back.add_argument("--batch", type=int, default=5, help="days per insert (default 5)")
    p_back.add_argument("--reverse", action="store_true", help="newest first")
    p_back.add_argument("--delete-nc", action="store_true",
                        help="delete each file once its batch has committed")
    p_back.add_argument("--fresh", action="store_true",
                        help="truncate the selected products' data and status tables first")
    p_back.add_argument("--product", action="append", choices=("sst", "mhw"),
                        help="repeatable; default both archives")
    p_back.set_defaults(func=cmd_backfill)

    p_roll = sub.add_parser("rollup", help="build region_daily from the ingested archive")
    p_roll.add_argument("--start", type=_parse_date, help="first day, inclusive")
    p_roll.add_argument("--end", type=_parse_date, help="last day, inclusive")
    p_roll.add_argument("--region", action="append", choices=sorted(regions()),
                        help="repeatable; default every named region")
    p_roll.add_argument("--fresh", action="store_true",
                        help="truncate the selected regions first")
    p_roll.set_defaults(func=cmd_rollup)

    p_rend = with_selection(sub.add_parser("render", help="render images in bulk"))
    p_rend.add_argument("--period", action="append", choices=PERIODS,
                        help="repeatable; default all three")
    p_rend.add_argument("--variable", action="append", choices=imaging.VARIABLES,
                        help="repeatable; default all three")
    p_rend.add_argument("--width", type=int, default=DEFAULT_WIDTH)
    p_rend.add_argument("--workers", type=int, default=imaging.default_workers(),
                        help="pool size (default: half the cores)")
    p_rend.add_argument("--force", action="store_true",
                        help="re-render buckets already cached")
    p_rend.add_argument("--dry-run", action="store_true")
    p_rend.set_defaults(func=cmd_render)

    p_run = sub.add_parser("run", help="download + ingest + render")
    p_run.add_argument("--date", type=_parse_date,
                       help="a single day; without it, every day from the last "
                            "ingested through yesterday, plus a revision recheck")
    p_run.add_argument("--force", action="store_true")
    p_run.add_argument("--keep-nc", action="store_true", help="skip retention pruning")
    p_run.add_argument("--width", type=int, default=DEFAULT_WIDTH)
    p_run.add_argument("--recheck-days", type=int, default=30,
                       help="how far back to HEAD for in-place revisions")
    p_run.add_argument("--max-days", type=int, help="cap the catch-up range")
    p_run.set_defaults(func=cmd_run)

    with_selection(sub.add_parser("status", help="summarise pipeline state")).set_defaults(
        func=cmd_status
    )

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
