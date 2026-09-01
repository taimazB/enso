"""Grid geometry and variable metadata loaded from ``domain.yml``.

The single source of truth for converting between latitude/longitude and the
integer cell indices (``gy``, ``gx``) stored in ClickHouse. Those indices are
into the *global* CoralTemp grid, not the Pacific subset — see ``domain.yml``,
which also documents the two orientation conventions this file assumes.
"""

from __future__ import annotations

import functools
import os
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import yaml

_DOMAIN_YML = Path(__file__).with_name("domain.yml")


@dataclass(frozen=True)
class GlobalGrid:
    """The full CoralTemp 0.05-degree grid cell indices are defined against."""

    resolution: float
    lat0: float
    lon0: float
    nlat: int
    nlon: int

    def gy(self, lat):
        """Latitude (degrees_north) -> global row index."""
        return np.rint((np.asarray(lat, dtype="float64") - self.lat0) / self.resolution).astype("int32")

    def gx(self, lon):
        """Longitude -> global column index. Accepts -180..180 or 0..360."""
        lon = np.mod(np.asarray(lon, dtype="float64") - self.lon0, 360.0)
        return np.rint(lon / self.resolution).astype("int32") % self.nlon

    def lat(self, gy):
        return self.lat0 + np.asarray(gy, dtype="float64") * self.resolution

    def lon(self, gx):
        return self.lon0 + np.asarray(gx, dtype="float64") * self.resolution


@dataclass(frozen=True)
class Subset:
    """The box that is actually ingested and rendered."""

    name: str
    lat_min: float
    lat_max: float
    lon_min: float
    lon_max: float
    nlat: int
    nlon: int

    def gy_range(self, grid: GlobalGrid) -> tuple[int, int]:
        """Inclusive ``(first, last)`` global row index covered by the box."""
        return int(grid.gy(self.lat_min)), int(grid.gy(self.lat_max))

    def gx_range(self, grid: GlobalGrid) -> tuple[int, int]:
        """Inclusive ``(first, last)`` global column index covered by the box.

        Contiguous, not wrapping — which is the entire reason `domain.yml` puts
        `lon0` on the 0-360 convention. On the source's native -180..180 grid a
        Pacific box straddles the array edge and this would have to return two
        ranges, and every `WHERE gx BETWEEN` in the codebase would have to know.
        """
        return int(grid.gx(self.lon_min)), int(grid.gx(self.lon_max))

    @property
    def bounds(self) -> tuple[float, float, float, float]:
        """``(west, south, east, north)`` for a Mapbox image source.

        Longitudes are returned **unwrapped** — a box reaching 290 is reported
        as 290, not -70. Mapbox accepts that and places the quad correctly
        across the antimeridian (verified in Chromium: `project([290,0])` and
        `project([-70,0])` return the same pixel). Wrapping the east edge into
        -180..180 would make west > east and collapse the image source.
        """
        return (self.lon_min, self.lat_min, self.lon_max, self.lat_max)

    def contains(self, lat: float, lon: float) -> bool:
        """Whether a point falls in the box. Accepts either lon convention."""
        half = 0.5 * (self.lon_max - self.lon_min) / max(self.nlon - 1, 1)
        if not (self.lat_min - half <= lat <= self.lat_max + half):
            return False
        # Bring the point onto the box's own 0-360 frame. A box crossing 360
        # (none today, but Box B ends at 289.975 and a wider one could) needs
        # the shifted comparison rather than a plain modulo.
        lon360 = (lon - self.lon_min) % 360.0 + self.lon_min
        return self.lon_min - half <= lon360 <= self.lon_max + half


@dataclass(frozen=True)
class Category:
    """One class of a categorical variable: its code, colour and name."""

    value: int
    color: str
    label: str


