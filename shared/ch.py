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
# THERE ARE NO PROJECTIONS HERE, AND THAT IS THE CENTRAL DESIGN DECISION.
#
# The previous (OISST) schema carried a `by_date` projection so a whole-day map
# read did not have to scan a table ordered for point timeseries. Measured, that
# projection was 1.47 of 2.22 bytes per row — 66% of total storage. At
# CoralTemp's 7.5M cells/day over 15,211 days it would have cost ~150 GB.
#
# It is gone because **images are no longer rendered from the database**. The
# daily NetCDF is still on disk when `process` renders that day's frames, so the
# renderer reads the file directly (see `shared/fields.py`). ClickHouse is left
# serving only what it is ordered for: point and box timeseries.
#
# The cost of this, stated plainly: re-rendering a historical bucket without its
# NetCDF is a partition scan. Pre-rendered images are therefore the durable
# artifact, and a mass re-render means re-downloading the range. `/image` refuses
# rather than hangs — see api/SERVER.py.
#
# SST is stored as the source's raw Int16 counts (lossless, 2 bytes, compresses
# far better than Float32) with an ALIAS doing the *0.01. ALIAS columns cost no
# storage, so queries read naturally as `sst`.

# One row per date, per archive. Written as a template because there are two
# archives with identical bookkeeping — see the `mhw_status` entry below.
_STATUS_DDL = """
    CREATE TABLE IF NOT EXISTS {database}.{table}
    (
        date             Date,
        filename         String,
        status           LowCardinality(String),
        n_rows           UInt32,
        file_size        UInt64,
        source_url       String,
        remote_size      UInt64,
        remote_modified  String,
        message          String,
        updated_at       DateTime DEFAULT now()
    )
    ENGINE = ReplacingMergeTree(updated_at)
    ORDER BY date
"""

