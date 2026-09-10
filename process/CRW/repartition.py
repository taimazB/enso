"""Move a daily table onto `shared.ch.DAILY_PARTITION_SQL`, year by year.

**This is a one-off migration, not part of the pipeline.** `ensure_schema()` is
`CREATE TABLE IF NOT EXISTS`, so editing the DDL does nothing to a database that
already holds the archive; this is what actually rewrites it. See `shared/ch.py`
for why the partition key changes at all — in short, a point query's cost is one
seek per part per column and year-partitioning multiplies the part count by ~40.

**It never needs free space for a second copy of the table.** The obvious
`INSERT INTO new SELECT * FROM old` wants 129 GB of headroom and there is 90 GB.
Instead each source year is copied and then DROPPED, so occupancy stays flat at
the table's own size with a one-year bulge (~3.1 GB), and the run is resumable at
every year boundary. That is the whole design of this module.

The order of operations per year is the safety rail, and it only ever moves in
one direction:

    rows in old partition  ->  INSERT  ->  rows landed == rows expected
                                       ->  only then DROP the source partition

so an interruption loses work, never data. A crash *during* an insert leaves a
partial year in the target, which the next run detects and clears before
retrying (`_stale_rows`).

Row counts come from `system.parts`, not from `count()`: they are exact, and a
merge never changes them, so verification costs nothing on a table this size.
"""

from __future__ import annotations

import fcntl
import logging
import os
import time
from contextlib import contextmanager
from pathlib import Path

from shared.ch import DATABASE, DAILY_PARTITION_SQL, is_repartitioned, partition_key_of

log = logging.getLogger(__name__)

# The tables this can move. Both are the big per-date ones; nothing else here is
# partitioned at all.
TABLES: tuple[str, ...] = ("sst_daily", "mhw_daily")

# Suffix for the table being built. Swapped in by `finish()`.
NEW_SUFFIX = "_repart"

# Stop feeding a partition once it holds this many parts and let the background
# merges catch up. ClickHouse's own `parts_to_delay_insert` is 150 per partition
# and this migration pours ten years into one of them, so without a pause the
# inserts would be throttled by the server instead of paced by us. Well under
# the limit, because on a spinning disk a merge is slow and being close to the
# ceiling when one starts is how an insert stalls for minutes.
SETTLE_PARTS = 60

# How long to wait for that. Advisory: exceeding it logs and carries on rather
# than failing, because a slow merge is not a broken migration.
SETTLE_TIMEOUT_S = 1800

# `clickhouse_connect` defaults `send_receive_timeout` to **300 seconds**, and a single
# year of `sst_daily` is ~2.7 billion rows. On the NVMe box that is 14 s; on the production
# HDD it is comfortably past five minutes, and the failure is a loop rather than an error:
# the insert times out part-done, the next run clears the partial year and times out at the
# same place. So the migration opens its own client with a timeout that cannot be reached
# by anything this command legitimately does.
CLIENT_TIMEOUT_S = 7200

_INSERT_SETTINGS = {
    # A year is ~2.7 billion rows and there is no useful deadline on it.
    "max_execution_time": 0,
    # Two, not the default eight. The source read and the target write are the
    # same spindle here, so more writer threads just interleave two sequential
    # streams into a seek storm.
    "max_insert_threads": 2,
}


# Where the cross-process lock lives. Any directory both containers share works;
# `/opt/data` is the archive mount, which dev and prod both have.
LOCK_PATH = Path(os.environ.get("CRW_LOCK_DIR", "/opt/data")) / ".repartition.lock"


