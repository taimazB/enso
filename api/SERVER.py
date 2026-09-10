"""FastAPI service for the CoralTemp Pacific SST dashboard.

Timeseries are read live from ClickHouse. **Imagery is not**: map frames are
rendered by `process` from the daily NetCDF and served from the cache here, so
`/image` 404s on a bucket it has neither cached nor a source file for. See
`modules/render.py` for why that is deliberate rather than a limitation.

Blocking work runs in the default thread pool via FastAPI's sync endpoints
rather than blocking the event loop.
"""

from __future__ import annotations

import datetime as dt
import logging
import time
from typing import Literal

from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from shared.domain import global_grid, quantities, regions, subset, variable, variables

from modules import render, state
from modules.clickhouse_helpers import client, reset
from modules.periods import Period
from modules.posthog_helpers import capture_event
from modules.timeseries import (
    VARIABLES,
    OutsideDomainError,
    coverage,
    monthly_ranking,
    named_region_timeseries,
    point_timeseries,
    region_monthly_ranking,
    region_timeseries,
)

# A Literal so FastAPI rejects an unknown name with a 422 before it reaches the
# query builder, and documents the choice in /docs.
Variable = Literal["sst", "anom", "mhw"]

LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"

logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
log = logging.getLogger("enso.api")


def _timestamp_uvicorn_logs() -> None:
    """Put a datetime on uvicorn's own lines too, not just this module's.

    Uvicorn installs its own handlers with a timestamp-less format, and its
    loggers do not propagate, so `basicConfig` above never reaches them --
    every request line and every startup/shutdown event lands undated. The
    formatters are uvicorn subclasses (a colourised level name, and the access
    line's own fields), so the existing format string is rebuilt on the same
    class rather than replaced with a plain `Formatter`.

    Safe to call at import time: uvicorn configures logging in `Config.__init__`,
    before it imports the app.
    """
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        for handler in logging.getLogger(name).handlers:
            fmt = handler.formatter
            if fmt is None or fmt._fmt is None:
                handler.setFormatter(logging.Formatter(LOG_FORMAT))
            elif "%(asctime)s" not in fmt._fmt:
                handler.setFormatter(type(fmt)("%(asctime)s " + fmt._fmt))


_timestamp_uvicorn_logs()

