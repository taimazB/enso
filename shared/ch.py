"""ClickHouse connection helper and schema DDL.

Both `api` and `process` import this so there is exactly one definition of the
tables. `get_client()` selects between the local docker-compose instance
(``CH_HOST``/``CH_PORT``) and a remote one (``CH_USE_REMOTE=true`` +
``CH_REMOTE_URL``), matching the ocean-acidification-dashboard convention.
"""

from __future__ import annotations

import os

import clickhouse_connect

DATABASE = os.environ.get("CH_DATABASE", "enso")


def _bool_env(name: str, default: str = "false") -> bool:
    return os.environ.get(name, default).strip().lower() in {"1", "true", "yes", "on"}


def get_client(database: str | None = None, **kwargs):
    """Return a ClickHouse client pointed at the local or remote instance."""
    database = database if database is not None else DATABASE
    if _bool_env("CH_USE_REMOTE"):
        return clickhouse_connect.get_client(
            host=os.environ["CH_REMOTE_URL"],
            port=int(os.environ.get("CH_REMOTE_PORT", 8123)),
            username=os.environ.get("CH_REMOTE_USER", "default"),
            password=os.environ.get("CH_REMOTE_PASSWORD", ""),
            database=database,
            secure=_bool_env("CH_REMOTE_SECURE", "true"),
            **kwargs,
        )
    return clickhouse_connect.get_client(
        host=os.environ.get("CH_HOST", "db-ch"),
        port=int(os.environ.get("CH_PORT", 8123)),
        username=os.environ.get("CH_USER", "default"),
        password=os.environ.get("CH_PASSWORD", ""),
        database=database,
        **kwargs,
    )


# --- Schema -----------------------------------------------------------------
#
# `sst_anom` is ordered (gy, gx, date) because a point timeseries — every day at
# one cell, ~16k rows out of ~1.6e9 — is the query that is unaffordable any
# other way. Whole-day map reads want the opposite order, so they are served by
# the `by_date` projection rather than a second table; ClickHouse keeps it in
# sync on insert and picks it automatically.
#
# anom is stored as the source's raw Int16 counts (lossless, 2 bytes, compresses
# far better than Float32) with an ALIAS column doing the *0.01 so queries can
# just say `anom`. ALIAS columns cost no storage.

DDL: tuple[str, ...] = (
    f"CREATE DATABASE IF NOT EXISTS {DATABASE}",
    f"""
    CREATE TABLE IF NOT EXISTS {DATABASE}.sst_anom
    (
        date      Date    CODEC(DoubleDelta, ZSTD(3)),
        gy        UInt16  CODEC(DoubleDelta, ZSTD(3)),
        gx        UInt16  CODEC(DoubleDelta, ZSTD(3)),
        anom_raw  Int16   CODEC(ZSTD(3)),

        anom      Float32 ALIAS anom_raw * 0.01,
        lat       Float32 ALIAS -89.875 + gy * 0.25,
        lon       Float32 ALIAS 0.125 + gx * 0.25,

        PROJECTION by_date
        (
            SELECT date, gy, gx, anom_raw
            ORDER BY (date, gy, gx)
        )
    )
    ENGINE = MergeTree
    PARTITION BY toYear(date)
    ORDER BY (gy, gx, date)
    """,
    f"""
    CREATE TABLE IF NOT EXISTS {DATABASE}.ingest_status
    (
        date            Date,
        filename        String,
        status          LowCardinality(String),
        is_preliminary  UInt8,
        n_rows          UInt32,
        file_mtime      DateTime,
        file_size       UInt64,
        message         String,
        updated_at      DateTime DEFAULT now()
    )
    ENGINE = ReplacingMergeTree(updated_at)
    ORDER BY date
    """,
)

# Status values used by process/OISST. A preliminary file that is later replaced
# by the final one re-enters at `pending_ingest`; ReplacingMergeTree keyed on
# `date` means the status table always shows one row per day.
STATUS_PENDING = "pending_ingest"
STATUS_INGESTING = "ingesting"
STATUS_SUCCESS = "success_ingest"
STATUS_FAILED = "failed_ingest"


def ensure_schema(client=None) -> None:
    """Create the database and tables if they do not exist. Idempotent."""
    owned = client is None
    if owned:
        # The database may not exist yet, so connect without selecting one.
        client = get_client(database="")
    try:
        for statement in DDL:
            client.command(statement)
    finally:
        if owned:
            client.close()
