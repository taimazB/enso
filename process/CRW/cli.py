"""Command-line entry point for the CoralTemp pipeline.

    python -m CRW.cli init                          # schema + climatology + region means
    python -m CRW.cli scan     [--limit N]          # what is on disk vs. ingested
    python -m CRW.cli backfill [--start/--end]      # ingest local files
    python -m CRW.cli run      [--date ...]         # download + ingest + image
    python -m CRW.cli status   [--date ...]

`run` is the daily job. `backfill` is its one-time counterpart over an archive
already on disk, and tolerates a partial one — the bulk download can still be in
flight; re-running picks up whatever has since arrived.
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import sys

import httpx
from shared.ch import DATABASE, ensure_schema, get_client
from shared.render import DEFAULT_WIDTH

from . import climatology, config, download, imaging, ingest, status as status_mod

log = logging.getLogger(__name__)


def _parse_date(value: str) -> dt.date:
    try:
        return dt.datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected YYYY-MM-DD, got {value!r}") from None


def _select(args) -> list[config.NcFile]:
    files = config.scan()
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


def cmd_scan(args) -> int:
    files = _select(args)
    if not files:
        print(f"no CoralTemp files found under {config.NC_DIR}")
        return 1

    with get_client() as client:
        done = status_mod.ingested_dates(client)

    stale = [f for f in files if f.date not in done]
    print(f"directory      : {config.NC_DIR}")
    print(f"files on disk  : {len(files)}  ({files[0].date} .. {files[-1].date})")
    print(f"already loaded : {len(files) - len(stale)}")
    print(f"to ingest      : {len(stale)}")
    if stale:
        shown = ", ".join(str(f.date) for f in stale[:5])
        more = f" ... (+{len(stale) - 5} more)" if len(stale) > 5 else ""
        print(f"  next         : {shown}{more}")
    return 0


def cmd_backfill(args) -> int:
    ensure_schema()

    if args.fresh:
        # Both together, always. `ingest_status` describes what is in
        # `sst_daily`; emptying one without the other makes every day look
        # already-ingested, and `ingest_files` would then issue an
        # `ALTER ... DELETE` mutation per day against rows that do not exist.
        with get_client() as client:
            for table in ("sst_daily", "ingest_status"):
                client.command(f"TRUNCATE TABLE IF EXISTS {DATABASE}.{table}")
        print("truncated sst_daily and ingest_status")

    files = _select(args)
    if args.reverse:
        files = list(reversed(files))
    if not files:
        print("nothing to do")
        return 0

    def committed(nc: config.NcFile) -> None:
        if args.delete_nc:
            nc.path.unlink(missing_ok=True)

    print(
        f"backfilling {len(files)} day(s), {files[0].date} .. {files[-1].date}"
        + (" (deleting NetCDF as it goes)" if args.delete_nc else "")
    )
    with get_client() as client:
        counts = ingest.ingest_files(
            client,
            files,
            force=args.force,
            batch_days=args.batch,
            on_committed=committed,
        )

    print(
        "ingested {ingested} day(s), {rows:,} rows; skipped {skipped}; failed {failed}".format(
            **counts
        )
    )
    return 1 if counts["failed"] else 0


def _process_date(client, http, date, *, force, keep_nc, width) -> str:
    """Download, ingest and render one date. Returns a short outcome word."""
    remote = download.head(date, client=http)
    if remote is None:
        log.info("%s: not published by CRW yet", date)
        return "unpublished"

    existing = status_mod.load(client).get(date)
    if not force and status_mod.is_current(existing, remote):
        log.info("%s: already ingested and unrevised", date)
        return "skipped"

    nc = download.fetch(date, client=http)
    counts = ingest.ingest_files(
        client, [nc], force=True, batch_days=1, source_url=download.url(date)
    )
    if counts["failed"]:
        return "failed"

    status_mod.record(
        client,
        nc,
        status_mod.STATUS_SUCCESS,
        n_rows=counts["rows"],
        source_url=download.url(date),
        remote_size=remote[0],
        remote_modified=remote[1],
    )
    imaging.render_date(date, width=width, available=config.available_dates())
    if not keep_nc:
        imaging.prune(date)
    return "ingested"


def cmd_run(args) -> int:
    ensure_schema()

    with get_client() as client, download.new_client() as http:
        if args.date:
            targets = [args.date]
        else:
            yesterday = dt.datetime.now(dt.UTC).date() - dt.timedelta(days=1)
            last = status_mod.last_ingested(client)
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
        rows = client.query(
            f"""
            SELECT status, count() AS days, sum(n_rows) AS rows,
                   min(date) AS first, max(date) AS last
            FROM {DATABASE}.ingest_status FINAL
            GROUP BY status ORDER BY status
            """
        ).result_rows
        daily = client.query(
            f"SELECT count(), min(date), max(date) FROM {DATABASE}.sst_daily"
        ).result_rows[0]
        clim = client.query(
            f"SELECT count(), uniqExact(mmdd) FROM {DATABASE}.sst_clim"
        ).result_rows[0]

    if not rows:
        print("no ingest recorded yet")
    for st, days, n_rows, first, last in rows:
        print(f"{st:<18} {days:>6} day(s)  {n_rows or 0:>15,} rows  {first} .. {last}")
    print(f"\nsst_daily: {daily[0]:,} rows, {daily[1]} .. {daily[2]}")
    print(f"sst_clim : {clim[0]:,} rows, {clim[1]} of 366 mmdd keys")
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
                        help="truncate sst_daily and ingest_status first")
    p_back.set_defaults(func=cmd_backfill)

    p_run = sub.add_parser("run", help="download + ingest + image")
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
