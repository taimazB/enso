"""Point and area-mean SST-anomaly timeseries out of ClickHouse."""

from __future__ import annotations

import datetime as dt

from shared.domain import global_grid, regions, subset

from .clickhouse_helpers import DATABASE, client
from .periods import Period, bucket_sql


class OutsideDomainError(RuntimeError):
    """Raised when a requested point falls outside the ingested subset box."""

    def __init__(self, message: str, lat: float, lon: float):
        super().__init__(message, lat, lon)
        self.message = message
        self.lat = lat
        self.lon = lon


def _date_filter(start: dt.date | None, end: dt.date | None) -> tuple[str, dict]:
    clauses, params = [], {}
    if start is not None:
        clauses.append("date >= %(start)s")
        params["start"] = start
    if end is not None:
        clauses.append("date <= %(end)s")
        params["end"] = end
    return ("".join(f" AND {c}" for c in clauses), params)


def point_timeseries(
    lat: float,
    lon: float,
    start: dt.date | None = None,
    end: dt.date | None = None,
    period: Period = "daily",
) -> dict:
    """Every ingested day at the grid cell nearest `(lat, lon)`.

    Hits the `(gy, gx, date)` primary key head-on, so this reads only the ~16k
    rows for that one cell rather than scanning the table. `period` averages
    those rows into weekly or monthly buckets, labelled by their first day.
    """
    box = subset()
    if not box.contains(lat, lon):
        raise OutsideDomainError(
            f"({lat:.3f}, {lon:.3f}) is outside the ingested domain "
            f"({box.lat_min}..{box.lat_max}N, {box.lon_min}..{box.lon_max}E)",
            lat,
            lon,
        )

    grid = global_grid()
    gy, gx = int(grid.gy(lat)), int(grid.gx(lon))
    where, params = _date_filter(start, end)
    params |= {"gy": gy, "gx": gx}

    rows = client().query(
        f"""
        SELECT {bucket_sql(period)} AS bucket, avg(anom) AS value
        FROM {DATABASE}.sst_anom
        WHERE gy = %(gy)s AND gx = %(gx)s{where}
        GROUP BY bucket
        ORDER BY bucket
        """,
        parameters=params,
    ).result_rows

    return {
        "variable": "anom",
        "units": "degC",
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
    }


def region_timeseries(
    lat_bounds: tuple[float, float],
    lon_bounds: tuple[float, float],
    start: dt.date | None = None,
    end: dt.date | None = None,
    label: str | None = None,
    period: Period = "daily",
) -> dict:
    """Area-weighted mean anomaly over a lat/lon box, per `period` bucket.

    Cells are weighted by cos(latitude): at 60N a 0.25-degree cell covers half
    the area of one at the equator, so a plain average would over-weight the
    poleward end of any box tall enough to matter — which every box here is.
    """
    grid = global_grid()
    gy0, gy1 = sorted(int(grid.gy(v)) for v in lat_bounds)
    gx0, gx1 = sorted(int(grid.gx(v)) for v in lon_bounds)

    where, params = _date_filter(start, end)
    params |= {"gy0": gy0, "gy1": gy1, "gx0": gx0, "gx1": gx1}

    rows = client().query(
        f"""
        SELECT {bucket_sql(period)} AS bucket,
               sum(anom * cos(lat * pi() / 180)) / sum(cos(lat * pi() / 180)) AS mean_anom,
               uniqExact(date) AS n_days,
               count() / uniqExact(date) AS n_cells
        FROM {DATABASE}.sst_anom
        WHERE gy BETWEEN %(gy0)s AND %(gy1)s
          AND gx BETWEEN %(gx0)s AND %(gx1)s{where}
        GROUP BY bucket
        ORDER BY bucket
        """,
        parameters=params,
    ).result_rows

    return {
        "variable": "anom",
        "units": "degC",
        "period": period,
        "label": label,
        "bounds": {"lat": list(lat_bounds), "lon": list(lon_bounds)},
        "dates": [str(r[0]) for r in rows],
        "values": [None if r[1] is None else round(float(r[1]), 3) for r in rows],
        "n_days": [int(r[2]) for r in rows],
        "n_cells": [round(float(r[3])) for r in rows],
    }


def named_region_timeseries(
    key: str,
    start: dt.date | None = None,
    end: dt.date | None = None,
    period: Period = "daily",
) -> dict:
    region = regions()[key]
    result = region_timeseries(
        region.lat, region.lon, start, end, label=region.label, period=period
    )
    result["region"] = key
    result["partial"] = region.partial
    return result


def coverage() -> dict:
    """The ingested date range and row count."""
    row = client().query(
        f"SELECT count(), min(date), max(date) FROM {DATABASE}.sst_anom"
    ).result_rows[0]
    if not row[0]:
        return {"rows": 0, "start": None, "end": None, "days": 0}
    days = client().query(
        f"SELECT uniqExact(date) FROM {DATABASE}.ingest_status FINAL "
        "WHERE status = 'success_ingest'"
    ).result_rows[0][0]
    return {"rows": int(row[0]), "start": str(row[1]), "end": str(row[2]), "days": int(days)}


