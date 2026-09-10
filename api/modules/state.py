"""The one-line answer: what the Pacific is doing today.

Everything else in this API answers a question the visitor has already framed —
this cell, that region, this date. This module answers the question they have
not framed yet, which is the one a first-time visitor actually arrives with:
*is anything happening out there right now?* It is what the header ribbon reads,
and it is deliberately the only endpoint that decides what is worth saying rather
than serving what was asked for.

Two findings, because the archive supports exactly two that need no context to
read:

* **ENSO state**, from the Nino 3.4 anomaly. See `enso_state` for why this is
  ONI-*style* and not the ONI.
* **Marine heatwave extent**, the share of the Pacific box in a heatwave today
  against what is normal for the date.

Both come off `region_daily`, so the whole endpoint is a few thousand rows and
runs in milliseconds — the `pacific` region exists in `domain.yml` precisely so
the basin-wide half is a rollup read and not a scan of 113.8 billion rows.
"""

from __future__ import annotations

import datetime as dt

from shared.domain import regions, variable

from .clickhouse_helpers import DATABASE, client
from .timeseries import _MHW_EXTENT_SCALE, MMDD_SQL

# The region whose anomaly defines the ENSO state. Nino 3.4 is the box the ONI is
# computed over and the one this repo is named for.
ENSO_REGION = "nino34"

# The whole ingested box. Its rollup is what makes the basin-wide extent a
# 15k-row read; see `domain.yml`'s `pacific` region.
BASIN_REGION = "pacific"

# NOAA's ENSO threshold, on the 3-month running mean of the Nino 3.4 anomaly.
THRESHOLD = 0.5

# How many consecutive overlapping seasons past the threshold make an *episode*
# rather than merely *conditions*. NOAA's definition, and the distinction is
# worth keeping: conditions are what is measured this season, an episode is a
# claim about a run of them.
EPISODE_SEASONS = 5

# |index| bands. NOAA publishes these for episodes; used here to put a word next
# to a number so the ribbon reads without a legend.
_STRENGTH = ((2.0, "very strong"), (1.5, "strong"), (1.0, "moderate"), (0.0, "weak"))

# Three-letter season names, indexed by the season's MIDDLE month minus one.
#
# A season spans the month before its middle and the month after, so index 0 is
# January's season, DJF. Getting this off by one is silent and plausible: every
# label is still a real season name, just the neighbouring one, and "AMJ" beside
# an index built from May, June and July reads perfectly well.
_SEASONS = (
    "DJF", "JFM", "FMA", "MAM", "AMJ", "MJJ",
    "JJA", "JAS", "ASO", "SON", "OND", "NDJ",
)

# Half-width, in days, of the window a day-of-year "normal" is averaged over.
#
# A single MMDD over 1991-2020 is a mean of 30 numbers and jitters by a percent
# or two between neighbouring days, which would make the ribbon's comparison move
# for reasons that are not weather. +/-7 days is 450 values and is smooth without
# blurring the seasonal cycle, which at this latitude range turns over months
# rather than weeks.
NORMAL_WINDOW_DAYS = 7

# The climatological baseline, matching `sst_clim` and every anomaly this API
# serves. Reported in the payload so the ribbon can name it rather than assume.
#
# Read off `anom`'s own declaration rather than restated, because the dashboard
# has TWO baselines — `mhw`'s is NOAA's 1985-2012 90th percentile — and a
# hard-coded string here is how the wrong one gets printed beside the right
# number. `domain.yml` is the single definition; see `shared.domain.Baseline`.
BASELINE = variable("anom").baseline.period


