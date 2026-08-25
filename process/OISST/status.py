"""Read/write the `ingest_status` state table.

One row per date, `ReplacingMergeTree`-collapsed on `updated_at`, so re-ingesting
a day overwrites its record rather than appending. Reads use `FINAL` — the table
has at most a few tens of thousands of rows, so the cost is irrelevant here.
"""

from __future__ import annotations

import datetime as dt

from shared.ch import DATABASE, STATUS_SUCCESS

from .config import NcFile


def load(client) -> dict[dt.date, dict]:
    """Current status row per date, keyed by date."""
    result = client.query(
        f"""
        SELECT date, filename, status, is_preliminary, n_rows, file_mtime, file_size, message
        FROM {DATABASE}.ingest_status FINAL
        """
    )
    columns = result.column_names
    return {row[0]: dict(zip(columns, row)) for row in result.result_rows}


def record(
    client,
    nc: NcFile,
    status: str,
    *,
    n_rows: int = 0,
    message: str = "",
) -> None:
    stat = nc.path.stat()
    client.insert(
        f"{DATABASE}.ingest_status",
        [
            [
                nc.date,
                nc.filename,
                status,
                int(nc.is_preliminary),
                n_rows,
                dt.datetime.fromtimestamp(stat.st_mtime).replace(microsecond=0),
                stat.st_size,
                message,
            ]
        ],
        column_names=[
            "date",
            "filename",
            "status",
            "is_preliminary",
            "n_rows",
            "file_mtime",
            "file_size",
            "message",
        ],
    )


def is_current(row: dict | None, nc: NcFile) -> bool:
    """True when `nc` is already ingested and the file has not changed since.

    Size and mtime are compared rather than just the date, so a preliminary file
    swapped for its final version is correctly seen as stale even though the
    date and (for a same-named replacement) filename are unchanged.
    """
    if row is None or row["status"] != STATUS_SUCCESS:
        return False
    if row["filename"] != nc.filename:
        return False
    stat = nc.path.stat()
    if int(row["file_size"]) != stat.st_size:
        return False
    recorded = row["file_mtime"]
    return int(recorded.timestamp()) == int(stat.st_mtime)