@contextmanager
def exclusive(client, table: str):
    """Refuse to run while another migration of the same table is in flight.

    **Two concurrent migrations silently duplicate rows, and the row check does
    not catch it** — each process inserts the same source partition once, so
    every individual INSERT is correct and only the total is wrong. Found the
    hard way: a second `repartition` was started against a table already being
    moved, both copied 2018, and the target ended up with 585,482,468 rows twice.
    The one that noticed noticed by accident, because the other's writes landed
    between its own before/after counts and made the arithmetic disagree.

    Two guards, because neither covers every deployment on its own:

    * an `flock` on the shared archive mount, which releases even on SIGKILL —
      but only excludes processes that actually share that mount;
    * a `system.processes` check for an INSERT into the twin, which is
      deployment-independent, since every migration talks to the same server —
      but has a window between the check and the first INSERT, which the flock
      is what closes.
    """
    new = f"{table}{NEW_SUFFIX}"
    busy = client.query(
        """
        SELECT count() FROM system.processes
        WHERE query LIKE %(p)s AND query NOT LIKE '%%system.processes%%'
        """,
        parameters={"p": f"%{new}%"},
    ).result_rows[0][0]
    if busy:
        raise RuntimeError(
            f"another migration is already writing to {new}; "
            "wait for it to finish rather than running a second one"
        )

    handle = None
    try:
        LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
        handle = open(LOCK_PATH, "w")
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        raise RuntimeError(
            f"another migration holds {LOCK_PATH}; only one may run at a time"
        ) from None
    except OSError as exc:
        # An unwritable or unshared lock directory is not a reason to refuse to
        # migrate — it just means the flock half is unavailable and the
        # `system.processes` check above is carrying it alone. Say so loudly.
        log.warning("could not take %s (%s); relying on the server-side check only",
                    LOCK_PATH, exc)
        handle = None
    try:
        yield
    finally:
        if handle is not None:
            fcntl.flock(handle, fcntl.LOCK_UN)
            handle.close()


def _rows_and_bytes(client, table: str, partition_id: str | None = None):
    """Exact row and byte counts for a table or one of its partitions.

    From `system.parts` rather than `count()`. A `count()` over a decade
    partition would have to read the date column of ~27 billion rows to answer
    a per-year question; the parts table already knows, exactly.
    """
    where = "active AND database = %(db)s AND table = %(t)s"
    params = {"db": DATABASE, "t": table}
    if partition_id is not None:
        where += " AND partition_id = %(p)s"
        params["p"] = partition_id
    row = client.query(
        f"SELECT sum(rows), sum(bytes_on_disk), count() FROM system.parts WHERE {where}",
        parameters=params,
    ).result_rows[0]
    return int(row[0] or 0), int(row[1] or 0), int(row[2] or 0)


def _free_bytes(client) -> int:
    row = client.query("SELECT min(free_space) FROM system.disks").result_rows[0]
    return int(row[0] or 0)


def _human(n: int) -> str:
    step = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if step < 1024 or unit == "TiB":
            return f"{step:,.2f} {unit}"
        step /= 1024
    return f"{n} B"


def _physical_columns(client, table: str) -> list[str]:
    """The stored columns, in order, excluding ALIAS ones.

    Read from `system.columns` rather than written out here, so `sst_daily` and
    `mhw_daily` share this code and a column added to either needs no edit. The
    ALIAS columns (`sst`, `lat`, `lon`) are computed and must not be inserted.
    """
    return [
        r[0]
        for r in client.query(
            """
            SELECT name FROM system.columns
            WHERE database = %(db)s AND table = %(t)s AND default_kind != 'ALIAS'
            ORDER BY position
            """,
            parameters={"db": DATABASE, "t": table},
        ).result_rows
    ]


def plan(client, table: str) -> list[dict]:
    """The source partitions still to be moved, oldest first.

    Oldest first because the recent end is the part `run` touches daily: leaving
    it until last keeps the window in which a normal ingest would collide with
    the migration as short as possible.
    """
    rows = client.query(
        """
        SELECT partition, partition_id, sum(rows), sum(bytes_on_disk), count()
        FROM system.parts
        WHERE active AND database = %(db)s AND table = %(t)s
        GROUP BY partition, partition_id
        ORDER BY partition
        """,
        parameters={"db": DATABASE, "t": table},
    ).result_rows
    return [
        {
            "partition": r[0],
            "partition_id": r[1],
            "rows": int(r[2]),
            "bytes": int(r[3]),
            "parts": int(r[4]),
        }
        for r in rows
    ]


# Stamped into the target table's COMMENT when it is created, and checked by `finish()`.
# See `_expected_rows` for why the total lives there rather than in a bookkeeping table.
_COMMENT_PREFIX = "enso-repartition expected_rows="