def _monthly_nino34() -> list[tuple[dt.date, float, int]]:
    """(month, mean anomaly, days) for Nino 3.4, over the whole archive.

    Straight off the two rollups, the same pair `_region_ranking_source` reads:
    `region_daily.mean_sst_clim` minus `region_clim.mean_clim` for the date's
    MMDD. Per day and not per month, because the identity
    `mean(sst - clim) == mean(sst) - mean(clim)` holds over cells, not over
    calendar spans — a month straddling the ice edge's seasonal move must still
    subtract the climatology that matches each day.
    """
    return [
        (month, float(value), int(n))
        for month, value, n in client().query(
            f"""
            SELECT toStartOfMonth(d.date) AS month,
                   avg(d.mean_sst_clim - c.mean_clim) AS anom,
                   count() AS n
            FROM {DATABASE}.region_daily AS d FINAL
            INNER JOIN {DATABASE}.region_clim AS c FINAL
                ON c.region = d.region
               AND c.mmdd = {MMDD_SQL.format(d="d.date")}
            WHERE d.region = %(key)s AND isFinite(d.mean_sst_clim)
            GROUP BY month ORDER BY month
            """,
            parameters={"key": ENSO_REGION},
        ).result_rows
    ]


def _days_in_month(month: dt.date) -> int:
    return ((month.replace(day=28) + dt.timedelta(days=4)).replace(day=1) - month).days


def _strength(index: float) -> str:
    magnitude = abs(index)
    for floor, word in _STRENGTH:
        if magnitude >= floor:
            return word
    return "weak"


def _run_length(seasons: list[float], sign: int) -> int:
    """How many seasons, counting back from the newest, stay past the threshold.

    Same sign throughout: a run that flips from El Nino to La Nina is two runs,
    which is what makes this the episode test rather than a count of anything
    unusual.
    """
    n = 0
    for value in reversed(seasons):
        if value * sign < THRESHOLD:
            break
        n += 1
    return n


def enso_state() -> dict | None:
    """The ENSO phase, ONI-style, from the Nino 3.4 anomaly.

    **This is not the ONI, and the payload says so.** NOAA's Oceanic Nino Index
    is the 3-month running mean of the Nino 3.4 SST anomaly against a *shifting*
    30-year base period, updated every five years so a warming trend does not
    accumulate into the index. This archive carries one fixed 1991-2020
    climatology, so the index here runs warm relative to the official one by
    however much the tropical Pacific has warmed since that baseline's centre,
    and the two will not agree to the tenth. Everything else is NOAA's: the
    three-month overlapping seasons, the +/-0.5 degC threshold, the five
    consecutive seasons that separate an *episode* from *conditions*, and the
    strength bands.

    The distinction is reported rather than smoothed over -- `official: False`
    and `baseline` are in the payload -- because a dashboard that quietly
    published a number under the ONI's name and thresholds would be wrong in the
    one way nobody would check.

    Seasons are built from **complete calendar months only**. The month in
    progress is a mean over however many days have landed and would drag a
    running mean that is supposed to be three whole months; it is reported
    separately as `latestMonth`, flagged partial, since it is also the most
    interesting number on the page.
    """
    monthly = _monthly_nino34()
    if len(monthly) < 3:
        return None

    latest_month, latest_value, latest_days = monthly[-1]
    partial = latest_days < _days_in_month(latest_month)
    complete = monthly[:-1] if partial else monthly

    if len(complete) < 3:
        return None

    # Overlapping 3-month means, each labelled by its middle month.
    seasons = [
        (complete[i + 1][0], (complete[i][1] + complete[i + 1][1] + complete[i + 2][1]) / 3)
        for i in range(len(complete) - 2)
    ]
    middle, index = seasons[-1]
    values = [v for _, v in seasons]

    if index >= THRESHOLD:
        phase, sign = "el_nino", 1
    elif index <= -THRESHOLD:
        phase, sign = "la_nina", -1
    else:
        phase, sign = "neutral", 0

    run = _run_length(values, sign) if sign else 0

    # Where the month in progress sits among every other instance of that
    # calendar month. Folded from `monthly` in Python rather than asked of
    # `_ranked_months`, because it is one number out of a list already in hand
    # and the ranking endpoint would re-read the rollup to produce forty-two.
    same_month = [v for m, v, _ in monthly if m.month == latest_month.month]
    warmer = sum(1 for v in same_month if v > latest_value)

    return {
        "region": ENSO_REGION,
        "label": regions()[ENSO_REGION].label,
        "phase": phase,
        # Only meaningful once there is a phase; a "weak neutral" is not a thing.
        "strength": _strength(index) if sign else None,
        "index": round(index, 2),
        "season": f"{_SEASONS[middle.month - 1]} {middle.year}",
        "seasonEnd": str(middle),
        # Consecutive overlapping seasons at or past the threshold, same sign.
        "seasons": run,
        # NOAA's five-season rule: below it these are *conditions*, not an
        # episode. The ribbon's wording turns on this.
        "episode": run >= EPISODE_SEASONS,
        "latestMonth": {
            "month": str(latest_month),
            "value": round(latest_value, 2),
            "days": latest_days,
            "partial": partial,
            # Among the same calendar month in every other year. This is the
            # line that turns a number into a finding: +2.74 means little, and
            # "the warmest August in 42 years" means a great deal.
            "rank": warmer + 1,
            "of": len(same_month),
        },
        "threshold": THRESHOLD,
        "baseline": BASELINE,
        # Not NOAA's index. See the docstring.
        "official": False,
    }