app = FastAPI(title="CoralTemp Pacific SST API", version="0.2.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def _stamp_request_context(request: Request, call_next):
    """Per-request state that `capture_event` reads if analytics is enabled.

    `start_time` becomes the event's `duration_ms`; `distinct_id` is the
    frontend's posthog-js identity, forwarded by the axios default header set in
    `front/app/plugins/posthog.client.ts`, so a visitor's UI events and the API
    calls they cause share one identity instead of being attributed by IP.

    Doing it here rather than at each call site means neither concern is
    repeated in seven endpoints, and it costs nothing when POSTHOG_API_KEY is
    unset — `capture_event` returns immediately and this state is simply unread.
    """
    request.state.start_time = time.perf_counter()
    request.state.distinct_id = request.headers.get("x-posthog-distinct-id", "").strip() or None
    return await call_next(request)


# --- Models -----------------------------------------------------------------


class PointRequest(BaseModel):
    lat: float = Field(..., ge=-90, le=90)
    lon: float = Field(..., ge=-360, le=360)
    start: dt.date | None = None
    end: dt.date | None = None
    period: Period = "daily"
    variable: Variable = "sst"


class RankingRequest(BaseModel):
    lat: float = Field(..., ge=-90, le=90)
    lon: float = Field(..., ge=-360, le=360)
    top: int = Field(10, ge=1, le=100)
    variable: Variable = "anom"


class BoxRequest(BaseModel):
    lat: tuple[float, float]
    lon: tuple[float, float]
    start: dt.date | None = None
    end: dt.date | None = None
    period: Period = "daily"
    variable: Variable = "sst"


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
            "resolution": global_grid().resolution,
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
                # Named one-click display ranges. The default is NOT among them:
                # it is `vmin`/`vmax` above, and the control builds its chip from
                # those, so the default has one definition. Empty for a
                # categorical variable, whose range is not the user's to move.
                "presets": [
                    {"label": p.label, "vmin": p.vmin, "vmax": p.vmax} for p in v.presets
                ],
                # `anom` is computed as sst - climatology, not stored.
                "derived": v.derived,
                # WHAT THIS VARIABLE IS MEASURED AGAINST, or null where that is
                # not a question (`sst` is an absolute temperature).
                #
                # Shipped per variable rather than as one project-wide constant
                # because the dashboard carries **two different baselines** and
                # they are not reconcilable: `anom` is a departure from a
                # 1991-2020 daily mean computed here, `mhw` a category NOAA
                # assigned against a 1985-2012 90th percentile with an 11-day
                # window. Nothing in either number says so, so the UI has to.
                #
                # `statistic` is what a client keys off to phrase it correctly:
                # a `mean` baseline makes a departure, a `p90` one an
                # exceedance, and calling the latter a departure is the specific
                # confusion this block exists to prevent.
                "baseline": (
                    {
                        "period": v.baseline.period,
                        "statistic": v.baseline.statistic,
                        "windowDays": v.baseline.window_days,
                        "label": v.baseline.label,
                        "computedBy": v.baseline.computed_by,
                        "note": v.baseline.note,
                        # The method's own authors, where the data provider did
                        # not invent it. Empty for anything computed here.
                        "references": [
                            {
                                "authors": r.authors,
                                "year": r.year,
                                "title": r.title,
                                "source": r.source,
                                "url": r.url,
                            }
                            for r in v.baseline.references
                        ],
                    }
                    if v.baseline is not None
                    else None
                ),
                # An ordinal class rather than a measurement. The frontend needs
                # this to draw a step ramp instead of an interpolation, to hide
                # the colour-range control (there is nothing between two classes
                # to re-range), and to stop printing degrees at it.
                "categorical": v.categorical,
                "categories": [
                    {"value": c.value, "color": c.color, "label": c.label}
                    for c in v.colors
                ],
                # How /image packs this variable's value into the WebP, ready to
                # hand to Mapbox: `mix` is `raster-color-mix` verbatim and
                # `range` is what the encoding can represent. Computed here, not
                # in the frontend, so the packing and the unpacking cannot
                # drift — the images outlive the NetCDF they were made from.
                "encoding": {
                    "mix": v.encoding.mix(),
                    "range": list(v.encoding.value_range()),
                    "scale": v.encoding.scale,
                    "sentinel": v.encoding.sentinel,
                    # `raster-color-range`: the span the 256-entry colour ramp
                    # is tabulated over. A variable whose codes must land
                    # one-per-entry tabulates its whole encoding range — `anom`
                    # so its sentinel gets a slot of its own, `mhw` so that code
                    # k is entry k. Everything else spends all 256 entries on the
                    # display range instead, which is where they are useful.
                    # `Variable.color_range()` owns that decision; the frontend
                    # mirrors it in `colorRangeFor`.
                    "colorRange": list(v.color_range()),
                    # How far the user may move the displayed range. The map is
                    # re-ranged client-side — the images carry data, not colour —
                    # and this bounds that control. Not the same as `range`:
                    # what the bytes can hold is not what is worth showing.
                    "limits": list(v.range_limits()),
                },
            }
            for name, v in variables().items()
        },
        # Series-only quantities: numbers a timeseries can report that are not
        # fields the map can draw, so they are NOT in `variables` above — the
        # frontend's variable toggle is built from that block, and an entry there
        # with no raster would appear as a fourth map layer that renders nothing.
        #
        # There is one, `mhw_extent`: what `mhw` means over a REGION, where the
        # value is the share of the box's ocean area in a heatwave rather than a
        # category. A timeseries response names its quantity in `quantity`, and
        # the client formats from this block when it is set.
        "quantities": {
            name: {
                "longName": q.long_name,
                "shortName": q.short_name,
                "units": q.units,
                "precision": q.precision,
                "vmin": q.vmin,
                "vmax": q.vmax,
                "colormap": q.colormap,
            }
            for name, q in quantities().items()
        },
        # Per variable: `sst`'s scale is sequential and `anom`'s diverging, so
        # there is no single legend that serves both. Quantities are in the same
        # mapping rather than a second one — a consumer looks up whichever key
        # the series named, and the two namespaces do not collide.
        "colorStops": {
            **{name: render.colormap_stops(name) for name in VARIABLES},
            **{name: render.quantity_stops(name) for name in quantities()},
        },
        "defaultVariable": "sst",
        # Ocean with SST but no climatology — the seasonal ice fringe, about
        # 3.2% of the box. Drawn flat so it reads as "no anomaly here" rather
        # than as land or as zero.
        "noClimColor": "#%02x%02x%02x" % render.NO_CLIM_RGBA[:3],
        # `lat`/`lon` are the region's BOUNDING BOX on every entry, masked or
        # not, because that is what the camera frames and what a caller needs to
        # place it. `masked` says the box is not the region: a polygon region's
        # numbers cover only the cells inside its outline, and the outline itself
        # is at `/region/{key}/geometry` rather than here — it is 120 KB for the
        # BC EEZ against ~2 KB for this whole payload, and most sessions never
        # select it.
        "regions": [
            {
                "key": r.key,
                "label": r.label,
                "lat": list(r.lat),
                "lon": list(r.lon),
                "partial": r.partial,
                "masked": r.masked,
            }
            for r in regions().values()
        ],
    }


