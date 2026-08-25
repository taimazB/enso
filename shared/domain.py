"""Grid geometry and variable metadata loaded from ``domain.yml``.

The single source of truth for converting between latitude/longitude and the
integer cell indices (``gy``, ``gx``) stored in ClickHouse. Those indices are
into the *global* OISST grid, not the regional subset — see ``domain.yml``.
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
    """The full OISST 0.25-degree grid that cell indices are defined against."""

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
    """The box the NetCDF files on disk actually cover."""

    name: str
    lat_min: float
    lat_max: float
    lon_min: float
    lon_max: float
    nlat: int
    nlon: int

    @property
    def bounds(self) -> tuple[float, float, float, float]:
        """``(west, south, east, north)`` with longitudes in -180..180."""
        west = ((self.lon_min + 180.0) % 360.0) - 180.0
        east = ((self.lon_max + 180.0) % 360.0) - 180.0
        return (west, self.lat_min, east, self.lat_max)

    def contains(self, lat: float, lon: float) -> bool:
        lon360 = lon % 360.0
        return (
            self.lat_min - 0.125 <= lat <= self.lat_max + 0.125
            and self.lon_min - 0.125 <= lon360 <= self.lon_max + 0.125
        )


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
    colormap: str


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
    return {name: Variable(name=name, **cfg) for name, cfg in _raw()["variables"].items()}


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
