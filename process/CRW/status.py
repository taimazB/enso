"""Read/write the `ingest_status` state table.

One row per date, `ReplacingMergeTree`-collapsed on `updated_at`, so re-ingesting
a day overwrites its record rather than appending. Reads use `FINAL` — the table
has at most a few tens of thousands of rows, so the cost is irrelevant here.

**The row outlives the file.** `run` deletes the NetCDF once a date is ingested
and rendered, so the local file cannot be compared against anything afterwards.
`remote_size` / `remote_modified` — what the source reported at download time —
are therefore the only way to notice that CoralTemp has revised a date in place,
which the v3.1_op near-real-time stream does.
"""

from __future__ import annotations

import datetime as dt

from shared.ch import DATABASE, STATUS_SUCCESS

from .config import NcFile

COLUMNS = [
    "date",
    "filename",
    "status",
    "n_rows",
    "file_size",
    "source_url",
    "remote_size",
    "remote_modified",
    "message",
]


def load(client) -> dict[dt.date, dict]:
    """Current status row per date, keyed by date."""
    result = client.query(
        f"SELECT {', '.join(COLUMNS)} FROM {DATABASE}.ingest_status FINAL"
    )
    columns = result.column_names
    return {row[0]: dict(zip(columns, row)) for row in result.result_rows}


def ingested_dates(client) -> set[dt.date]:
    rows = client.query(
        f"SELECT date FROM {DATABASE}.ingest_status FINAL WHERE status = %(ok)s",
        parameters={"ok": STATUS_SUCCESS},
    ).result_rows
    return {row[0] for row in rows}


def last_ingested(client) -> dt.date | None:
    row = client.query(
        f"SELECT max(date) FROM {DATABASE}.ingest_status FINAL WHERE status = %(ok)s",
        parameters={"ok": STATUS_SUCCESS},
    ).result_rows
    return row[0][0] if row and row[0][0] else None


def record(
    client,
    nc: NcFile,
    status: str,
    *,
    n_rows: int = 0,
    message: str = "",
    source_url: str = "",
    remote_size: int = 0,
    remote_modified: str = "",
) -> None:
    """Write this date's row.

    File size comes from disk when the file is still there; a status written
    after deletion records zero rather than failing.
    """
    try:
        size = nc.path.stat().st_size
    except OSError:
        size = 0

    client.insert(
        f"{DATABASE}.ingest_status",
        [[
            nc.date,
            nc.filename,
            status,
            n_rows,
            size,
            source_url,
            remote_size,
            remote_modified,
            message,
        ]],
        column_names=COLUMNS,
    )


def is_current(row: dict | None, remote: tuple[int, str] | None = None) -> bool:
    """Whether a date is ingested and the source has not revised it since.

    With `remote` — a `(size, last_modified)` pair from a HEAD against the
    source — this detects an in-place revision. Without it, presence at
    `success_ingest` is taken as current, which is what `backfill` wants: it
    works from files already on disk and must not issue 15,000 HEAD requests.
    """
    if row is None or row["status"] != STATUS_SUCCESS:
        return False
    if remote is None:
        return True
    size, modified = remote
    # A zero recorded size means the row predates revision tracking; treat it as
    # current rather than re-downloading the whole archive on first upgrade.
    if not int(row["remote_size"] or 0):
        return True
    if size and int(row["remote_size"]) != size:
        return False
    return not (modified and row["remote_modified"] and row["remote_modified"] != modified)
