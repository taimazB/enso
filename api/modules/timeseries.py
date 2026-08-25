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
