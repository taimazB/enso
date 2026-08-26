"""Daily / weekly / monthly aggregation buckets.

One definition, used by both the timeseries queries and the map renderer, so a
chart point and the map frame carrying the same date always cover exactly the
same days. Weeks start on Monday; months are calendar months.

Buckets at the edges of the archive are simply short: the mean covers whichever
days are ingested, and the frontend mirrors this bucketing when it snaps the
selected date.
"""

from __future__ import annotations

import datetime as dt
from typing import Literal

Period = Literal["daily", "weekly", "monthly"]
PERIODS: tuple[Period, ...] = ("daily", "weekly", "monthly")

# ClickHouse expression mapping a row's `date` onto the first day of its bucket.
_BUCKET_SQL: dict[str, str] = {
    "daily": "date",
    "weekly": "toMonday(date)",
    "monthly": "toStartOfMonth(date)",
}


def bucket_sql(period: Period) -> str:
    """The GROUP BY expression for `period`, as a literal (never user text)."""
    try:
        return _BUCKET_SQL[period]
    except KeyError:
        raise ValueError(f"unknown period {period!r}; known: {list(PERIODS)}") from None


def start_of(date: dt.date, period: Period) -> dt.date:
    """First day of the bucket containing `date`."""
    if period == "daily":
        return date
    if period == "weekly":
        return date - dt.timedelta(days=date.weekday())
    if period == "monthly":
        return date.replace(day=1)
    raise ValueError(f"unknown period {period!r}; known: {list(PERIODS)}")


def span(date: dt.date, period: Period) -> tuple[dt.date, dt.date]:
    """Inclusive `(first, last)` day of the bucket containing `date`."""
    start = start_of(date, period)
    if period == "daily":
        return start, start
    if period == "weekly":
        return start, start + dt.timedelta(days=6)
    # Day 32 of any month lands in the next one; back up to its last day.
    return start, (start + dt.timedelta(days=32)).replace(day=1) - dt.timedelta(days=1)