def _month_end(day: dt.date) -> dt.date:
    """Last day of `day`'s month -- day 32 always lands in the next one."""
    return (day.replace(day=1) + dt.timedelta(days=32)).replace(day=1) - dt.timedelta(days=1)


def _archive_edges() -> tuple[dt.date, dt.date] | None:
    """First and last day present in `sst_anom`, or None on an empty table."""
    lo, hi = client().query(
        f"SELECT min(date), max(date) FROM {DATABASE}.sst_anom"
    ).result_rows[0]
    return None if lo is None else (lo, hi)


def _partial_months(lo: dt.date, hi: dt.date) -> set[tuple[int, int]]:
    """The (year, month) pairs the archive covers only partly, at either edge.

    At most two: the month the record starts in and the month it ends in. Both
    are still *ranked* -- the month in progress is the one people most want to
    look at, and hiding it made the browser stop a month short of today for no
    gain -- but they are flagged so the chart can star them and say why.

    A month missing an *interior* day is **not** partial: 1986-03-18 is absent
    from OISST itself, so March 1986 has 30 days and is as complete as it will
    ever be. Only a truncated edge month is marked, because only that one is
    still going to change.
    """
    partial = set()
    if lo.day != 1:
        partial.add((lo.year, lo.month))
    if hi != _month_end(hi):
        partial.add((hi.year, hi.month))
    return partial


def monthly_ranking(lat: float, lon: float, top: int = 10) -> dict:
    """Every calendar month at the nearest cell, ranked within its month-of-year.

    One row per (month-of-year, year): the mean daily anomaly, the standard
    deviation of the daily values inside that month, and the year's rank among
    all years for that month, warmest first. The archive's edge months are
    ranked alongside the rest and flagged `partial`, so the month in progress is
    visible with the caveat attached rather than missing.

    `sd` is day-to-day spread at a single cell, so it is much wider than the same
    statistic on an area mean -- spatial averaging cancels daily noise that one
    cell keeps. That is signal about the cell, not error in the mean.

    Reads only the ~16k rows for one cell: `ORDER BY (gy, gx, date)` puts the
    whole 45-year record for a point in one contiguous run, so all twelve months
    come back from a single query in milliseconds and need no precomputation.
    """
    box = subset()
    if not box.contains(lat, lon):
        raise OutsideDomainError(
            f"({lat:.3f}, {lon:.3f}) is outside the ingested domain "
            f"({box.lat_min}..{box.lat_max}N, {box.lon_min}..{box.lon_max}E)",
            lat,
            lon,
        )

    grid = global_grid()
    gy, gx = int(grid.gy(lat)), int(grid.gx(lon))
    months: dict[str, list[dict]] = {str(m): [] for m in range(1, 13)}
    edges = _archive_edges()

    if edges is not None:
        lo, hi = edges
        partial = _partial_months(lo, hi)
        rows = client().query(
            f"""
            SELECT toMonth(date) AS month,
                   toYear(date)  AS year,
                   avg(anom)     AS mean_anom,
                   stddevSamp(anom) AS sd,
                   count()       AS n,
                   row_number() OVER (
                       PARTITION BY toMonth(date) ORDER BY avg(anom) DESC
                   ) AS rank
            FROM {DATABASE}.sst_anom
            WHERE gy = %(gy)s AND gx = %(gx)s
            GROUP BY month, year
            ORDER BY month, rank
            """,
            parameters={"gy": gy, "gx": gx},
        ).result_rows

        for month, year, mean_anom, sd, n, rank in rows:
            months[str(month)].append({
                "year": int(year),
                "mean": round(float(mean_anom), 3),
                "sd": None if sd is None else round(float(sd), 3),
                "n": int(n),
                "rank": int(rank),
                # Truncated by the edge of the archive, so its mean is over a
                # part-month and its rank will move as the rest lands.
                "partial": (int(year), int(month)) in partial,
            })

    return {
        "variable": "anom",
        "units": "degC",
        "requested": {"lat": lat, "lon": lon},
        "cell": {
            "gy": gy,
            "gx": gx,
            "lat": float(grid.lat(gy)),
            "lon": float(grid.lon(gx)),
        },
        # Every month the archive touches, edge months included; the two that
        # may be truncated carry `partial` on their rows.
        "span": None if edges is None else {
            "start": str(edges[0].replace(day=1)),
            "end": str(_month_end(edges[1])),
        },
        # The last day with data, so a caller can say how far the partial month
        # has actually got without re-reading /coverage.
        "through": None if edges is None else str(edges[1]),
        "top": top,
        "months": months,
    }
