"""Render a day's anomaly field to a Web-Mercator PNG for the map.

The source grid is a regular 0.25-degree lat/lon field, which is linear in
longitude but *not* in Mercator y — so the rows are resampled onto an evenly
spaced Mercator axis here. Skipping that step and handing Mapbox the raw array
as an image source would stretch the field increasingly toward the pole, which
in a domain reaching 90N is a gross error, not a rounding one.

Web Mercator cannot represent latitudes beyond ~85.05N, so the top few degrees
of the domain are clipped. `bounds()` reports what actually made it in.
"""

from __future__ import annotations

import datetime as dt
import io
import logging
import os
from pathlib import Path

import matplotlib
import numpy as np
from PIL import Image
from shared.domain import global_grid, subset, variable

from .clickhouse_helpers import DATABASE, client
from .periods import Period, span, start_of

log = logging.getLogger(__name__)

IMAGE_DIR = Path(os.environ.get("OISST_IMAGE_DIR", "/opt/data/images"))

MERCATOR_LAT_LIMIT = 85.0511287798066
DEFAULT_WIDTH = 720


def _merc_y(lat_deg: np.ndarray | float) -> np.ndarray:
    lat = np.radians(np.clip(np.asarray(lat_deg, dtype="float64"), -MERCATOR_LAT_LIMIT, MERCATOR_LAT_LIMIT))
    return np.log(np.tan(np.pi / 4 + lat / 2))


def bounds() -> dict:
    """The rendered image's geographic extent, Mercator-clipped.

    Longitudes are returned in -180..180 for Mapbox. The domain's east edge
    (269.875E) becomes -90.125, so west > east never happens here; a subset that
    straddled the antimeridian differently would need a two-piece image source.
    """
    box = subset()
    west = ((box.lon_min - 0.125 + 180.0) % 360.0) - 180.0
    east = ((box.lon_max + 0.125 + 180.0) % 360.0) - 180.0
    south = max(box.lat_min - 0.125, -MERCATOR_LAT_LIMIT)
    north = min(box.lat_max + 0.125, MERCATOR_LAT_LIMIT)
    return {"west": west, "south": south, "east": east, "north": north}


def read_field(date: dt.date, period: Period = "daily") -> tuple[np.ndarray, int] | None:
    """The bucket's mean anomaly as a `(nlat, nlon)` float32 array, NaN over land.

    Served by `sst_anom`'s `by_date` projection: the table itself is ordered
    (gy, gx, date) for point timeseries, which would make a whole-day read a
    full partition scan without it. For `daily` the range is a single day and
    the per-cell `avg` collapses to the value itself.

    Also returns how many distinct days went into the mean, which is what tells
    `render()` whether the bucket is complete enough to be worth caching.
    """
    box = subset()
    grid = global_grid()
    gy0, gx0 = int(grid.gy(box.lat_min)), int(grid.gx(box.lon_min))
    first, last = span(date, period)

    rows = client().query_np(
        f"""
        SELECT gy, gx, avg(anom_raw) AS value, uniqExact(date) AS n_days
        FROM {DATABASE}.sst_anom
        WHERE date BETWEEN %(first)s AND %(last)s
        GROUP BY gy, gx
        """,
        parameters={"first": first, "last": last},
    )
    if rows is None or len(rows) == 0:
        return None

    var = variable("anom")
    field = np.full((box.nlat, box.nlon), np.nan, dtype="float32")
    iy = rows["gy"].astype("int32") - gy0
    ix = rows["gx"].astype("int32") - gx0
    inside = (iy >= 0) & (iy < box.nlat) & (ix >= 0) & (ix < box.nlon)
    field[iy[inside], ix[inside]] = (
        rows["value"][inside].astype("float32") * var.scale_factor + var.add_offset
    )
    return field, int(rows["n_days"].max())