@dataclass(frozen=True)
class Preset:
    """A named display range for the colour-range control.

    A shortcut, not a mode: clicking one is exactly the range the user could
    have typed, so it goes through the same clamping and is remembered the same
    way. Narrowing to a band CLAMPS everything outside it to the end colours
    rather than hiding it — the band is emphasised, not isolated.

    The default range is deliberately NOT one of these. It is the variable's own
    `vmin`/`vmax`, and the control synthesises its chip from them, so there is
    one definition of it rather than two that can drift apart.
    """

    label: str
    vmin: float
    vmax: float


@dataclass(frozen=True)
class Variable:
    name: str
    long_name: str
    short_name: str
    units: str
    precision: int
    scale_factor: float
    add_offset: float
    fill_value: int
    vmin: float
    vmax: float
    encoding: Encoding
    # Exactly one of these two carries the palette. `colormap` names a
    # matplotlib colormap sampled server-side; `colors` lists the classes of a
    # categorical variable explicitly, because `mhw`'s five are NOAA's own and
    # are recognised on sight — sampling some sequential map at five points
    # would throw that away.
    colormap: str | None = None
    colors: tuple[Category, ...] = ()
    # `anom` is computed as `sst - climatology(mmdd)` rather than stored.
    derived: bool = False
    # An ordinal class rather than a measurement. Three things follow, and each
    # of them is silently wrong if this is not honoured:
    #
    #   * the Mercator resample must be nearest, not bilinear — blending a Cat 2
    #     against a Cat 4 invents a Cat 3 along every edge;
    #   * the colour ramp must be a step, not an interpolation;
    #   * the displayed range is not user-adjustable, and the ramp is tabulated
    #     over the whole encoding range so entry k is exactly code k.
    categorical: bool = False
    # How far the *user* may move the displayed colour range, as opposed to
    # `vmin`/`vmax`, which are only where it opens. The frontend re-ranges the
    # map client-side — the images carry data, not colour — and this is what
    # bounds that control. Optional: absent, it falls back to `encoding` limits.
    #
    # It exists because the two are not the same question. `sst` packs into two
    # bytes at 0.01 degC and can therefore represent up to 650 degC, which is
    # arithmetic, not oceanography — a slider bounded by it would spend 95% of
    # its travel above the boiling point. `anom` has no such gap, so it can and
    # does simply omit this.
    limits: tuple[float, float] | None = None
    # Named shortcuts offered by the colour-range control. Same family as
    # `limits` — about the control, not about the data — and optional for the
    # same reason: `mhw` is categorical, its range is not the user's to move,
    # and the control does not exist for it. Validated against `range_limits()`
    # by the loader; see `Preset`.
    presets: tuple[Preset, ...] = ()

    def range_limits(self) -> tuple[float, float]:
        """Bounds for a user-chosen display range, clipped to what is encodable."""
        low, high = self.encoding.value_range()
        # Never past the encoding: a range the image cannot represent would show
        # a span of colour that no pixel can ever land in.
        if self.limits is None:
            return low, high
        return max(self.limits[0], low), min(self.limits[1], high)

    def color_range(self) -> tuple[float, float]:
        """`raster-color-range`: the span the 256-entry ramp is tabulated over.

        A variable whose *codes* have to land one-per-ramp-entry must tabulate
        its whole encoding range. Two do, for different reasons: `anom`'s
        sentinel needs a slot of its own, and `mhw`'s categories need code k to
        be entry k — tabulate its five classes over 1..5 instead and code 2 lands
        at entry 63.75, where a Cat 2 can pick up Cat 1's colour.

        Everything else spends all 256 entries on the display range, which is
        where they are actually useful.
        """
        if self.encoding.sentinel is not None or self.categorical:
            return self.encoding.value_range()
        return self.vmin, self.vmax