@app.get("/coverage")
def coverage_endpoint() -> dict:
    """Ingested date range and row count."""
    return coverage()


@app.get("/state")
def state_endpoint() -> dict:
    """What the Pacific is doing today, in the two numbers that need no context.

    The header ribbon's whole payload: the ENSO phase from Nino 3.4 and the share
    of the basin in a marine heatwave against the date's normal. Every other
    endpoint answers a question the visitor has already framed; this one answers
    the question they arrive with.

    Both halves are rollup reads — `region_daily` for `nino34` and for the whole
    `pacific` box — so this is a few thousand rows and milliseconds, not a scan.
    Not instrumented: it is page-load plumbing, fired once per visit like
    `/domain` and `/coverage`, and a `state_viewed` event on every load would say
    nothing a page view does not already say. The ribbon's *clicks* are tracked,
    in the frontend, because those are choices.
    """
    return state.pacific_state()


@app.get("/variables")
def variables_endpoint() -> dict:
    return {
        "variables": [
            {
                "name": name,
                "longName": variable(name).long_name,
                "shortName": variable(name).short_name,
                "units": variable(name).units,
                "derived": variable(name).derived,
                "categorical": variable(name).categorical,
            }
            for name in VARIABLES
        ]
    }


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
def timeseries(request: PointRequest, http_request: Request):
    """Full anomaly record at the grid cell nearest a point.

    `period` averages the daily values into weekly (Monday-start) or monthly
    buckets, each labelled by its first day — the same bucketing the map imagery
    uses, so a chart point and the map frame for that date agree.
    """
    try:
        result = point_timeseries(
            request.lat,
            request.lon,
            request.start,
            request.end,
            request.period,
            request.variable,
        )
    except OutsideDomainError as exc:
        # Captured too: a point outside the box is a real thing people try, and
        # a heat map of *where* they try it is the argument for widening the
        # domain — which costs only a `domain.yml` edit, no re-ingest.
        capture_event(http_request, "point_queried", {
            "variable": request.variable,
            "period": request.period,
            "lat": request.lat,
            "lon": request.lon,
            "outside_domain": True,
        })
        return _outside_domain(exc)
    capture_event(http_request, "point_queried", {
        "variable": request.variable,
        "period": request.period,
        "lat": request.lat,
        "lon": request.lon,
        "buckets": len(result.get("dates", [])),
        "outside_domain": False,
    })
    return result


@app.post("/regionTimeseries")
def region_timeseries_endpoint(request: BoxRequest, http_request: Request) -> dict:
    """cos(lat)-weighted mean anomaly over an arbitrary box, per `period` bucket."""
    result = region_timeseries(
        request.lat,
        request.lon,
        request.start,
        request.end,
        period=request.period,
        variable_name=request.variable,
    )
    # An arbitrary box has no rollup and is the 3-12 s live aggregation, so this
    # is also the event that says whether anyone actually uses the endpoint the
    # frontend no longer exposes.
    capture_event(http_request, "region_box_queried", {
        "variable": request.variable,
        "period": request.period,
        "lat": list(request.lat),
        "lon": list(request.lon),
    })
    return result


@app.get("/region/{key}")
def named_region(
    key: str,
    http_request: Request,
    start: dt.date | None = None,
    end: dt.date | None = None,
    period: Period = "daily",
    variable: Variable = "sst",
) -> dict:
    """Mean over one of `domain.yml`'s named regions, per `period` bucket.

    For `anom` this uses the precomputed `region_clim` means rather than
    computing the box's climatology per request — the only thing in the whole
    pipeline that is materialised ahead of time.
    """
    if key not in regions():
        raise HTTPException(404, f"unknown region {key!r}; known: {sorted(regions())}")
    result = named_region_timeseries(key, start, end, period, variable)
    capture_event(http_request, "region_queried", {
        "region": key,
        "variable": variable,
        "period": period,
    })
    return result


