"""Point and area-mean SST / SST-anomaly timeseries out of ClickHouse.

`sst` is stored; `anom` is derived as `sst - climatology(mmdd)` at query time.
How that derivation is done differs by query shape, and the difference is the
main thing worth understanding here:

* **A point** joins `sst_daily` to `sst_clim` on `(gy, gx, mmdd)`. Both tables
  are ordered `(gy, gx, ...)`, so one cell is ~15k rows against 366 — a
  primary-key read on both sides, and a trivial join.

* **A box** never joins, because the means commute:

      mean(sst - clim) == mean(sst) - mean(clim)

  over the same cells with the same cos(lat) weights. So the daily side stays a
  plain aggregation and the climatology side collapses to one value per MMDD.
  Joining instead would put box_cells x 366 rows on the right of a hash join —
  461M for the PDO box — for a result that is 366 numbers.

  The identity's precondition is that both sides average the *same* cells, which
  is what `sst_daily.has_clim` is for: about 3.2% of the box's ocean has SST but
  no climatology, and the ice edge that defines it moves through the year.
"""

from __future__ import annotations

import datetime as dt

from shared.domain import global_grid, regions, subset, variable

from .clickhouse_helpers import DATABASE, client
from .periods import Period, bucket_sql

# Queryable variables. `sst` is a stored ALIAS column; `anom` is derived.
VARIABLES: tuple[str, ...] = ("sst", "anom")


def check_variable(name: str) -> str:
    if name not in VARIABLES:
        raise ValueError(f"unknown variable {name!r}; known: {list(VARIABLES)}")
    return name


class OutsideDomainError(RuntimeError):
    """Raised when a requested point falls outside the ingested subset box."""

    def __init__(self, message: str, lat: float, lon: float):
        super().__init__(message, lat, lon)
        self.message = message
        self.lat = lat
        self.lon = lon


def _require_inside(lat: float, lon: float) -> None:
    box = subset()
    if not box.contains(lat, lon):
        raise OutsideDomainError(
            f"({lat:.3f}, {lon:.3f}) is outside the ingested domain "
            f"({box.lat_min}..{box.lat_max}N, {box.lon_min}..{box.lon_max}E)",
            lat,
            lon,
        )


def _date_filter(start: dt.date | None, end: dt.date | None, alias: str = "") -> tuple[str, dict]:
    prefix = f"{alias}." if alias else ""
    clauses, params = [], {}
    if start is not None:
        clauses.append(f"{prefix}date >= %(start)s")
        params["start"] = start
    if end is not None:
        clauses.append(f"{prefix}date <= %(end)s")
        params["end"] = end
    return ("".join(f" AND {c}" for c in clauses), params)


# ClickHouse expression for a date's climatology key, matching
# `shared.fields.mmdd_of`. Change one and change the other.
MMDD_SQL = "toMonth({d}) * 100 + toDayOfMonth({d})"