def _assert_no_concurrent_run(client, table: str, new: str) -> None:
    """Refuse to start while another migration is touching the same tables.

    **Two concurrent runs corrupt the target silently**, and the per-partition check
    cannot see it: each run verifies only the delta its own INSERT produced, so a second
    run's rows land as duplicates that every count still agrees with. Found the hard way —
    a `docker compose run` container outlived the CLI client that started it, and the next
    invocation cleared a partial year and re-inserted it while the orphan was still writing
    the same year.

    This catches the realistic case, which is an INSERT in flight — that is where
    essentially all of a migration's wall-clock goes. It cannot catch a rival that happens
    to be between statements, so `finish()` re-checks the total against what was recorded
    at the start.
    """
    rows = client.query(
        """
        SELECT query_id, elapsed FROM system.processes
        WHERE (query LIKE %(a)s OR query LIKE %(b)s) AND query NOT LIKE '%%system.processes%%'
        """,
        parameters={"a": f"%{new}%", "b": f"%INSERT INTO {DATABASE}.{table}%"},
    ).result_rows
    if rows:
        raise RuntimeError(
            f"another query is already writing {new} (query_id {rows[0][0]}, "
            f"{float(rows[0][1]):.0f}s in). Two migrations of one table produce silent "
            "duplicates; wait for it or kill it before re-running."
        )


def _expected_rows(client, new: str) -> int | None:
    """The source's total row count, as recorded when the target was created.

    Kept in the table's COMMENT rather than in a bookkeeping table: this is a one-off
    migration and it would be the only thing that table ever held, while the comment
    survives restarts, is visible in `SHOW CREATE TABLE`, and goes away with the swap.
    """
    rows = client.query(
        "SELECT comment FROM system.tables WHERE database = %(db)s AND name = %(t)s",
        parameters={"db": DATABASE, "t": new},
    ).result_rows
    comment = rows[0][0] if rows else ""
    if not comment.startswith(_COMMENT_PREFIX):
        return None
    return int(comment[len(_COMMENT_PREFIX):].strip())


def create_target(client, table: str) -> str:
    """Create the repartitioned twin if it is not already there.

    `CREATE TABLE ... AS <source>` copies the columns, codecs and ALIAS
    expressions verbatim, so only the engine clause is written here — there is
    no second copy of the schema to drift out of step with `shared/ch.py`. The
    sorting key is read off the live table for the same reason.
    """
    new = f"{table}{NEW_SUFFIX}"
    exists = client.query(
        "SELECT count() FROM system.tables WHERE database = %(db)s AND name = %(t)s",
        parameters={"db": DATABASE, "t": new},
    ).result_rows[0][0]
    if exists:
        return new
    sorting_key = client.query(
        "SELECT sorting_key FROM system.tables WHERE database = %(db)s AND name = %(t)s",
        parameters={"db": DATABASE, "t": table},
    ).result_rows[0][0]
    total = _rows_and_bytes(client, table)[0]
    client.command(
        f"CREATE TABLE {DATABASE}.{new} AS {DATABASE}.{table} "
        f"ENGINE = MergeTree PARTITION BY {DAILY_PARTITION_SQL} "
        f"ORDER BY ({sorting_key}) "
        f"COMMENT '{_COMMENT_PREFIX}{total}'"
    )
    log.info("created %s.%s", DATABASE, new)
    return new


def _stale_rows(client, new: str, partition: str) -> int:
    """Rows already in the target for a source partition that is still in place.

    Only ever non-zero after a crash mid-insert, and it cannot be answered from
    `system.parts` — a source year sits inside a target decade, so this is the
    one query in the module that reads data. Called once per invocation rather
    than once per year: after the first year the run has done every subsequent
    one itself and knows they were clean.
    """
    year = int(partition)
    return int(
        client.query(
            f"SELECT count() FROM {DATABASE}.{new} "
            "WHERE date >= %(a)s AND date <= %(b)s",
            parameters={"a": f"{year}-01-01", "b": f"{year}-12-31"},
        ).result_rows[0][0]
    )


