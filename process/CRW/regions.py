"""Build `region_daily`: per named region, the daily area means the API serves.

### Why this table exists

`region_clim` next door precomputes the *climatology* side of a region anomaly.
Measured against the full archive, that side costs 0.296 s live and the daily
side costs 12.14 s for Nino 3.4 — so the existing precomputation removes 2.4% of
the request and this one removes the rest. The live daily query runs 3.14 s for
the smallest named region (Nino 1+2, 38,455 cells) and 12.14 s for Nino 3.4
(201,392 cells); the North Pacific PDO box is 4.5x larger again. None of that is
a latency an interactive panel can be built on.

The result is ~121,688 rows — 8 regions x 15,211 days — against the
113.77 billion in `sst_daily`.

### What it does NOT do

**No bucketing.** Weekly and monthly reduction stays in `region_timeseries()`,
which folds these daily rows in Python through `shared.periods`. Materialising
weeks here would put a second definition of "a week" in the codebase, which is
the drift that retiring `api/prerender.py` was meant to end.

**No arbitrary boxes.** `/regionTimeseries` keeps the live path — the rollup is
keyed by region name and only the eight named regions have one.

### The two SST columns

`region_timeseries()` restricts the daily mean to `has_clim = 1` for `anom` and
deliberately does not for `sst`, so the two variables average different cell
sets. The gap is not small and it is not static: the climatological ice edge
moves through the year, so the Bering Sea box holds 70,166 ocean cells but only
12,086 with a climatology on 15 March. One column serving both would break the
`mean(sst - clim) == mean(sst) - mean(clim)` identity precisely over the ice
fringe, which is where a marine-heatwave question gets asked.

### The two marine-heatwave columns

`mean_mhw` is the area mean of NOAA's five ordinal classes and `mhw_area_frac`
the share of the box's ocean area at category >= 1. Both come off the same scan
and the same denominator, and they answer different questions: the first mixes
severity with extent so completely that half a box at Cat 1 and a tenth of it at
Cat 5 both come to about 0.5, while the second is simply how much of the box is
affected. A region's `mhw` series plots the second; the first survives as the
only column that carries severity at all, shown beside it as a stat card.

`mean_sst_clim` is NaN when a region has no climatology cells at all on a date.
That cannot happen for the eight regions configured today (the smallest is the
Bering Sea's 8,764), but a future box further north could, and a silent 0 there
would read as "the region is exactly at its climatology".
"""

from __future__ import annotations

import datetime as dt
import logging

from shared.ch import DATABASE
from shared.domain import global_grid, regions
from shared.mask import cells_in

log = logging.getLogger(__name__)

COLUMNS = (
    "region",
    "date",
    "mean_sst",
    "n_cells",
    "mean_sst_clim",
    "n_cells_clim",
    "mean_mhw",
    "mhw_area_frac",
    "n_cells_mhw",
)

# cos(latitude) area weight. At 60N a 0.05-degree cell covers half the area of
# one at the equator, so a plain avg() over-weights the poleward end of any box
# tall enough to matter. `lat` is an ALIAS column on both daily tables.
_WEIGHT = "cos(lat * pi() / 180)"


def box_of(region) -> tuple[int, int, int, int]:
    """The region's inclusive (gy0, gy1, gx0, gx1) on the GLOBAL grid.

    Via `shared.domain` rather than hand-rolled arithmetic — cell identity does
    not depend on the current subset, and the 0-360 longitude convention is
    applied in exactly one place.

    For a POLYGON region this is the bounding box, which is the prefilter and not
    the region: `mask_filter()` below narrows it to the cells actually inside.
    """
    grid = global_grid()
    return (*region.gy_range(grid), *region.gx_range(grid))


def mask_filter(region) -> str:
    """The SQL clause that cuts a box down to a polygon region's own cells.

    Empty for a plain box, so every query below reads the same whether or not the
    region has a mask — one aggregation, not two that could drift into computing
    different means.

    A set membership rather than a join: `region_cells` holds 26,158 rows for the
    BC EEZ, ClickHouse builds the tuple set once and probes it per row, and the
    `gy`/`gx` BETWEEN in front of it still does the primary-key work.
    """
    if not region.masked:
        return ""
    return (
        " AND (gy, gx) IN ("
        f"SELECT gy, gx FROM {DATABASE}.region_cells FINAL WHERE region = %(region)s)"
    )


def build_region_cells(client, keys: list[str] | None = None) -> dict[str, int]:
    """Rasterise every polygon region into `region_cells`.

    Cheap and idempotent — 26,158 rows for the only region that has one today,
    and a `ReplacingMergeTree` on (region, gy, gx) — so it is re-run rather than
    checked: `init` and `rollup` both call it, and a changed polygon needs no
    migration beyond running it again.

    **Old cells are deleted first.** Replacing collapses rows that are still
    there; a polygon that SHRANK would otherwise leave the cells it no longer
    covers behind, quietly averaging water outside the zone. A region with no
    polygon is deleted and not rewritten, which is what demoting a polygon region
    back to a box needs.
    """
    grid = global_grid()
    selected = regions() if keys is None else {k: regions()[k] for k in keys}
    counts: dict[str, int] = {}

    for key, region in selected.items():
        client.command(
            f"DELETE FROM {DATABASE}.region_cells WHERE region = %(region)s",
            parameters={"region": key},
        )
        if not region.masked:
            continue
        gy, gx = cells_in(region, grid)
        client.insert(
            f"{DATABASE}.region_cells",
            [[key, int(y), int(x)] for y, x in zip(gy, gx)],
            column_names=["region", "gy", "gx"],
        )
        counts[key] = int(len(gy))
        log.info("region_cells: %s -> %d cell(s)", key, len(gy))

    return counts


