"""FastAPI service for the ENSO / North Pacific SST-anomaly dashboard.

Reads live from ClickHouse; the NetCDF files are the ingest service's business,
not this one's. Blocking work (queries, image rendering) runs in the default
thread pool via FastAPI's sync endpoints rather than blocking the event loop.
"""

from __future__ import annotations

import datetime as dt
import logging

from fastapi import FastAPI, HTTPException, Query, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from shared.domain import regions, subset, variable, variables

from modules import render
from modules.clickhouse_helpers import client, reset
from modules.periods import Period
from modules.timeseries import (
    OutsideDomainError,
    coverage,
    monthly_ranking,
    named_region_timeseries,
    point_timeseries,
    region_timeseries,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
log = logging.getLogger("enso.api")

app = FastAPI(title="ENSO SST Anomaly API", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


# --- Models -----------------------------------------------------------------


class PointRequest(BaseModel):
    lat: float = Field(..., ge=-90, le=90)
    lon: float = Field(..., ge=-360, le=360)
    start: dt.date | None = None
    end: dt.date | None = None
    period: Period = "daily"


class RankingRequest(BaseModel):
    lat: float = Field(..., ge=-90, le=90)
    lon: float = Field(..., ge=-360, le=360)
    top: int = Field(10, ge=1, le=100)


class BoxRequest(BaseModel):
    lat: tuple[float, float]
    lon: tuple[float, float]
    start: dt.date | None = None
    end: dt.date | None = None
    period: Period = "daily"


# --- Metadata ---------------------------------------------------------------


@app.get("/health")
def health() -> dict:
    try:
        client().query("SELECT 1")
    except Exception as exc:  # noqa: BLE001 — health must report, not raise
        reset()
        return {"status": "degraded", "clickhouse": str(exc)}
    return {"status": "ok", "clickhouse": "ok"}


@app.get("/domain")
def domain() -> dict:
    """Grid extent, variable metadata, regions and colour stops for the client."""
    box = subset()
    return {
        "subset": {
            "name": box.name,
            "lat": [box.lat_min, box.lat_max],
            "lon": [box.lon_min, box.lon_max],
            "shape": [box.nlat, box.nlon],
            "resolution": 0.25,
        },
        "imageBounds": render.bounds(),
        "variables": {
            name: {
                "longName": v.long_name,
                "shortName": v.short_name,
                "units": v.units,
                "precision": v.precision,
                "vmin": v.vmin,
                "vmax": v.vmax,
                "colormap": v.colormap,
            }
            for name, v in variables().items()
        },
        "colorStops": render.colormap_stops(),
        "regions": [
            {
                "key": r.key,
                "label": r.label,
                "lat": list(r.lat),
                "lon": list(r.lon),
                "partial": r.partial,
            }
            for r in regions().values()
        ],
    }


@app.get("/coverage")
def coverage_endpoint() -> dict:
    """Ingested date range and row count."""
    return coverage()


@app.get("/variables")
def variables_endpoint() -> dict:
    v = variable("anom")
    return {"variables": [{"name": v.name, "longName": v.long_name, "units": v.units}]}


# --- Timeseries -------------------------------------------------------------


def _outside_domain(exc: OutsideDomainError) -> JSONResponse:
    """The 400 body for a point outside the ingested box.

    Carries both a plain-string `detail` and a structured `error`: callers that
    just print `detail` keep working, while `error.code` lets the UI show this as
    an informational empty state rather than a red failure.
    """
    box = subset()
    return JSONResponse(
        status_code=400,
        content={
            "detail": exc.message,
            "error": {
                "code": "outside_domain",
                "requested": {"lat": exc.lat, "lon": exc.lon},
                "domain": {
                    "lat": [box.lat_min, box.lat_max],
                    "lon": [box.lon_min, box.lon_max],
                },
            },
        },
    )


@app.post("/timeseries")
def timeseries(request: PointRequest):
    """Full anomaly record at the grid cell nearest a point.

    `period` averages the daily values into weekly (Monday-start) or monthly
    buckets, each labelled by its first day — the same bucketing the map imagery
    uses, so a chart point and the map frame for that date agree.
    """
    try:
        return point_timeseries(
            request.lat, request.lon, request.start, request.end, request.period
        )
    except OutsideDomainError as exc:
        return _outside_domain(exc)


@app.post("/regionTimeseries")
def region_timeseries_endpoint(request: BoxRequest) -> dict:
    """cos(lat)-weighted mean anomaly over an arbitrary box, per `period` bucket."""
    return region_timeseries(
        request.lat, request.lon, request.start, request.end, period=request.period
    )


@app.get("/region/{key}")
def named_region(
    key: str,
    start: dt.date | None = None,
    end: dt.date | None = None,
    period: Period = "daily",
) -> dict:
    """Mean anomaly over one of `domain.yml`'s named regions, per `period` bucket."""
    if key not in regions():
        raise HTTPException(404, f"unknown region {key!r}; known: {sorted(regions())}")
    return named_region_timeseries(key, start, end, period)


@app.post("/monthlyRanking")
def monthly_ranking_endpoint(request: RankingRequest):
    """Each calendar month's years at one cell, ranked warmest-first.

    Always monthly regardless of the caller's `period`: ranking weekly buckets
    against each other is a different question, and letting the period toggle
    change what this means would make it unreadable.

    Every month is ranked, the archive's truncated edge months included: the
    month in progress is the one most worth looking at, so it is shown with
    `partial: true` on its rows -- the frontend stars it and says how many days
    it stands on -- rather than left out.
    """
    try:
        return monthly_ranking(request.lat, request.lon, request.top)
    except OutsideDomainError as exc:
        return _outside_domain(exc)


# --- Map imagery ------------------------------------------------------------


@app.get("/image/{date}.webp")
def image(
    date: dt.date,
    width: int = Query(render.DEFAULT_WIDTH, ge=180, le=4320),
    nocache: bool = False,
    period: Period = "daily",
) -> Response:
    """One bucket's anomaly field as a Web-Mercator WebP, for a Mapbox image source.

    `period` widens the frame from a single day to the mean over the week or
    month containing `date`; any date inside a bucket renders (and caches) the
    same image. Rendered on demand from ClickHouse and cached under
    `OISST_IMAGE_DIR`. There is no tile pyramid yet — at 360x360 cells the whole
    domain is one modest image, and a pyramid can be added behind the same URL
    shape later.
    """
    payload = render.render(date, width=width, use_cache=not nocache, period=period)
    if payload is None:
        raise HTTPException(404, f"no data ingested for {date} ({period})")
    return Response(
        content=payload,
        media_type="image/webp",
        headers={"Cache-Control": "public, max-age=86400"},
    )