def point_timeseries(
    lat: float,
    lon: float,
    start: dt.date | None = None,
    end: dt.date | None = None,
    period: Period = "daily",
    variable_name: str = "sst",
) -> dict:
    """Every ingested day at the grid cell nearest `(lat, lon)`.

    Hits the `(gy, gx, date)` primary key head-on, so this reads only the ~15k
    rows for that one cell rather than scanning the table. `period` averages
    those rows into weekly or monthly buckets, labelled by their first day.
    """
    _require_inside(lat, lon)
    name = check_variable(variable_name)

    grid = global_grid()
    gy, gx = int(grid.gy(lat)), int(grid.gx(lon))

    if name == "sst":
        where, params = _date_filter(start, end)
        params |= {"gy": gy, "gx": gx}
        sql = f"""
            SELECT {bucket_sql(period)} AS bucket,
                   avg(sst) AS value,
                   count() AS n
            FROM {DATABASE}.sst_daily
            WHERE gy = %(gy)s AND gx = %(gx)s{where}
            GROUP BY bucket ORDER BY bucket
        """
    else:
        where, params = _date_filter(start, end, alias="d")
        params |= {"gy": gy, "gx": gx}
        bucket = bucket_sql(period).replace("date", "d.date")
        sql = f"""
            SELECT {bucket} AS bucket,
                   avg(d.sst - c.clim) AS value,
                   count() AS n
            FROM {DATABASE}.sst_daily AS d
            INNER JOIN {DATABASE}.sst_clim AS c
                ON c.gy = d.gy AND c.gx = d.gx
               AND c.mmdd = {MMDD_SQL.format(d="d.date")}
            WHERE d.gy = %(gy)s AND d.gx = %(gx)s{where}
            GROUP BY bucket ORDER BY bucket
        """

    rows = client().query(sql, parameters=params).result_rows
    return {
        "variable": name,
        "units": variable(name).units,
        "period": period,
        "requested": {"lat": lat, "lon": lon},
        "cell": {
            "gy": gy,
            "gx": gx,
            "lat": float(grid.lat(gy)),
            "lon": float(grid.lon(gx)),
        },
        "dates": [str(r[0]) for r in rows],
        "values": [None if r[1] is None else round(float(r[1]), 2) for r in rows],
        "n_days": [int(r[2]) for r in rows],
    }


def _box_clim_by_mmdd(gy0: int, gy1: int, gx0: int, gx1: int) -> dict[int, float]:
    """cos(lat)-weighted climatology mean per MMDD over an arbitrary box.

    366 rows out of `sst_clim`. This is what `region_clim` stores for the named
    regions; an arbitrary box computes it on the fly instead, which is still far
    cheaper than joining the daily table.
    """
    rows = client().query(
        f"""
        SELECT mmdd,
               sum(clim * cos(lat * pi() / 180)) / sum(cos(lat * pi() / 180)) AS mean_clim
        FROM {DATABASE}.sst_clim
        WHERE gy BETWEEN %(gy0)s AND %(gy1)s AND gx BETWEEN %(gx0)s AND %(gx1)s
        GROUP BY mmdd
        """,
        parameters={"gy0": gy0, "gy1": gy1, "gx0": gx0, "gx1": gx1},
    ).result_rows
    return {int(m): float(v) for m, v in rows if v is not None}


def _region_clim_by_mmdd(key: str) -> dict[int, float]:
    """The precomputed climatology means for a named region."""
    rows = client().query(
        f"SELECT mmdd, mean_clim FROM {DATABASE}.region_clim FINAL WHERE region = %(k)s",
        parameters={"k": key},
    ).result_rows
    return {int(m): float(v) for m, v in rows}


