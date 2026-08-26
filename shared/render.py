"""Render a field array to a Web-Mercator WebP for the map.

The source grid is a regular 0.05-degree lat/lon field, which is linear in
longitude but *not* in Mercator y — so the rows are resampled onto an evenly
spaced Mercator axis here. Skipping that and handing Mapbox the raw array as an
image source stretches the field increasingly toward the pole.

**This module takes arrays, never a database client.** Images are rendered from
the NetCDF while it is still on disk (see `shared/fields.py`), which is what let
the `by_date` projection — 66% of storage — be dropped from the schema. Nothing
here reads ClickHouse.
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

from .domain import subset, variable
from .periods import Period, start_of

log = logging.getLogger(__name__)

IMAGE_DIR = Path(os.environ.get("OISST_IMAGE_DIR", "/opt/data/images"))

MERCATOR_LAT_LIMIT = 85.0511287798066

# The Pacific box is 3800 source columns wide; 2048 keeps a whole bucket under
# ~200 KB while staying sharp at the zoom levels the map opens on.
DEFAULT_WIDTH = 2048

# Ocean that has SST but no climatology (the seasonal ice fringe). Drawn as a
# flat neutral grey on anomaly maps: transparent would read as land, and any
# colour on the diverging scale would read as a real anomaly near zero.
NO_CLIM_RGBA = (110, 110, 118, 255)


def _merc_y(lat_deg: np.ndarray | float) -> np.ndarray:
    lat = np.radians(np.clip(np.asarray(lat_deg, dtype="float64"), -MERCATOR_LAT_LIMIT, MERCATOR_LAT_LIMIT))
    return np.log(np.tan(np.pi / 4 + lat / 2))


def bounds() -> dict:
    """The rendered image's geographic extent, Mercator-clipped.

    Longitudes are **unwrapped** — the Pacific box's east edge is reported as
    290, not -70. Mapbox accepts that and places the quad correctly across the
    antimeridian; wrapping it would make west > east and collapse the source.
    """
    box = subset()
    half = 0.5 * (box.lon_max - box.lon_min) / max(box.nlon - 1, 1)
    return {
        "west": box.lon_min - half,
        "south": max(box.lat_min - half, -MERCATOR_LAT_LIMIT),
        "east": box.lon_max + half,
        "north": min(box.lat_max + half, MERCATOR_LAT_LIMIT),
    }


def to_mercator(field: np.ndarray, width: int = DEFAULT_WIDTH) -> np.ndarray:
    """Resample a lat-linear field onto an evenly spaced Mercator y axis.

    Returns a north-up array; the input's row 0 is the *southern* edge, as
    `shared.fields` guarantees.
    """
    box = subset()
    extent = bounds()
    res = (box.lat_max - box.lat_min) / max(box.nlat - 1, 1)

    y_top, y_bot = _merc_y(extent["north"]), _merc_y(extent["south"])
    x_span = np.radians(extent["east"] - extent["west"])
    height = max(1, int(round(width * (y_top - y_bot) / x_span)))

    # Pixel centres, north to south.
    y = y_top + (np.arange(height) + 0.5) / height * (y_bot - y_top)
    lat = np.degrees(2 * np.arctan(np.exp(y)) - np.pi / 2)

    src = (lat - box.lat_min) / res
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


def _nearest_mercator_mask(mask: np.ndarray, width: int) -> np.ndarray:
    """Resample a boolean mask onto the same Mercator axes, nearest-neighbour.

    Bilinear would produce fractional values along the ice edge with no sensible
    threshold; a mask is categorical, so it is sampled, not blended.
    """
    return to_mercator(mask.astype("float32"), width) > 0.5


def colorize(
    field: np.ndarray,
    variable_name: str = "sst",
    no_clim: np.ndarray | None = None,
) -> Image.Image:
    """Map values to RGBA using the variable's colormap.

    `sst` is sequential and `anom` diverging — see domain.yml for why that
    distinction is not cosmetic. `no_clim`, when given, paints ocean cells that
    have no climatology in a flat grey after colouring.
    """
    var = variable(variable_name)
    cmap = matplotlib.colormaps[var.colormap].with_extremes(bad=(0, 0, 0, 0))
    norm = matplotlib.colors.Normalize(vmin=var.vmin, vmax=var.vmax, clip=True)
    rgba = cmap(norm(np.ma.masked_invalid(field)), bytes=True)
    if no_clim is not None:
        rgba[no_clim] = NO_CLIM_RGBA
    return Image.fromarray(rgba, mode="RGBA")


def encode(
    field: np.ndarray,
    width: int = DEFAULT_WIDTH,
    variable_name: str = "sst",
    no_clim: np.ndarray | None = None,
) -> bytes:
    """WebP bytes for a field array on the subset grid."""
    merc = to_mercator(field, width)
    merc_no_clim = None if no_clim is None else _nearest_mercator_mask(no_clim, width)
    buffer = io.BytesIO()
    # Lossy q90, not lossless. Measured on OISST against a lossless encode of
    # the same field: mean error 0.015 degC, 99.6% of ocean pixels within
    # 0.1 degC. It buys ~5x on the wire and ~10x on encode.
    #
    # The alpha channel survives exactly — libwebp always codes alpha
    # losslessly — so the land mask is bit-identical and does not bleed.
    #
    # `method=4` because 6 costs many times the time for under 2% fewer bytes.
    # Exact values are the timeseries endpoints' job; this raster is to look at.
    colorize(merc, variable_name, merc_no_clim).save(
        buffer, format="WEBP", quality=90, method=4
    )
    return buffer.getvalue()


def cache_path(
    date: dt.date,
    width: int = DEFAULT_WIDTH,
    period: Period = "daily",
    variable_name: str = "sst",
    image_dir: Path | None = None,
) -> Path:
    """Cache file for a bucket, keyed by (variable, period, bucket start, width).

    Keyed by the bucket's *first* day, so every date inside a week or month
    resolves to the same render.
    """
    bucket = start_of(date, period)
    return (
        (image_dir or IMAGE_DIR)
        / variable_name
        / period
        / f"{bucket:%Y}"
        / f"{bucket:%Y-%m-%d}_w{width}.webp"
    )


def write_cache(path: Path, payload: bytes) -> None:
    """Write a rendered bucket to its cache file, tolerating a bad volume."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Via a temp file so a half-written image is never served: the API may
        # be answering requests out of this same directory.
        tmp = path.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_bytes(payload)
        tmp.replace(path)
    except OSError:
        log.warning("could not cache %s", path, exc_info=True)


def colormap_stops(variable_name: str = "sst", n: int = 33) -> list[dict]:
    """Legend stops: evenly spaced values with their hex colours."""
    var = variable(variable_name)
    cmap = matplotlib.colormaps[var.colormap]
    values = np.linspace(var.vmin, var.vmax, n)
    norm = matplotlib.colors.Normalize(vmin=var.vmin, vmax=var.vmax)
    return [
        {"value": round(float(v), 3), "color": matplotlib.colors.to_hex(cmap(norm(v)))}
        for v in values
    ]
