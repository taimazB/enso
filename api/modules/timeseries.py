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

`mhw` is stored, in its own table, but **sparsely**: only cells at category >= 1
have a row. So absence is ambiguous — it is heatwave-free ocean, ice, or land —
and every MHW query reads `mhw_daily` as the right side of a LEFT JOIN against
`sst_daily`, which holds exactly the ocean cells for a date. That join is what
supplies the zeros and excludes the land, and both tables are ordered
`(gy, gx, date)`, so at a point it is a primary-key read on both sides.

**How a bucket is reduced differs for `mhw`, and it has to.** A category is an
ordinal class, and its mean is not one: a cell at Cat 1 for two days of seven
averages to 0.29, which is no category at all. Weekly and monthly buckets take
the **max**, matching `shared/buckets.py` — so a chart point and the map frame
carrying the same date still agree, which is the invariant the whole period
mechanism exists to keep. The two places this deliberately does not apply are
noted where they occur: a region area-mean, which is no longer a category, and
the monthly rankings, which are asking a different question.

The rankings come in both shapes for the same reason the timeseries do — a cell
and a named region — and share one definition of the ranking itself
(`_ranked_months`) over two different daily series. A named region's is free:
`region_daily` already holds its ~15k daily area means, the same order of rows as
one cell's record. An arbitrary box has no rollup, so it has no ranking.
"""

from __future__ import annotations

import datetime as dt

from shared.domain import global_grid, regions, subset, variable

from .clickhouse_helpers import DATABASE, client
from .periods import Period, bucket_sql

# Queryable variables. `sst` is a stored ALIAS column, `anom` is derived, and
# `mhw` is stored in its own sparse table.
VARIABLES: tuple[str, ...] = ("sst", "anom", "mhw")

# The per-cell, per-date MHW category with its zeros restored.
#
# `sst_daily` is the authority on which cells are ocean on a given date, so it is
# the left side; `mhw_daily` only carries category >= 1. A LEFT JOIN miss yields
# ClickHouse's default for a UInt8 — 0 — which is exactly "ocean, no heatwave".
# `ifNull` covers the case where a caller has `join_use_nulls` on.
MHW_SOURCE = """
    SELECT d.date AS date, toUInt8(ifNull(m.cat, 0)) AS value
    FROM {db}.sst_daily AS d
    LEFT JOIN {db}.mhw_daily AS m
        ON m.gy = d.gy AND m.gx = d.gx AND m.date = d.date
    WHERE d.gy = %(gy)s AND d.gx = %(gx)s{where}