def region_timeseries(
    lat_bounds: tuple[float, float],
    lon_bounds: tuple[float, float],
    start: dt.date | None = None,
    end: dt.date | None = None,
    label: str | None = None,
    period: Period = "daily",
    variable_name: str = "sst",
    clim_by_mmdd: dict[int, float] | None = None,
) -> dict:
    """cos(lat)-weighted mean over a lat/lon box, per `period` bucket.

    Cells are weighted by cos(latitude): at 60N a 0.05-degree cell covers half
    the area of one at the equator, so a plain average would over-weight the
    poleward end of any box tall enough to matter.

    For `anom` the daily mean is restricted to `has_clim = 1` and the
    climatology mean for each contributing day is subtracted — see the module
    docstring for why this is not a join.
    """
    name = check_variable(variable_name)
    grid = global_grid()
    gy0, gy1 = sorted(int(grid.gy(v)) for v in lat_bounds)
    gx0, gx1 = sorted(int(grid.gx(v)) for v in lon_bounds)

    where, params = _date_filter(start, end)
    params |= {"gy0": gy0, "gy1": gy1, "gx0": gx0, "gx1": gx1}
    clim_filter = " AND has_clim = 1" if name == "anom" else ""

    # For anomaly the per-day SST mean has to be formed before bucketing, so the
    # climatology can be subtracted day by day; weekly/monthly means are then the
    # mean of those daily anomalies. For SST the bucket mean can be taken
    # directly.
    rows = client().query(
        f"""
        SELECT date,
               sum(sst * cos(lat * pi() / 180)) / sum(cos(lat * pi() / 180)) AS mean_sst,
               count() AS n_cells
        FROM {DATABASE}.sst_daily
        WHERE gy BETWEEN %(gy0)s AND %(gy1)s
          AND gx BETWEEN %(gx0)s AND %(gx1)s{clim_filter}{where}
        GROUP BY date ORDER BY date
        """,
        parameters=params,
    ).result_rows

    if name == "anom":
        clim = clim_by_mmdd if clim_by_mmdd is not None else _box_clim_by_mmdd(gy0, gy1, gx0, gx1)
    else:
        clim = None

    # Fold days into buckets in Python: the daily list is at most ~15k long, and
    # doing it here keeps one bucketing implementation (shared.periods) rather
    # than duplicating the SQL branch.
    from .periods import start_of

    buckets: dict[dt.date, list[float]] = {}
    cells: dict[dt.date, int] = {}
    for date, mean_sst, n_cells in rows:
        if mean_sst is None:
            continue
        value = float(mean_sst)
        if clim is not None:
            key = date.month * 100 + date.day
            if key not in clim:
                continue
            value -= clim[key]
        b = start_of(date, period)
        buckets.setdefault(b, []).append(value)
        cells[b] = int(n_cells)

    ordered = sorted(buckets)
    return {
        "variable": name,
        "units": variable(name).units,
        "period": period,
        "label": label,
        "bounds": {"lat": list(lat_bounds), "lon": list(lon_bounds)},
        "dates": [str(b) for b in ordered],
        "values": [round(sum(v) / len(v), 3) for b in ordered for v in [buckets[b]]],
        "n_days": [len(buckets[b]) for b in ordered],
        "n_cells": [cells[b] for b in ordered],
    }


def named_region_timeseries(
    key: str,
    start: dt.date | None = None,
    end: dt.date | None = None,
    period: Period = "daily",
    variable_name: str = "sst",
) -> dict:
    region = regions()[key]
    clim = _region_clim_by_mmdd(key) if variable_name == "anom" else None
    result = region_timeseries(
        region.lat,
        region.lon,
        start,
        end,
        label=region.label,
        period=period,
        variable_name=variable_name,
        clim_by_mmdd=clim,
    )
    result["region"] = key
    result["partial"] = region.partial
    return result


def coverage() -> dict:
    """The ingested date range and row count."""
    row = client().query(
        f"SELECT count(), min(date), max(date) FROM {DATABASE}.sst_daily"
    ).result_rows[0]
    if not row[0]:
        return {"rows": 0, "start": None, "end": None, "days": 0, "climatology": None}
    days = client().query(
        f"SELECT uniqExact(date) FROM {DATABASE}.ingest_status FINAL "
        "WHERE status = 'success_ingest'"
    ).result_rows[0][0]
    clim_keys = client().query(
        f"SELECT uniqExact(mmdd) FROM {DATABASE}.sst_clim"
    ).result_rows[0][0]
    return {
        "rows": int(row[0]),
        "start": str(row[1]),
        "end": str(row[2]),
        "days": int(days),
        # Anomaly is unavailable until all 366 keys are loaded; the frontend
        # uses this to decide whether to offer the variable at all.
        "climatology": {"keys": int(clim_keys), "complete": int(clim_keys) == 366},
    }


def _month_end(day: dt.date) -> dt.date:
    """Last day of `day`'s month -- day 32 always lands in the next one."""
    return (day.replace(day=1) + dt.timedelta(days=32)).replace(day=1) - dt.timedelta(days=1)


def _archive_edges() -> tuple[dt.date, dt.date] | None:
    """First and last day present in `sst_daily`, or None on an empty table."""
    lo, hi = client().query(
        f"SELECT min(date), max(date) FROM {DATABASE}.sst_daily"
    ).result_rows[0]
    return None if lo is None else (lo, hi)