def _date_filter(start: dt.date | None, end: dt.date | None) -> tuple[str, dict]:
    clauses, params = "", {}
    if start:
        clauses += " AND date >= %(start)s"
        params["start"] = start
    if end:
        clauses += " AND date <= %(end)s"
        params["end"] = end
    return clauses, params


def build_region_daily(
    client,
    keys: list[str] | None = None,
    start: dt.date | None = None,
    end: dt.date | None = None,
) -> dict:
    """Roll up `sst_daily` + `mhw_daily` into `region_daily`, one region at a time.

    Server-side throughout: an INSERT ... SELECT per region, so 113.77 billion
    rows are reduced inside ClickHouse and only the ~15k results per region are
    ever materialised. No data crosses into Python.

    The SST side drives the join and the MHW side is LEFT JOINed onto it, for the
    same reason every other MHW query is shaped that way: `mhw_daily` is sparse,
    and only `sst_daily` knows which cells are ocean on a given date. A date with
    no heatwave anywhere in the box gets no right-hand row, ClickHouse supplies a
    0 numerator, and the region reports a real mean of 0 — not a gap. A date
    present in `mhw_daily` but not yet in `sst_daily` (the two archives publish
    ~90 minutes apart) produces no row at all, which is correct: there is no
    ocean denominator to divide by yet.

    Re-running over a range is safe. `ReplacingMergeTree(updated_at)` collapses
    the re-inserted rows and every read uses FINAL.
    """
    selected = regions() if keys is None else {k: regions()[k] for k in keys}
    where, date_params = _date_filter(start, end)
    counts: dict[str, int] = {}

    for key, region in selected.items():
        gy0, gy1, gx0, gx1 = box_of(region)
        params = date_params | {"gy0": gy0, "gy1": gy1, "gx0": gx0, "gx1": gx1, "region": key}
        # Appended to BOTH halves. Applying it to only one would divide a masked
        # numerator by an unmasked denominator, which is a plausible-looking
        # extent rather than an error.
        mask = mask_filter(region)

        client.command(
            f"""
            INSERT INTO {DATABASE}.region_daily
                (region, date, mean_sst, n_cells, mean_sst_clim, n_cells_clim,
                 mean_mhw, mhw_area_frac, n_cells_mhw)
            SELECT %(region)s,
                   s.date,
                   s.mean_sst,
                   s.n_cells,
                   s.mean_sst_clim,
                   s.n_cells_clim,
                   m.weighted / s.weight_total,
                   m.weighted_area / s.weight_total,
                   m.n_mhw
            FROM (
                SELECT date,
                       sum(sst * {_WEIGHT}) / sum({_WEIGHT}) AS mean_sst,
                       count() AS n_cells,
                       sum({_WEIGHT}) AS weight_total,
                       countIf(has_clim = 1) AS n_cells_clim,
                       if(n_cells_clim > 0,
                          sumIf(sst * {_WEIGHT}, has_clim = 1)
                              / sumIf({_WEIGHT}, has_clim = 1),
                          nan) AS mean_sst_clim
                FROM {DATABASE}.sst_daily
                WHERE gy BETWEEN %(gy0)s AND %(gy1)s
                  AND gx BETWEEN %(gx0)s AND %(gx1)s{mask}{where}
                GROUP BY date
            ) AS s
            LEFT JOIN (
                SELECT date,
                       sum(cat * {_WEIGHT}) AS weighted,
                       -- No `If` needed: `mhw_daily` holds cat >= 1 only, so
                       -- every row in it is a heatwave cell by construction.
                       -- Testing `cat >= 1` here would read as though the table
                       -- carried zeros, which is the misreading its sparsity
                       -- causes everywhere else.
                       sum({_WEIGHT}) AS weighted_area,
                       count() AS n_mhw
                FROM {DATABASE}.mhw_daily
                WHERE gy BETWEEN %(gy0)s AND %(gy1)s
                  AND gx BETWEEN %(gx0)s AND %(gx1)s{mask}{where}
                GROUP BY date
            ) AS m ON s.date = m.date
            """,
            parameters=params,
        )

        n = client.query(
            f"SELECT count() FROM {DATABASE}.region_daily FINAL WHERE region = %(region)s",
            parameters={"region": key},
        ).result_rows[0][0]
        counts[key] = int(n)
        log.info("region_daily: %s -> %d row(s)", key, n)

    return counts


def truncate(client, keys: list[str] | None = None) -> None:
    """Drop the rollup, for a rebuild after a domain or weighting change."""
    if keys is None:
        client.command(f"TRUNCATE TABLE IF EXISTS {DATABASE}.region_daily")
        log.info("region_daily: truncated")
        return
    for key in keys:
        client.command(
            f"DELETE FROM {DATABASE}.region_daily WHERE region = %(region)s",
            parameters={"region": key},
        )
        log.info("region_daily: deleted %s", key)


def optimize(client) -> None:
    """Collapse the ReplacingMergeTree so FINAL has nothing left to do.

    Cheap here and worth doing after a bulk build: the whole table is ~121,688
    rows, and a rebuild over an existing range doubles it until merged.
    """
    client.command(f"OPTIMIZE TABLE {DATABASE}.region_daily FINAL")


def last_rolled(client, key: str | None = None) -> dt.date | None:
    """The most recent date present, for `run` to append from."""
    where = " WHERE region = %(region)s" if key else ""
    row = client.query(
        f"SELECT max(date) FROM {DATABASE}.region_daily FINAL{where}",
        parameters={"region": key} if key else {},
    ).result_rows[0]
    return row[0] if row and row[0] and row[0].year > 1970 else None