def _clear_year(client, new: str, partition: str) -> None:
    """Remove a half-copied year from the target.

    A mutation over the whole decade partition, which is expensive and is
    supposed to be: it runs only on the crash path, and the alternative — the
    insert appending a second partial copy — is silent duplication.
    """
    year = int(partition)
    log.warning("clearing a partial copy of %s from %s (mutation, slow)", year, new)
    client.command(
        f"ALTER TABLE {DATABASE}.{new} DELETE WHERE date >= %(a)s AND date <= %(b)s",
        parameters={"a": f"{year}-01-01", "b": f"{year}-12-31"},
        settings={"mutations_sync": 2},
    )


def _settle(client, new: str, *, max_parts: int = SETTLE_PARTS, timeout_s: int = SETTLE_TIMEOUT_S) -> None:
    """Wait for background merges to bring the busiest target partition down."""
    deadline = time.monotonic() + timeout_s
    while True:
        row = client.query(
            """
            SELECT max(n) FROM (
                SELECT count() AS n FROM system.parts
                WHERE active AND database = %(db)s AND table = %(t)s
                GROUP BY partition_id)
            """,
            parameters={"db": DATABASE, "t": new},
        ).result_rows[0]
        worst = int(row[0] or 0)
        if worst <= max_parts:
            return
        if time.monotonic() > deadline:
            log.warning(
                "%s still has a partition with %d parts after %ds; continuing",
                new, worst, timeout_s,
            )
            return
        log.info("waiting for merges: busiest partition has %d parts", worst)
        time.sleep(30)


def migrate(
    client,
    table: str,
    *,
    limit: int | None = None,
    settle_parts: int = SETTLE_PARTS,
    dry_run: bool = False,
) -> dict:
    """Copy every remaining source partition into the repartitioned twin.

    Returns a summary. Safe to interrupt and re-run: a partition is dropped from
    the source only once its rows are confirmed present in the target, so the
    work already done is never repeated and the work not yet done is never lost.
    """
    if is_repartitioned(client, table):
        log.info("%s is already on the new partition key", table)
        return {"table": table, "moved": 0, "rows": 0, "done": True}

    pending = plan(client, table)
    if limit is not None:
        pending = pending[:limit]

    total_rows = sum(p["rows"] for p in pending)
    biggest = max((p["bytes"] for p in pending), default=0)
    free = _free_bytes(client)
    log.info(
        "%s: %d partition(s) to move, %s rows, largest source partition %s, free %s",
        table, len(pending), f"{total_rows:,}", _human(biggest), _human(free),
    )
    if dry_run:
        for p in pending:
            log.info(
                "  %-8s %15s rows  %10s  %3d part(s)",
                p["partition"], f"{p['rows']:,}", _human(p["bytes"]), p["parts"],
            )
        return {"table": table, "moved": 0, "rows": 0, "done": False, "planned": len(pending)}

    # Headroom for the bulge, not for a whole second copy. Twice the largest
    # source partition covers the year in flight plus the merge it triggers in
    # the target; below that this would run itself out of disk mid-year.
    if pending and free < biggest * 2:
        raise RuntimeError(
            f"{_human(free)} free is too little to move {table} safely; "
            f"need at least {_human(biggest * 2)} (twice the largest partition)"
        )

    stack = exclusive(client, table)
    stack.__enter__()
    try:
        return _move(client, table, pending, settle_parts)
    finally:
        stack.__exit__(None, None, None)