def _partial_months(lo: dt.date, hi: dt.date) -> set[tuple[int, int]]:
    """The (year, month) pairs the archive covers only partly, at either edge.

    At most two: the month the record starts in and the month it ends in. Both
    are still *ranked* -- the month in progress is the one people most want to
    look at -- but flagged so the chart can star them and say why.

    A month missing an *interior* day is **not** partial: it is as complete as
    it will ever be, and only a truncated edge month is still going to change.
    """
    partial = set()
    if lo.day != 1:
        partial.add((lo.year, lo.month))
    if hi != _month_end(hi):
        partial.add((hi.year, hi.month))
    return partial


def monthly_ranking(
    lat: float, lon: float, top: int = 10, variable_name: str = "anom"
) -> dict:
    """Every calendar month at the nearest cell, ranked within its month-of-year.

    One row per (month-of-year, year): the mean daily value, the standard
    deviation of the daily values inside that month, and the year's rank among
    all years for that month, warmest first.

    `sd` is day-to-day spread at a single cell, so it is much wider than the same
    statistic on an area mean -- spatial averaging cancels daily noise that one
    cell keeps. That is signal about the cell, not error in the mean.

    Reads only the ~15k rows for one cell: `ORDER BY (gy, gx, date)` puts the
    whole record for a point in one contiguous run, so all twelve months come
    back from a single query and need no precomputation.
    """
    _require_inside(lat, lon)
    name = check_variable(variable_name)

    grid = global_grid()
    gy, gx = int(grid.gy(lat)), int(grid.gx(lon))
    months: dict[str, list[dict]] = {str(m): [] for m in range(1, 13)}
    edges = _archive_edges()

    if edges is not None:
        lo, hi = edges
        partial = _partial_months(lo, hi)
        if name == "sst":
            source = f"""
                SELECT date, sst AS value FROM {DATABASE}.sst_daily
                WHERE gy = %(gy)s AND gx = %(gx)s
            """
        else:
            source = f"""
                SELECT d.date AS date, d.sst - c.clim AS value
                FROM {DATABASE}.sst_daily AS d
                INNER JOIN {DATABASE}.sst_clim AS c
                    ON c.gy = d.gy AND c.gx = d.gx
                   AND c.mmdd = {MMDD_SQL.format(d="d.date")}
                WHERE d.gy = %(gy)s AND d.gx = %(gx)s
            """
        rows = client().query(
            f"""
            SELECT toMonth(date) AS month,
                   toYear(date)  AS year,
                   avg(value)    AS mean_value,
                   stddevSamp(value) AS sd,
                   count()       AS n,
                   row_number() OVER (
                       PARTITION BY toMonth(date) ORDER BY avg(value) DESC
                   ) AS rank
            FROM ({source})
            GROUP BY month, year
            ORDER BY month, rank
            """,
            parameters={"gy": gy, "gx": gx},
        ).result_rows

        for month, year, mean_value, sd, n, rank in rows:
            months[str(month)].append({
                "year": int(year),
                "mean": round(float(mean_value), 3),
                "sd": None if sd is None else round(float(sd), 3),
                "n": int(n),
                "rank": int(rank),
                # Truncated by the edge of the archive, so its mean is over a
                # part-month and its rank will move as the rest lands.
                "partial": (int(year), int(month)) in partial,
            })

    return {
        "variable": name,
        "units": variable(name).units,
        "requested": {"lat": lat, "lon": lon},
        "cell": {
            "gy": gy,
            "gx": gx,
            "lat": float(grid.lat(gy)),
            "lon": float(grid.lon(gx)),
        },
        "span": None if edges is None else {
            "start": str(edges[0].replace(day=1)),
            "end": str(_month_end(edges[1])),
        },
        "through": None if edges is None else str(edges[1]),
        "top": top,
        "months": months,
    }