DDL: tuple[str, ...] = (
    f"CREATE DATABASE IF NOT EXISTS {DATABASE}",
    # One row per (date, ocean cell) inside the Pacific subset. Ordered
    # (gy, gx, date) because a point timeseries — every day at one cell, ~15k
    # rows out of ~114 billion — is the query that is unaffordable any other
    # way. A box lands as one contiguous key range per `gy`, not a scan.
    f"""
    CREATE TABLE IF NOT EXISTS {DATABASE}.sst_daily
    (
        date      Date    CODEC(DoubleDelta, ZSTD(3)),
        gy        UInt16  CODEC(DoubleDelta, ZSTD(3)),
        gx        UInt16  CODEC(DoubleDelta, ZSTD(3)),
        sst_raw   Int16   CODEC(ZSTD(3)),

        -- Whether this cell has a climatology for this date's MMDD, and so
        -- whether an anomaly exists for it. About 3.2% of the box's ocean does
        -- not: the seasonal ice fringe, which the source flags explicitly.
        --
        -- This is per (cell, date) and NOT a static property of the cell,
        -- because the ice edge moves through the year. It is what makes the
        -- identity `mean(sst - clim) == mean(sst) - mean(clim)` exact over a
        -- box: both sides must average the same cells. Without the filter the
        -- two drift apart wherever the ice edge sits, which is precisely the
        -- region a marine-heatwave dashboard is asked about.
        has_clim  UInt8   CODEC(ZSTD(3)),

        sst       Float32 ALIAS sst_raw * 0.01,
        lat       Float32 ALIAS -89.975 + gy * 0.05,
        lon       Float32 ALIAS 0.025 + gx * 0.05
    )
    ENGINE = MergeTree
    PARTITION BY toYear(date)
    ORDER BY (gy, gx, date)
    """,
    # The 1991-2020 daily climatology, one row per (day-of-year, ocean cell).
    # 366 keys including 02-29, so there is no leap-day rule to invent.
    #
    # Ordered (gy, gx, mmdd) — the TIMESERIES ordering, deliberately not the
    # whole-day one. Image generation reads climatology straight from the 366
    # NetCDF files, which are small enough (1.6 GB) to keep forever, so nothing
    # ever needs a fast `WHERE mmdd = ...` across all cells.
    f"""
    CREATE TABLE IF NOT EXISTS {DATABASE}.sst_clim
    (
        mmdd      UInt16  CODEC(DoubleDelta, ZSTD(3)),
        gy        UInt16  CODEC(DoubleDelta, ZSTD(3)),
        gx        UInt16  CODEC(DoubleDelta, ZSTD(3)),
        clim_raw  Int16   CODEC(ZSTD(3)),

        clim      Float32 ALIAS clim_raw * 0.01,
        lat       Float32 ALIAS -89.975 + gy * 0.05,
        lon       Float32 ALIAS 0.025 + gx * 0.05
    )
    ENGINE = MergeTree
    ORDER BY (gy, gx, mmdd)
    """,
    # Per named region, the cos(lat)-weighted climatology mean for each MMDD:
    # 8 regions x 366 = 2,928 rows, computed once at `init`.
    #
    # This is the whole precomputation layer. A region ANOMALY series does not
    # join anything, because mean(sst - clim) = mean(sst) - mean(clim) when both
    # average the same cells — so the daily side stays a live query and only
    # this tiny climatology side is materialised.
    f"""
    CREATE TABLE IF NOT EXISTS {DATABASE}.region_clim
    (
        region      LowCardinality(String),
        mmdd        UInt16,
        mean_clim   Float32,
        n_cells     UInt32,
        updated_at  DateTime DEFAULT now()
    )
    ENGINE = ReplacingMergeTree(updated_at)
    ORDER BY (region, mmdd)
    """,
    # Per named region, the area means for each DATE: 8 regions x 15,211 days =
    # ~121,688 rows. This is the rollup that `region_clim` is not.
    #
    # `region_clim` above precomputes the CLIMATOLOGY side of a region anomaly,
    # measured at 0.296 s live against a daily side of 12.14 s for Nino 3.4 — so
    # it removes 2.4% of the request. This table is the other 97.6%: the query it
    # replaces runs 3.14 s for the smallest named region and 12.14 s for Nino
    # 3.4, which is not a latency anything interactive can be built on.
    #
    # THERE ARE TWO SST COLUMNS AND THEY ARE NOT REDUNDANT. `region_timeseries()`
    # restricts the daily mean to `has_clim = 1` for `anom` and deliberately does
    # NOT for `sst`, so the two variables average different cell sets — and the
    # gap is not small, because the climatological ice edge moves through the
    # year. The Bering Sea box holds 70,166 ocean cells but only 12,086 with a
    # climatology on 15 March. Serving `anom` from `mean_sst` would break the
    # `mean(sst - clim) == mean(sst) - mean(clim)` identity exactly where a
    # marine-heatwave question gets asked.
    #
    # `mean_mhw` is the cos(lat)-weighted mean category over the box's OCEAN, not
    # over its heatwave cells: numerator from the sparse `mhw_daily`, denominator
    # from `sst_daily`, which is the only table that knows which cells are ocean
    # on a given date. A date with no heatwave anywhere in the box is a real 0.
    #
    # Nothing here is a bucket. Weekly and monthly reduction stays in
    # `region_timeseries()`, which folds these daily rows in Python through
    # `shared.periods` — so the rollup cannot drift from the point path's idea of
    # what a week is.
    f"""
    CREATE TABLE IF NOT EXISTS {DATABASE}.region_daily
    (
        region         LowCardinality(String),
        date           Date    CODEC(DoubleDelta, ZSTD(3)),

        -- Over every ocean cell in the box. Serves `sst`.
        mean_sst       Float32,
        n_cells        UInt32,

        -- Over the box's `has_clim = 1` cells only. Serves `anom`, after
        -- subtracting `region_clim` for the date's MMDD.
        mean_sst_clim  Float32,
        n_cells_clim   UInt32,

        -- sum(cat * cos(lat)) over mhw_daily / sum(cos(lat)) over sst_daily.
        mean_mhw       Float32,

        updated_at     DateTime DEFAULT now()
    )
    ENGINE = ReplacingMergeTree(updated_at)
    ORDER BY (region, date)
    """,
    # NOAA CRW Marine Heatwave category, one row per (date, cell) — but ONLY for
    # cells actually in a heatwave.
    #
    # Land (-127), ice (-1) and heatwave-free ocean (0) are all dropped, which is
    # what makes this table affordable: measured over 40 random days spanning the
    # archive, the Pacific box averages 1,592,012 cells at category >= 1 per day
    # against 7,477,923 ocean cells — so ~24.2 B rows for the full archive rather
    # than the ~113.7 B `sst_daily` carries.
    #
    # The cost of that sparsity, stated plainly: **absence is ambiguous**. A
    # missing (date, cell) is heatwave-free ocean, ice, or land, and this table
    # cannot say which. Every query therefore reads it as the RIGHT side of a
    # LEFT JOIN against `sst_daily`, which holds exactly the ocean cells — that
    # join supplies the zeros and excludes the land, and both tables are ordered
    # `(gy, gx, date)`, so at a point it is a primary-key read on both sides.
    #
    # Same ORDER BY as `sst_daily`, and same reason: a point timeseries is the
    # query that is unaffordable any other way, and a box is one contiguous key
    # range per `gy`. Same absence of a projection, too.
    f"""
    CREATE TABLE IF NOT EXISTS {DATABASE}.mhw_daily
    (
        date      Date    CODEC(DoubleDelta, ZSTD(3)),
        gy        UInt16  CODEC(DoubleDelta, ZSTD(3)),
        gx        UInt16  CODEC(DoubleDelta, ZSTD(3)),

        -- The source's own category, 1..5 (Moderate .. Beyond Extreme). Stored
        -- unscaled: `heatwave_category` is already an ordinal class, so unlike
        -- `sst_raw` there is no ALIAS undoing a scale factor.
        cat       UInt8   CODEC(ZSTD(3)),

        lat       Float32 ALIAS -89.975 + gy * 0.05,
        lon       Float32 ALIAS 0.025 + gx * 0.05
    )
    ENGINE = MergeTree
    PARTITION BY toYear(date)
    ORDER BY (gy, gx, date)
    """,
    # One row per date. `remote_size`/`remote_modified` are what a re-check
    # compares against: CoralTemp v3.1_op is a near-real-time stream whose files
    # can be revised in place, and the local NetCDF is deleted after ingest, so
    # staleness can only be detected by a HEAD against the source.
    _STATUS_DDL.format(database=DATABASE, table="ingest_status"),
    # The same table again for the MHW archive. A second TABLE rather than a
    # `product` column in `ingest_status`, because that table is `ORDER BY date`
    # with one row per date — adding a product would have to go into the sorting
    # key, which cannot be altered in place, and the two archives genuinely do
    # move independently: MHW is published about 90 minutes after CoralTemp on the
    # same day, so a `run` that lands between the two sees a date as SST-ingested
    # and MHW-pending.
    _STATUS_DDL.format(database=DATABASE, table="mhw_status"),
)

# Status values used by process/CRW. ReplacingMergeTree keyed on `date` means
# the table always shows one row per day.
STATUS_PENDING_DOWNLOAD = "pending_download"
STATUS_DOWNLOADING = "downloading"
STATUS_SUCCESS_DOWNLOAD = "success_download"
STATUS_FAILED_DOWNLOAD = "failed_download"

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