"""


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

    if name == "mhw":
        # `max`, not `avg`: see the module docstring. A daily bucket has one row
        # per date either way, so this is one code path across all three periods
        # and the chart cannot disagree with the map frame.
        where, params = _date_filter(start, end, alias="d")
        params |= {"gy": gy, "gx": gx}
        sql = f"""
            SELECT {bucket_sql(period)} AS bucket,
                   max(value) AS value,
                   count() AS n
            FROM ({MHW_SOURCE.format(db=DATABASE, where=where)})
            GROUP BY bucket ORDER BY bucket
        """
    elif name == "sst":
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
    # Rounded to the variable's own precision rather than a fixed 2: `mhw` is an
    # integer category and "3.0" invites the reader to look for a 3.4.
    places = variable(name).precision
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
        "values": [None if r[1] is None else round(float(r[1]), places) for r in rows],
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


# Which `region_daily` column serves which variable. The two SST columns are not
# interchangeable: `mean_sst` is the mean over every ocean cell in the box and
# `mean_sst_clim` the mean over its `has_clim = 1` cells only, which is what the
# `mean(sst - clim) == mean(sst) - mean(clim)` identity requires on the daily
# side. Serving `anom` from `mean_sst` would drift wherever the ice edge sits.
_ROLLUP_COLUMNS = {
    "sst": ("mean_sst", "n_cells"),
    "anom": ("mean_sst_clim", "n_cells_clim"),
    "mhw": ("mean_mhw", "n_cells"),
}


def _region_daily_rows(
    key: str, variable_name: str, start: dt.date | None, end: dt.date | None
) -> list[tuple]:
    """The named region's daily area means, read from the `region_daily` rollup.

    Returns exactly what the live aggregation in `region_timeseries()` returns —
    `(date, value, n_cells)` — so the bucketing below is shared and cannot drift
    between the two paths.

    Measured before this table existed, the live query it replaces ran 3.14 s for
    the smallest named region and 12.14 s for Nino 3.4. Only NAMED regions have a
    rollup; `/regionTimeseries` on an arbitrary box still aggregates live.

    `isFinite` drops dates where the region had no climatology cells at all, for
    which the rollup stores NaN rather than a 0 that would read as "exactly at
    climatology". No configured region hits this today.
    """
    value_col, count_col = _ROLLUP_COLUMNS[variable_name]
    where, params = _date_filter(start, end)
    return client().query(
        f"""
        SELECT date, {value_col}, {count_col}
        FROM {DATABASE}.region_daily FINAL
        WHERE region = %(key)s AND isFinite({value_col}){where}
        ORDER BY date
        """,
        parameters=params | {"key": key},
    ).result_rows


def _region_mhw_daily(
    gy0: int, gy1: int, gx0: int, gx1: int, where: str, params: dict
) -> list[tuple]:
    """Per-day cos(lat)-weighted mean MHW category over a box, and its cell count.

    **Deliberately not a join.** `mhw_daily` is sparse, so a LEFT JOIN over a box
    would put the whole box's ocean on the left — 14.55 B rows for the PDO domain
    — to add zeros that contribute nothing to a sum. Instead the two halves of
    the mean are computed separately and divided:

        numerator   sum(cat * cos(lat))  over mhw_daily   -- sparse, cheap
        denominator sum(cos(lat))        over sst_daily   -- the box's ocean

    The denominator has to come from `sst_daily` because it is the only table
    that knows which cells are ocean on a given date, and the ice edge moves. A
    day with no heatwave anywhere in the box has no numerator row at all, and is
    reported as a mean of 0 rather than dropped — "no heatwave" is a real answer,
    not a gap.
    """
    box = {"gy0": gy0, "gy1": gy1, "gx0": gx0, "gx1": gx1}
    weight = "cos(lat * pi() / 180)"

    numerator = {
        date: float(value)
        for date, value in client().query(
            f"""
            SELECT date, sum(cat * {weight}) AS weighted
            FROM {DATABASE}.mhw_daily
            WHERE gy BETWEEN %(gy0)s AND %(gy1)s
              AND gx BETWEEN %(gx0)s AND %(gx1)s{where}
            GROUP BY date
            """,
            parameters=params | box,
        ).result_rows
        if value is not None
    }

    return [
        (date, numerator.get(date, 0.0) / float(total), int(n_cells))
        for date, total, n_cells in client().query(
            f"""
            SELECT date, sum({weight}) AS total, count() AS n_cells
            FROM {DATABASE}.sst_daily
            WHERE gy BETWEEN %(gy0)s AND %(gy1)s
              AND gx BETWEEN %(gx0)s AND %(gx1)s{where}
            GROUP BY date ORDER BY date
            """,
            parameters=params | box,
        ).result_rows
        if total
    ]


def region_timeseries(
    lat_bounds: tuple[float, float],
    lon_bounds: tuple[float, float],
    start: dt.date | None = None,
    end: dt.date | None = None,
    label: str | None = None,
    period: Period = "daily",
    variable_name: str = "sst",
    clim_by_mmdd: dict[int, float] | None = None,
    daily_rows: list[tuple] | None = None,
) -> dict:
    """cos(lat)-weighted mean over a lat/lon box, per `period` bucket.

    Cells are weighted by cos(latitude): at 60N a 0.05-degree cell covers half
    the area of one at the equator, so a plain average would over-weight the
    poleward end of any box tall enough to matter.

    For `anom` the daily mean is restricted to `has_clim = 1` and the
    climatology mean for each contributing day is subtracted — see the module
    docstring for why this is not a join.

    **`mhw` buckets by mean, unlike a point.** Over a box the daily value is
    already an area mean — a continuous "average severity across the region" —
    and no longer a category, so there is nothing ordinal left for a max to
    preserve. Taking the max of daily area means would turn the series into a
    spike detector for the single worst day of each week.
    """
    name = check_variable(variable_name)
    grid = global_grid()
    gy0, gy1 = sorted(int(grid.gy(v)) for v in lat_bounds)
    gx0, gx1 = sorted(int(grid.gx(v)) for v in lon_bounds)

    where, params = _date_filter(start, end)
    params |= {"gy0": gy0, "gy1": gy1, "gx0": gx0, "gx1": gx1}
    clim_filter = " AND has_clim = 1" if name == "anom" else ""

    if daily_rows is not None:
        # A named region, served from the `region_daily` rollup. Everything below
        # — the climatology subtraction, the bucketing, the response shape — is
        # the same code the live path runs, which is the point of passing rows in
        # rather than giving named regions their own function.
        rows = daily_rows
    elif name == "mhw":
        rows = _region_mhw_daily(gy0, gy1, gx0, gx1, where, params)
    else:
        # For anomaly the per-day SST mean has to be formed before bucketing, so
        # the climatology can be subtracted day by day; weekly/monthly means are
        # then the mean of those daily anomalies. For SST the bucket mean can be
        # taken directly.
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
    """One of `domain.yml`'s named regions, served from the rollups.

    Both precomputed sides meet here: `region_daily` supplies the daily area
    means and `region_clim` the climatology to subtract from them. An arbitrary
    box through `/regionTimeseries` has neither and aggregates live, which is the
    only difference between the two endpoints.
    """
    region = regions()[key]
    name = check_variable(variable_name)
    clim = _region_clim_by_mmdd(key) if name == "anom" else None
    result = region_timeseries(
        region.lat,
        region.lon,
        start,
        end,
        label=region.label,
        period=period,
        variable_name=name,
        clim_by_mmdd=clim,
        daily_rows=_region_daily_rows(key, name, start, end),
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
        return {
            "rows": 0, "start": None, "end": None, "days": 0,
            "climatology": None, "mhw": None,
        }
    days = client().query(
        f"SELECT uniqExact(date) FROM {DATABASE}.ingest_status FINAL "
        "WHERE status = 'success_ingest'"
    ).result_rows[0][0]
    clim_keys = client().query(
        f"SELECT uniqExact(mmdd) FROM {DATABASE}.sst_clim"
    ).result_rows[0][0]
    mhw = client().query(
        f"SELECT count(), min(date), max(date) FROM {DATABASE}.mhw_daily"
    ).result_rows[0]
    mhw_days = client().query(
        f"SELECT uniqExact(date) FROM {DATABASE}.mhw_status FINAL "
        "WHERE status = 'success_ingest'"
    ).result_rows[0][0]
    return {
        "rows": int(row[0]),
        "start": str(row[1]),
        "end": str(row[2]),
        "days": int(days),
        # The MHW archive is ingested separately and lands about 90 minutes later,
        # so it gets its own range rather than being assumed to match.
        #
        # `complete` is the counterpart of `climatology.complete`, and it exists
        # for a sharper reason. `mhw_daily` is SPARSE — only category >= 1 has a
        # row — so every query restores the zeros with a LEFT JOIN against
        # `sst_daily`. That join cannot tell "this cell had no heatwave" from
        # "this date was never ingested": a half-backfilled archive reports a
        # confident **category 0** for every missing year, and a monthly ranking
        # then ranks 40 fabricated zeroes below one real month. There is no value
        # that could signal the difference, so the frontend must not offer the
        # variable until the archive covers the SST record. A day's tolerance,
        # because MHW is published about 90 minutes after CoralTemp and a run
        # landing between the two leaves a one-day gap that is not a hole.
        #
        # Guarded on the count, not on a null date: ClickHouse's min/max over an
        # empty Date column return the epoch, not NULL, so an un-ingested MHW
        # table would otherwise advertise coverage from 1970-01-01.
        "mhw": {
            "rows": int(mhw[0]),
            "days": int(mhw_days),
            "start": str(mhw[1]) if mhw[0] else None,
            "end": str(mhw[2]) if mhw[0] else None,
            "complete": bool(mhw[0]) and int(mhw_days) >= int(days) - 1,
        },
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


def _ranked_months(source: str, params: dict) -> dict:
    """Rank one daily series' calendar months against each other, year by year.

    `source` is any subquery yielding `(date, value)` — one cell's record, or a
    named region's daily area means. **The ranking itself is defined once, here**,
    so a region's ranks and a cell's cannot drift into meaning different things:
    same grouping, same `stddevSamp`, same `row_number()`, same partial-month
    flagging. Only the series underneath differs.

    Returns the `months` mapping plus the archive edges the caller reports.
    """
    months: dict[str, list[dict]] = {str(m): [] for m in range(1, 13)}
    edges = _archive_edges()
    if edges is None:
        return {"months": months, "edges": None}

    partial = _partial_months(*edges)
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
        parameters=params,
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

    return {"months": months, "edges": edges}


def _ranking_envelope(ranked: dict, top: int) -> dict:
    """The span/through/top fields both ranking endpoints report identically."""
    edges = ranked["edges"]
    return {
        "span": None if edges is None else {
            "start": str(edges[0].replace(day=1)),
            "end": str(_month_end(edges[1])),
        },
        "through": None if edges is None else str(edges[1]),
        "top": top,
        "months": ranked["months"],
    }


def _point_ranking_source(name: str) -> str:
    """The daily `(date, value)` series at one cell, for `_ranked_months`."""
    if name == "mhw":
        # **Mean, not max** — the one place `mhw` is deliberately averaged.
        # This ranks years against each other, and a max would put half the
        # archive on Cat 1 and rank nothing. The mean daily category over a
        # month is a severity-days index: it separates a month with one bad
        # week from one that spent all of it at Cat 1.
        return MHW_SOURCE.format(db=DATABASE, where="")
    if name == "sst":
        return f"""
            SELECT date, sst AS value FROM {DATABASE}.sst_daily
            WHERE gy = %(gy)s AND gx = %(gx)s
        """
    return f"""
        SELECT d.date AS date, d.sst - c.clim AS value
        FROM {DATABASE}.sst_daily AS d
        INNER JOIN {DATABASE}.sst_clim AS c
            ON c.gy = d.gy AND c.gx = d.gx
           AND c.mmdd = {MMDD_SQL.format(d="d.date")}
        WHERE d.gy = %(gy)s AND d.gx = %(gx)s
    """


def _region_ranking_source(name: str) -> str:
    """The daily `(date, value)` series for a named region, from the rollups.

    Same two tables `named_region_timeseries()` reads, and the same column choice
    (`_ROLLUP_COLUMNS`): `anom` comes off `mean_sst_clim`, restricted on the daily
    side to the `has_clim = 1` cells, because that is the precondition for
    `mean(sst - clim) == mean(sst) - mean(clim)`. Serving it from `mean_sst`
    would drift wherever the ice edge sits.

    So this reads ~15k rows for a region, the same order as one cell's record —
    which is the whole reason a named-region ranking costs nothing: the rollup
    already exists. **An arbitrary box has none** and would be the 3-12 s live
    aggregation, so `/regionTimeseries` gets no ranking.

    `isFinite` drops the dates where a region has no climatology cells at all,
    for which the rollup stores NaN rather than a 0 that would read as "exactly
    at climatology" — and which would otherwise drag that month's mean.
    """
    value_col, _ = _ROLLUP_COLUMNS[name]
    if name == "anom":
        # 366 rows on the right, one per MMDD. The commuting-means identity is
        # applied per day here rather than per month, so a month spanning the
        # ice edge's seasonal move is still subtracting the matching climatology.
        return f"""
            SELECT d.date AS date, d.{value_col} - c.mean_clim AS value
            FROM {DATABASE}.region_daily AS d FINAL
            INNER JOIN {DATABASE}.region_clim AS c FINAL
                ON c.region = d.region
               AND c.mmdd = {MMDD_SQL.format(d="d.date")}
            WHERE d.region = %(key)s AND isFinite(d.{value_col})
        """
    return f"""
        SELECT date, {value_col} AS value
        FROM {DATABASE}.region_daily FINAL
        WHERE region = %(key)s AND isFinite({value_col})
    """


def monthly_ranking(
    lat: float, lon: float, top: int = 10, variable_name: str = "anom"
) -> dict:
    """Every calendar month at the nearest cell, ranked within its month-of-year.

    One row per (month-of-year, year): the mean daily value, the standard
    deviation of the daily values inside that month, and the year's rank among
    all years for that month, warmest first.

    `sd` is day-to-day spread at a single cell, so it is much wider than the same
    statistic on `region_monthly_ranking()`'s area means — spatial averaging
    cancels daily noise that one cell keeps. That is signal about the cell, not
    error in the mean.

    Reads only the ~15k rows for one cell: `ORDER BY (gy, gx, date)` puts the
    whole record for a point in one contiguous run, so all twelve months come
    back from a single query and need no precomputation.
    """
    _require_inside(lat, lon)
    name = check_variable(variable_name)

    grid = global_grid()
    gy, gx = int(grid.gy(lat)), int(grid.gx(lon))
    ranked = _ranked_months(_point_ranking_source(name), {"gy": gy, "gx": gx})

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
        **_ranking_envelope(ranked, top),
    }


def region_monthly_ranking(
    key: str, top: int = 10, variable_name: str = "anom"
) -> dict:
    """The same ranking over one of `domain.yml`'s named regions.

    The ranked value is the month's mean of the region's **daily cos(lat)-weighted
    area means** — the same numbers `/region/{key}` plots, folded by month instead
    of by period bucket. So a year that ranks first here is the year whose month
    the chart draws highest, which is the invariant worth keeping between the two.

    Two things differ from a cell's ranking, and the response says so rather than
    leaving the caller to infer it:

    * **`sd` is the spread of daily area means**, not of daily values. Spatial
      averaging cancels most day-to-day noise, so it is much narrower than the
      same column at a point and is not comparable with it.
    * **`mhw` is a mean of area means**, which was never a category — a region's
      daily value is already continuous. At a point the mean is a deliberate
      departure from the max everything else takes; here there is nothing ordinal
      left to depart from.
    """
    region = regions()[key]
    name = check_variable(variable_name)
    ranked = _ranked_months(_region_ranking_source(name), {"key": key})

    return {
        "variable": name,
        "units": variable(name).units,
        "region": key,
        "label": region.label,
        "bounds": {"lat": list(region.lat), "lon": list(region.lon)},
        # The area mean cancels daily noise, so `sd` here answers a different
        # question than it does at a cell. Flagged rather than renamed: the
        # column is the same statistic over a different series.
        "areaMean": True,
        **_ranking_envelope(ranked, top),
    }