@dataclass(frozen=True)
class Encoding:
    """How a variable's value is packed into the bytes of a rendered image.

    The images the map consumes carry **data, not colour** — Mapbox colours them
    with `raster-color`, which reads a scalar out of the RGB channels via
    `raster-color-mix` and looks it up in a ramp. So the packing here and the
    mix vector the frontend sends must agree exactly, and `mix()` is the one
    place that arithmetic is done: the API ships the result in `/domain` rather
    than the TypeScript re-deriving it.

    `channels` are listed **high byte first**, so ``[G, B]`` means
    ``value = (G * 256 + B) * scale + offset``.
    """

    channels: tuple[str, ...]
    scale: float
    offset: float
    # A reserved code, always 0, for a cell that is ocean but has no value on
    # this variable — the ice fringe with no climatology. Real data starts at 1.
    sentinel: int | None = None

    @property
    def depth(self) -> int:
        """Number of distinct codes, e.g. 256 for one channel, 65536 for two."""
        return 256 ** len(self.channels)

    @property
    def low_code(self) -> int:
        return 1 if self.sentinel is not None else 0

    def mix(self) -> list[float]:
        """`raster-color-mix`: ``[r, g, b, offset]``.

        Mapbox computes ``mix.r*src.r + mix.g*src.g + mix.b*src.b + mix.a`` with
        each channel arriving normalised to 0..1, so a channel holding byte
        ``n`` arrives as ``n/255`` and its weight carries the 255 back.
        """
        out = [0.0, 0.0, 0.0, self.offset]
        idx = {"R": 0, "G": 1, "B": 2}
        for i, channel in enumerate(self.channels):
            place = 256 ** (len(self.channels) - 1 - i)
            out[idx[channel.upper()]] = 255.0 * place * self.scale
        return out

    def value_range(self) -> tuple[float, float]:
        """The span this encoding can represent, sentinel code included."""
        return self.offset, self.offset + self.scale * (self.depth - 1)


@dataclass(frozen=True)
class Region:
    key: str
    label: str
    lat: tuple[float, float]
    lon: tuple[float, float]
    partial: bool = False


@functools.lru_cache(maxsize=1)
def _raw() -> dict:
    path = Path(os.environ.get("ENSO_DOMAIN_YML", _DOMAIN_YML))
    with path.open() as fh:
        return yaml.safe_load(fh)


@functools.lru_cache(maxsize=1)
def global_grid() -> GlobalGrid:
    return GlobalGrid(**_raw()["global"])


@functools.lru_cache(maxsize=1)
def subset() -> Subset:
    return Subset(**_raw()["subset"])


@functools.lru_cache(maxsize=1)
def variables() -> dict[str, Variable]:
    out = {}
    for name, cfg in _raw()["variables"].items():
        cfg = dict(cfg)
        enc = dict(cfg.pop("encoding"))
        enc["channels"] = tuple(enc["channels"])
        if cfg.get("limits") is not None:
            cfg["limits"] = tuple(cfg["limits"])
        cfg["colors"] = tuple(Category(**c) for c in cfg.get("colors", ()))
        cfg["presets"] = tuple(Preset(**p) for p in cfg.get("presets", ()))
        out[name] = Variable(name=name, encoding=Encoding(**enc), **cfg)
        _check_presets(out[name])
    return out


def _check_presets(v: Variable) -> None:
    """Reject a declared preset the control could not honour.

    Raised rather than clipped, and rather than asserted (`assert` vanishes
    under `-O`). The frontend's `setScale` clamps every range it is given, so a
    preset past `range_limits()` has no loud symptom at all — it just lands
    somewhere other than where its label says, which is a chip that lies.
    """
    lo, hi = v.range_limits()
    for p in v.presets:
        if not lo <= p.vmin < p.vmax <= hi:
            raise ValueError(
                f"{v.name}: preset {p.label!r} spans {p.vmin}..{p.vmax}, "
                f"outside the adjustable range {lo}..{hi}"
            )


def variable(name: str) -> Variable:
    try:
        return variables()[name]
    except KeyError:
        raise KeyError(f"unknown variable {name!r}; known: {sorted(variables())}") from None


@functools.lru_cache(maxsize=1)
def regions() -> dict[str, Region]:
    return {
        key: Region(
            key=key,
            label=cfg["label"],
            lat=tuple(cfg["lat"]),
            lon=tuple(cfg["lon"]),
            partial=cfg.get("partial", False),
        )
        for key, cfg in _raw()["regions"].items()
    }