def to_mercator(field: np.ndarray, width: int = DEFAULT_WIDTH) -> np.ndarray:
    """Resample a lat-linear field onto an evenly spaced Mercator y axis.

    Returns a north-up array; the input's row 0 is the *southern* edge, since
    OISST latitudes ascend.
    """
    box = subset()
    extent = bounds()

    y_top, y_bot = _merc_y(extent["north"]), _merc_y(extent["south"])
    x_span = np.radians(box.lon_max - box.lon_min + 0.25)
    height = max(1, int(round(width * (y_top - y_bot) / x_span)))

    # Pixel centres, north to south.
    y = y_top + (np.arange(height) + 0.5) / height * (y_bot - y_top)
    lat = np.degrees(2 * np.arctan(np.exp(y)) - np.pi / 2)

    # Fractional source row for each output row.
    src = (lat - box.lat_min) / 0.25
    i0 = np.clip(np.floor(src).astype("int32"), 0, box.nlat - 1)
    i1 = np.clip(i0 + 1, 0, box.nlat - 1)
    w = (src - i0).astype("float32")[:, None]

    a, b = field[i0], field[i1]
    out = a * (1 - w) + b * w
    # Linear blending propagates NaN from either neighbour, which would erode a
    # pixel of ocean along every coastline; fall back to whichever side is real.
    out = np.where(np.isnan(out), np.where(np.isnan(a), b, a), out)

    if width != box.nlon:
        # Longitude is linear in Mercator x, so this is a plain horizontal resize.
        src_x = (np.arange(width) + 0.5) / width * box.nlon - 0.5
        j0 = np.clip(np.floor(src_x).astype("int32"), 0, box.nlon - 1)
        j1 = np.clip(j0 + 1, 0, box.nlon - 1)
        wx = (src_x - j0).astype("float32")[None, :]
        a, b = out[:, j0], out[:, j1]
        blended = a * (1 - wx) + b * wx
        out = np.where(np.isnan(blended), np.where(np.isnan(a), b, a), blended)

    return out


def colorize(field: np.ndarray) -> Image.Image:
    """Map anomalies to RGBA using the variable's diverging colormap."""
    var = variable("anom")
    cmap = matplotlib.colormaps[var.colormap].with_extremes(bad=(0, 0, 0, 0))
    norm = matplotlib.colors.Normalize(vmin=var.vmin, vmax=var.vmax, clip=True)
    rgba = cmap(norm(np.ma.masked_invalid(field)), bytes=True)
    return Image.fromarray(rgba, mode="RGBA")


def cache_path(date: dt.date, width: int, period: Period = "daily") -> Path:
    """Cache file for a bucket. Keyed by the bucket's *first* day, so every date
    inside a week or month resolves to the same render."""
    bucket = start_of(date, period)
    return IMAGE_DIR / "anom" / period / f"{bucket:%Y}" / f"{bucket:%Y-%m-%d}_w{width}.png"


def render(
    date: dt.date,
    width: int = DEFAULT_WIDTH,
    use_cache: bool = True,
    period: Period = "daily",
) -> bytes | None:
    """PNG bytes for one bucket, rendered on demand and cached to disk.

    Returns None when nothing in the bucket has been ingested.
    """
    path = cache_path(date, width, period)
    if use_cache and path.is_file():
        return path.read_bytes()

    result = read_field(date, period)
    if result is None:
        return None
    field, n_days = result

    buffer = io.BytesIO()
    colorize(to_mercator(field, width)).save(buffer, format="PNG", optimize=True)
    payload = buffer.getvalue()

    # A week or month that is still filling up would otherwise be cached from a
    # partial mean and never re-rendered as the remaining days land.
    first, last = span(date, period)
    if n_days < (last - first).days + 1:
        log.info("not caching partial %s bucket %s (%d days)", period, first, n_days)
        return payload

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
    except OSError:
        # A read-only or missing image volume must not break the request.
        log.warning("could not cache %s", path, exc_info=True)

    return payload


def colormap_stops(n: int = 33) -> list[dict]:
    """Legend stops: evenly spaced values with their hex colours."""
    var = variable("anom")
    cmap = matplotlib.colormaps[var.colormap]
    values = np.linspace(var.vmin, var.vmax, n)
    norm = matplotlib.colors.Normalize(vmin=var.vmin, vmax=var.vmax)
    return [
        {"value": round(float(v), 3), "color": matplotlib.colors.to_hex(cmap(norm(v)))}
        for v in values
    ]