@app.get("/region/{key}/geometry")
def named_region_geometry(key: str) -> dict:
    """The outline of a polygon region, as a GeoJSON Feature.

    Served separately from `/domain` rather than inlined in it: the BC EEZ ring
    is 120 KB against ~2 KB for the whole domain payload, and it is needed only
    once someone selects that region. `/domain`'s `masked` flag is what tells a
    client this endpoint has something to give.

    404 for a box region rather than a synthesised rectangle. A client that can
    draw a rectangle from `lat`/`lon` already has one, and returning a ring here
    would put a second definition of "the region's outline" in the codebase —
    the frontend's `densify()` bows the edges along their parallels, which a
    four-corner ring from these bounds would not.

    Longitudes are UNWRAPPED 0-360, like every other region bound the API
    reports: Mapbox places them correctly across the antimeridian and
    normalising into -180..180 is what collapses a crossing polygon.
    """
    region = regions().get(key)
    if region is None:
        raise HTTPException(404, f"unknown region {key!r}; known: {sorted(regions())}")
    if region.polygon is None:
        raise HTTPException(
            404,
            f"region {key!r} is a plain box; its outline is its lat/lon bounds",
        )
    return {
        "type": "Feature",
        "properties": {"key": region.key, "label": region.label},
        "geometry": {
            "type": "MultiPolygon",
            "coordinates": [[[list(p) for p in ring]] for ring in region.polygon],
        },
    }


@app.get("/region/{key}/monthlyRanking")
def named_region_ranking(
    key: str,
    http_request: Request,
    top: int = Query(10, ge=1, le=100),
    variable: Variable = "anom",
) -> dict:
    """A named region's years ranked, within each calendar month and over the year.

    `months` and `annual` both come back, off one scan -- the panel toggles
    between them without a second request.

    The cell endpoint's question asked of a box: the ranked value is the month's
    mean of the region's daily cos(lat)-weighted area means, so a year that ranks
    first here is the year whose month `/region/{key}` draws highest.

    Served from `region_daily`, which makes this ~15k rows — the same order as
    one cell's record. **Only named regions**: an arbitrary box through
    `/regionTimeseries` has no rollup and would be the 3-12 s live aggregation.

    `sd` is the spread of daily area means rather than of daily values, and is
    much narrower than the same column at a cell; the response carries
    `areaMean: true` so a client can say which it is showing.
    """
    if key not in regions():
        raise HTTPException(404, f"unknown region {key!r}; known: {sorted(regions())}")
    result = region_monthly_ranking(key, top, variable)
    capture_event(http_request, "region_ranking_queried", {
        "region": key,
        "variable": variable,
    })
    return result


@app.post("/monthlyRanking")
def monthly_ranking_endpoint(request: RankingRequest, http_request: Request):
    """One cell's years ranked, within each calendar month (`months`) and over
    the whole calendar year (`annual`).

    Never the caller's `period`: ranking weekly buckets against each other is a
    different question, and letting the period toggle change what this means
    would make it unreadable. The two groupings here are the two that can be
    asked, and they come off one scan.

    Every period is ranked, the archive's truncated edges included: the month or
    year in progress is the one most worth looking at, so it is shown with
    `partial: true` on its rows -- the frontend stars it and says how many days
    it stands on -- rather than left out.
    """
    try:
        result = monthly_ranking(
            request.lat, request.lon, request.top, request.variable
        )
    except OutsideDomainError as exc:
        return _outside_domain(exc)
    capture_event(http_request, "point_ranking_queried", {
        "variable": request.variable,
        "lat": request.lat,
        "lon": request.lon,
    })
    return result


# --- Map imagery ------------------------------------------------------------


@app.get("/image/{date}.webp")
def image(
    date: dt.date,
    width: int = Query(render.DEFAULT_WIDTH, ge=180, le=8192),
    nocache: bool = False,
    period: Period = "daily",
    variable: Variable = "sst",
) -> Response:
    """One bucket's field as a Web-Mercator WebP, for a Mapbox image source.

    `period` widens the frame from a single day to the mean over the week or
    month containing `date`; any date inside a bucket resolves to the same
    image. Served from the cache under `OISST_IMAGE_DIR`, keyed by
    (variable, period, bucket start, width).

    **This does not render from the database.** `process` produces every frame
    from the daily NetCDF, and `sst_daily` carries no `by_date` projection, so
    rebuilding an old bucket here would be a partition scan over billions of
    rows. A cache miss with no NetCDF left on disk is a 404 — deliberately, so
    a missing frame is a fast error rather than a hung request. Re-rendering
    history means re-downloading the range and running `CRW.cli render`.

    There is no tile pyramid; the Pacific box is one image, and a pyramid can be
    added behind the same URL shape later.
    """
    payload = render.render(
        date,
        width=width,
        use_cache=not nocache,
        period=period,
        variable_name=variable,
    )
    if payload is None:
        raise HTTPException(
            404,
            f"no cached {variable} image for {date} ({period}, w{width}) and no "
            "NetCDF on disk to render one from",
        )
    return Response(
        content=payload,
        media_type="image/webp",
        headers={"Cache-Control": "public, max-age=86400"},
    )
