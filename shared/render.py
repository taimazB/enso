"""Render a field array to a Web-Mercator WebP for the map.

The source grid is a regular 0.05-degree lat/lon field, which is linear in
longitude but *not* in Mercator y — so the rows are resampled onto an evenly
spaced Mercator axis here. Skipping that and handing Mapbox the raw array as an
image source stretches the field increasingly toward the pole.

**This module takes arrays, never a database client.** Images are rendered from
the NetCDF while it is still on disk (see `shared/fields.py`), which is what let
the `by_date` projection — 66% of storage — be dropped from the schema. Nothing
here reads ClickHouse.

**The images carry data, not colour.** `encode()` packs the value into the RGB
channels and the land mask into alpha; Mapbox applies the colour ramp itself
with `raster-color`. The reason is that the daily NetCDF is pruned to a
retention window, so once a bucket's file is gone the cached image is the only
surviving copy of that field — and a pre-coloured cache would have today's
colormap and today's vmin/vmax welded into it for good. Value-encoded, the
palette and the displayed range stay client-side settings.
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

    **Not on the serving path.** `encode()` ships data and the browser colours
    it; this exists so a field can be eyeballed or diffed against a reference
    without a browser, and to build the legend stops below.
    """
    var = variable(variable_name)
    cmap = matplotlib.colormaps[var.colormap].with_extremes(bad=(0, 0, 0, 0))
    norm = matplotlib.colors.Normalize(vmin=var.vmin, vmax=var.vmax, clip=True)
    rgba = cmap(norm(np.ma.masked_invalid(field)), bytes=True)
    if no_clim is not None:
        rgba[no_clim] = NO_CLIM_RGBA
    return Image.fromarray(rgba, mode="RGBA")


def _bleed(codes: np.ndarray, known: np.ndarray, passes: int = 8) -> np.ndarray:
    """Fill unknown cells (land) with nearby known values.

    Land cannot be left at 0. Mapbox filters the texture, so a coastal texel
    that blends an ocean value against a land 0 decodes to the bottom of the
    scale — a wrong-coloured fringe along every coastline. Land is cut by the
    **alpha** channel, which is exact; the value channels just need to carry
    something harmless underneath it.

    Bleeding operates on the integer code, never on the packed channels: with a
    two-channel value, averaging the low byte across a 255->0 wrap would land
    the result a full 256 counts away.
    """
    if known.all():
        return codes
    codes = np.where(known, codes, int(np.median(codes[known]))).astype("int64")
    for _ in range(passes):
        if known.all():
            break
        acc = np.zeros(codes.shape, "int64")
        cnt = np.zeros(codes.shape, "uint8")
        for dy, dx in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            acc += np.roll(np.where(known, codes, 0), (dy, dx), (0, 1))
            cnt += np.roll(known, (dy, dx), (0, 1))
        grow = (~known) & (cnt > 0)
        codes = np.where(grow, acc // np.maximum(cnt, 1), codes)
        known = known | grow
    return codes


def encode(
    field: np.ndarray,
    width: int = DEFAULT_WIDTH,
    variable_name: str = "sst",
    no_clim: np.ndarray | None = None,
) -> bytes:
    """Value-encoded WebP bytes for a field array on the subset grid.

    The image carries the **data**, not a picture of it: the value goes into the
    RGB channels per the variable's `encoding`, land goes into alpha, and Mapbox
    applies the colour ramp itself via `raster-color`. That is what keeps the
    palette and the displayed range client-side settings — which matters here
    because the NetCDF archive is pruned to a retention window, so a re-render
    of history is not available to fall back on. A pre-coloured cache would weld
    today's colour choices into the only surviving copy of the data.

    **Lossless, necessarily.** Lossy WebP is YUV 4:2:0 — it subsamples chroma
    and quantises, which is harmless for a picture and ruinous for packed data.
    Measured on this field at q90: mean error 0.074 degC but a maximum of
    1.613 degC, i.e. visible blotches. Lossless costs 2.3-2.7x the bytes and
    actually *decodes* slightly cheaper (no inverse DCT, no YUV conversion).
    """
    var = variable(variable_name)
    enc = var.encoding
    merc = to_mercator(field, width)
    has_value = np.isfinite(merc)

    # Ocean without a value on this variable — the ice fringe, which has SST but
    # no climatology. Opaque like any other ocean cell, but flagged so the ramp
    # can paint it a flat grey; transparent would read as land and a scale
    # colour would read as a real anomaly near zero.
    sentinel_cells = (
        np.zeros_like(has_value)
        if no_clim is None
        else _nearest_mercator_mask(no_clim, width)
    )
    ocean = has_value | sentinel_cells

    # CLAMP, never wrap. An out-of-range value that overflows the code would
    # reappear at the opposite end of the scale — a record-warm cell drawn as
    # the coldest colour on the map.
    codes = np.round((np.nan_to_num(merc, nan=0.0) - enc.offset) / enc.scale)
    codes = np.clip(codes, enc.low_code, enc.depth - 1).astype("int64")
    if enc.sentinel is not None:
        codes = np.where(sentinel_cells, enc.sentinel, codes)
    codes = _bleed(codes, ocean)

    rgba = np.zeros((*merc.shape, 4), dtype="uint8")
    index = {"R": 0, "G": 1, "B": 2}
    n = len(enc.channels)
    for i, channel in enumerate(enc.channels):
        shift = 8 * (n - 1 - i)
        rgba[..., index[channel.upper()]] = ((codes >> shift) & 0xFF).astype("uint8")
    if n == 1:
        # Mirror the single channel across RGB: libwebp's subtract-green
        # transform then codes two of the three planes as zero, for free.
        rgba[..., 1] = rgba[..., 2] = rgba[..., 0]
    rgba[..., 3] = np.where(ocean, 255, 0).astype("uint8")

    buffer = io.BytesIO()
    # `method=4`: 6 buys under 2% for ~2.5x the encode time.
    Image.fromarray(rgba, mode="RGBA").save(
        buffer, format="WEBP", lossless=True, method=4
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
