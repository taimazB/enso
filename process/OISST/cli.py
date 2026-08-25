"""Command-line entry point for the ingest pipeline.

    python -m OISST.cli init                       # create database + tables
    python -m OISST.cli scan   [--limit N]         # what is on disk vs. ingested
    python -m OISST.cli ingest [--date ...] [--limit N] [--force]
    python -m OISST.cli status [--date ...]

There is no `download` command yet — the archive in ./data is loaded as-is. When
NCEI fetching is added it becomes a step ahead of `ingest`, with the same
per-date status rows advancing through `pending_download -> ... -> success_ingest`.
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import sys

from shared.ch import DATABASE, ensure_schema, get_client

from . import config, ingest, status as status_mod


def _parse_date(value: str) -> dt.date:
    try:
        return dt.datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected YYYY-MM-DD, got {value!r}") from None


def _select(args) -> list[config.NcFile]:
    files = config.scan()
    if args.date:
        files = [f for f in files if f.date == args.date]
    if args.start:
        files = [f for f in files if f.date >= args.start]
    if args.end:
        files = [f for f in files if f.date <= args.end]
    if args.limit:
        files = files[: args.limit]
    return files


def cmd_init(args) -> int:
    ensure_schema()
    print(f"schema ready in database {DATABASE!r}")
    return 0


def cmd_scan(args) -> int:
    files = _select(args)
    if not files:
        print(f"no OISST files found under {config.NC_DIR}")
        return 1

    with get_client() as client:
        existing = status_mod.load(client)

    stale = [f for f in files if not status_mod.is_current(existing.get(f.date), f)]
    prelim = [f for f in files if f.is_preliminary]

    print(f"directory      : {config.NC_DIR}")
    print(f"files on disk  : {len(files)}  ({files[0].date} .. {files[-1].date})")
    print(f"preliminary    : {len(prelim)}")
    print(f"already loaded : {len(files) - len(stale)}")
    print(f"to ingest      : {len(stale)}")
    if stale:
        shown = ", ".join(str(f.date) for f in stale[:5])
        more = f" ... (+{len(stale) - 5} more)" if len(stale) > 5 else ""
        print(f"  next         : {shown}{more}")
    return 0


def cmd_ingest(args) -> int:
    ensure_schema()
    files = _select(args)
    if not files:
        print("nothing to do")
        return 0

    with get_client() as client:
        counts = ingest.ingest_files(
            client, files, force=args.force, batch_days=args.batch
        )

    print(
        "ingested {ingested} day(s), {rows:,} rows; skipped {skipped}; failed {failed}".format(
            **counts
        )
    )
    return 1 if counts["failed"] else 0


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
        total = client.query(
            f"SELECT count(), min(date), max(date) FROM {DATABASE}.sst_anom"
        ).result_rows[0]

    if not rows:
        print("no ingest recorded yet")
    for st, days, n_rows, first, last in rows:
        print(f"{st:<16} {days:>6} day(s)  {n_rows or 0:>14,} rows  {first} .. {last}")
    print(f"\nsst_anom: {total[0]:,} rows, {total[1]} .. {total[2]}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="OISST.cli", description=__doc__)
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    def with_selection(p):
        p.add_argument("--date", type=_parse_date, help="a single day, YYYY-MM-DD")
        p.add_argument("--start", type=_parse_date, help="first day, inclusive")
        p.add_argument("--end", type=_parse_date, help="last day, inclusive")
        p.add_argument("--limit", type=int, help="stop after N files")
        return p

    sub.add_parser("init", help="create the database and tables").set_defaults(func=cmd_init)
    with_selection(sub.add_parser("scan", help="report disk vs. ingested")).set_defaults(
        func=cmd_scan
    )
    p_ingest = with_selection(sub.add_parser("ingest", help="load NetCDF into ClickHouse"))
    p_ingest.add_argument("--force", action="store_true", help="re-ingest days already loaded")
    p_ingest.add_argument("--batch", type=int, default=30, help="days per insert (default 30)")
    p_ingest.set_defaults(func=cmd_ingest)
    with_selection(sub.add_parser("status", help="summarise ingest state")).set_defaults(
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
