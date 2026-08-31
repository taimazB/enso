"""One bucket of one variable, built from whatever NetCDF is on disk.

**This is the single implementation, and that is the point.** It used to exist
twice — `process/CRW/imaging.py` for the bulk and daily renders, and
`api/modules/render.py` for the on-demand render of buckets still inside the
retention window — which is exactly the drift the retirement of `api/prerender.py`
was meant to end. Two copies of "what is a week" is one copy too many, and the
MHW variable made that concrete: it aggregates a bucket differently from the
other two, and a second copy would have quietly kept averaging it.

Nothing here touches ClickHouse. Images are built from files, never from the
database — see `shared/ch.py` for why the schema carries no `by_date` projection.

### How a bucket is reduced, per variable

`sst` and `anom` take the **mean** over the days present. `mhw` takes the
**maximum**, because a category is an ordinal class and its mean is not one: a
cell at Cat 1 for two days of seven averages to 0.29, which is not a category at
all and would draw as nothing. The max answers the question the frame is read
for — how bad did it get this week — and keeps every period on the same discrete
1..5 scale, so one legend serves all three.
"""

from __future__ import annotations

import datetime as dt
import logging
from pathlib import Path

import numpy as np

from . import fields
from .domain import variable
from .periods import Period, span

log = logging.getLogger(__name__)


def _day_field(
    day: dt.date, variable_name: str, nc_dir: Path | None, mhw_dir: Path | None
) -> tuple[np.ndarray, np.ndarray | None] | None:
    """One day's `(value, no_clim)` for a variable, or None if its file is absent.

    NaN in `value` means "no value here" in every case; what that means
    physically differs by variable, and `no_clim` is the only distinction that
    survives into the image (ocean with SST but no climatology, drawn grey). For
    `mhw` there is deliberately no such distinction — land, ice and heatwave-free
    ocean are all simply not a heatwave.
    """
    try:
        if variable_name == "mhw":
            return fields.as_category(fields.read_mhw_raw(day, mhw_dir)), None
        raw = fields.read_daily_raw(day, nc_dir)
        if variable_name == "sst":
            return fields.as_celsius(raw), None
        clim = fields.read_clim_raw(fields.mmdd_of(day))
        return fields.anomaly(raw, clim), fields.no_clim_mask(raw, clim)
    except (FileNotFoundError, OSError):
        return None


def bucket_field(
    date: dt.date,
    period: Period,
    variable_name: str,
    available: set[dt.date] | None = None,
    *,
    nc_dir: Path | None = None,
    mhw_dir: Path | None = None,
) -> tuple[np.ndarray, np.ndarray | None, int] | None:
    """Reduce a bucket to one field, from whatever NetCDF is still on disk.

    Returns `(field, no_clim_mask_or_None, n_days)`, or None if no day in the
    bucket had a file. `n_days` lets the caller see how much of the bucket the
    result actually rests on.

    Cells are reduced where present: a cell that is ocean on some days of the
    week and ice-masked on others still gets a result over the days it had.

    `available` is an optional set of dates known to be on disk, which lets a
    caller skip the open/fail cycle on a sparse archive. It is only ever a
    filter — a date in it whose file is missing is still handled.
    """
    first, last = span(date, period)
    reduce_max = variable(variable_name).categorical

    total: np.ndarray | None = None
    count: np.ndarray | None = None
    peak: np.ndarray | None = None
    missing_any: np.ndarray | None = None
    n_days = 0

    day = first
    while day <= last:
        if available is not None and day not in available:
            day += dt.timedelta(days=1)
            continue

        result = _day_field(day, variable_name, nc_dir, mhw_dir)
        if result is None:
            day += dt.timedelta(days=1)
            continue
        value, no_clim = result

        finite = np.isfinite(value)
        if count is None:
            count = np.zeros(value.shape, dtype="int32")
            missing_any = np.zeros(value.shape, dtype=bool)
            if reduce_max:
                peak = np.full(value.shape, -np.inf, dtype="float32")
            else:
                total = np.zeros(value.shape, dtype="float64")

        if reduce_max:
            # `np.maximum` would propagate the NaN; the whole point is that a day
            # with no heatwave at a cell must not erase another day's.
            np.maximum(peak, np.where(finite, value, -np.inf), out=peak)
        else:
            total[finite] += value[finite]
        count[finite] += 1
        if no_clim is not None:
            missing_any |= no_clim
        n_days += 1
        day += dt.timedelta(days=1)

    if count is None or n_days == 0:
        return None

    if reduce_max:
        field = np.where(count > 0, peak, np.nan).astype("float32")
    else:
        with np.errstate(invalid="ignore"):
            field = np.where(count > 0, total / np.maximum(count, 1), np.nan).astype("float32")

    # A cell with no anomaly on any contributing day stays "no climatology"; one
    # that had an anomaly on at least one day carries that day's value.
    no_clim = (missing_any & (count == 0)) if variable_name == "anom" else None
    return field, no_clim, n_days