def _move(client, table: str, pending: list[dict], settle_parts: int) -> dict:
    """The copy loop itself. Only ever called under `exclusive()`."""
    new = create_target(client, table)
    _assert_no_concurrent_run(client, table, new)
    columns = ", ".join(_physical_columns(client, table))
    checked_for_stale = False
    moved = 0
    moved_rows = 0

    for p in pending:
        partition, pid, expected = p["partition"], p["partition_id"], p["rows"]

        if not checked_for_stale:
            checked_for_stale = True
            stale = _stale_rows(client, new, partition)
            if stale:
                _clear_year(client, new, partition)

        before, _, _ = _rows_and_bytes(client, new)
        started = time.monotonic()
        log.info("moving %s %s (%s rows)", table, partition, f"{expected:,}")
        client.command(
            f"INSERT INTO {DATABASE}.{new} ({columns}) "
            f"SELECT {columns} FROM {DATABASE}.{table} "
            "WHERE _partition_id = %(pid)s",
            parameters={"pid": pid},
            settings=_INSERT_SETTINGS,
        )
        after, _, _ = _rows_and_bytes(client, new)
        landed = after - before

        if landed != expected:
            raise RuntimeError(
                f"{table} {partition}: {landed:,} rows landed in {new} but the source "
                f"holds {expected:,}. Nothing dropped; investigate before re-running."
            )

        client.command(f"ALTER TABLE {DATABASE}.{table} DROP PARTITION ID %(pid)s",
                       parameters={"pid": pid})
        moved += 1
        moved_rows += landed
        log.info(
            "  %s done in %.1fs, source partition dropped, free now %s",
            partition, time.monotonic() - started, _human(_free_bytes(client)),
        )
        _settle(client, new, max_parts=settle_parts)

    remaining = plan(client, table)
    done = not remaining
    log.info(
        "%s: moved %d partition(s), %s rows; %d source partition(s) remain",
        table, moved, f"{moved_rows:,}", len(remaining),
    )
    return {"table": table, "moved": moved, "rows": moved_rows, "done": done}


def optimize(client, table: str) -> None:
    """Merge each target partition down to one part, where the disk allows it.

    Skipped rather than attempted when free space is short: ClickHouse would
    refuse the merge anyway, and the partition simply stays at a handful of
    parts — which is already ~40x fewer than it started with, so this is a
    finishing touch rather than the point of the exercise.
    """
    new = f"{table}{NEW_SUFFIX}"
    target = new if _rows_and_bytes(client, new)[0] else table
    for p in plan(client, target):
        if p["parts"] <= 1:
            continue
        free = _free_bytes(client)
        if free < p["bytes"]:
            log.warning(
                "skipping OPTIMIZE of %s %s: needs %s, %s free",
                target, p["partition"], _human(p["bytes"]), _human(free),
            )
            continue
        log.info("optimizing %s %s (%d parts, %s)",
                 target, p["partition"], p["parts"], _human(p["bytes"]))
        client.command(
            f"OPTIMIZE TABLE {DATABASE}.{target} PARTITION ID %(pid)s FINAL",
            parameters={"pid": p["partition_id"]},
            settings={"max_execution_time": 0, "alter_sync": 2},
        )


def finish(client, table: str) -> None:
    """Swap the twin into place and drop the emptied original.

    Deliberately a separate step. `EXCHANGE TABLES` is atomic, so the cutover is
    instant, but it is also the only irreversible moment in the migration and
    the operator should be the one choosing when it happens.
    """
    new = f"{table}{NEW_SUFFIX}"
    remaining = plan(client, table)
    if remaining:
        raise RuntimeError(
            f"{table} still holds {len(remaining)} partition(s); finish the migration first"
        )
    old_rows = _rows_and_bytes(client, table)[0]
    new_rows = _rows_and_bytes(client, new)[0]
    if old_rows:
        raise RuntimeError(f"{table} is not empty ({old_rows:,} rows)")

    # The check the per-partition one cannot make. Every INSERT verified its own delta,
    # which says nothing about rows a *concurrent* run may have added; this compares the
    # finished table against the count taken before anything moved.
    expected = _expected_rows(client, new)
    if expected is not None and new_rows != expected:
        raise RuntimeError(
            f"{new} holds {new_rows:,} rows but {table} held {expected:,} before the "
            f"migration ({new_rows - expected:+,}). Not swapping. A surplus means a second "
            "migration ran concurrently; find the affected partition by comparing per-year "
            "counts, then OPTIMIZE ... PARTITION ID <id> FINAL DEDUPLICATE."
        )
    log.info("swapping %s <- %s (%s rows)", table, new, f"{new_rows:,}")
    client.command(f"EXCHANGE TABLES {DATABASE}.{table} AND {DATABASE}.{new}")
    client.command(f"DROP TABLE {DATABASE}.{new}")
    log.info("%s is now partitioned by %s", table, partition_key_of(client, table))