def _basin_extent_rows() -> list[tuple[dt.date, float]]:
    """(date, extent %) for the whole box, straight from the `pacific` rollup."""
    return [
        (date, float(value))
        for date, value in client().query(
            f"""
            SELECT date, mhw_area_frac * {_MHW_EXTENT_SCALE}
            FROM {DATABASE}.region_daily FINAL
            WHERE region = %(key)s AND isFinite(mhw_area_frac)
            ORDER BY date
            """,
            parameters={"key": BASIN_REGION},
        ).result_rows
    ]


def heatwave_state() -> dict | None:
    """How much of the Pacific is in a marine heatwave, against the date's normal.

    The comparison is the finding, not the number: 47% means nothing on its own,
    and means a great deal beside a late-August normal of 15%. Both come out of
    the same ~15k rows, folded in Python rather than in three separate queries —
    the whole series is smaller than one day of `mhw_daily`.

    `normal` is the 1991-2020 mean over a +/-`NORMAL_WINDOW_DAYS` window around
    the date's day-of-year rather than that day alone; see that constant. It is
    None if the baseline years are not in the archive, in which case the ribbon
    reports the extent without a comparison rather than inventing one.
    """
    rows = _basin_extent_rows()
    if not rows:
        return None

    date, extent = rows[-1]
    doy = date.timetuple().tm_yday

    def in_window(day: dt.date) -> bool:
        gap = abs(day.timetuple().tm_yday - doy)
        # Wrap the year, so a normal for early January is not built out of
        # January alone.
        return min(gap, 366 - gap) <= NORMAL_WINDOW_DAYS

    baseline = [v for d, v in rows if 1991 <= d.year <= 2020 and in_window(d)]
    normal = sum(baseline) / len(baseline) if baseline else None

    # Rank among every day in the archive, warmest first. `rows` is ~15k floats,
    # so this is a comparison rather than a query.
    hotter = sum(1 for _, v in rows if v > extent)

    return {
        "region": BASIN_REGION,
        "label": regions()[BASIN_REGION].label,
        "date": str(date),
        "extent": round(extent, 1),
        "normal": None if normal is None else round(normal, 1),
        # How many times the normal, for the ribbon's plainest possible phrasing.
        # Guarded against a zero normal, which no day of this archive has but
        # which a narrower future box could.
        "ratio": None if not normal else round(extent / normal, 1),
        "rank": hotter + 1,
        "of": len(rows),
        "baseline": BASELINE,
        "windowDays": NORMAL_WINDOW_DAYS,
    }


def pacific_state() -> dict:
    """Both findings, plus the date they are as of.

    Either half may be None — an archive without the `pacific` rollup, or with
    fewer than three months of it — and the ribbon renders whichever it gets.
    """
    return {"enso": enso_state(), "heatwave": heatwave_state()}
