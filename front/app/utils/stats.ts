/**
 * Summary numbers for the panel beside the map, derived from a series.
 *
 * Pure functions of a `Series`, for the same reason `ranking.ts` is: they can be
 * asserted on directly, which is far cheaper than driving a browser to read a
 * number off a card.
 *
 * **Everything here comes out of the series the chart is already drawing.** That
 * is a deliberate limit on this pass, not an oversight. A point's series and a
 * region's series have the same shape, so one panel serves both scopes and there
 * is no second definition of "warmest on record" to drift. The numbers that
 * would need more — the absolute climatology behind an `sst` reading, the share
 * of a region's area at Cat >= 3 — are honestly absent rather than approximated
 * from what happens to be on hand; they need an endpoint, and inventing them
 * from the area mean would give a confident wrong answer.
 *
 * Two consequences of reading the *bucketed* series worth keeping in mind:
 *
 * - Ranks and records are over buckets at the current period, so "3rd warmest"
 *   means third warmest week when the app is weekly. That matches what the chart
 *   and the map are showing, which is the point.
 * - For `mhw` a weekly bucket is a **max**, not a mean (see `shared/buckets.py`),
 *   so `activeRecent` counts weeks that reached a category, not days.
 */
import type { Series } from '~/stores/main'

export interface Extremum { date: string, value: number }

export interface SeriesStats {
  /** Buckets carrying a value. Nulls are gaps and are excluded throughout. */
  n: number
  /** The bucket the map is on, if it is in the series. */
  current: Extremum | null
  /**
   * 1-based rank of `current` among all buckets, largest first — so rank 1 is
   * the warmest, or for `mhw` the most severe. Ties share the better rank.
   */
  rank: number | null
  max: Extremum | null
  min: Extremum | null
  mean: number | null
  /** Mean over the last 365 days of the series. */
  recentMean: number | null
  /** Least-squares slope over the whole series, in units per decade. */
  trendPerDecade: number | null
  /**
   * Buckets that reached at least category 1 in the last 365 days, out of the
   * buckets there. Only meaningful for `mhw`; callers gate on that.
   */
  activeRecent: { hits: number, of: number } | null
}

const EMPTY: SeriesStats = {
  n: 0,
  current: null,
  rank: null,
  max: null,
  min: null,
  mean: null,
  recentMean: null,
  trendPerDecade: null,
  activeRecent: null,
}

/** Days since epoch. Dates are ISO `YYYY-MM-DD` and UTC throughout the app. */
function dayNumber(iso: string): number {
  return Date.UTC(
    Number(iso.slice(0, 4)),
    Number(iso.slice(5, 7)) - 1,
    Number(iso.slice(8, 10)),
  ) / 86_400_000
}

export function summarise(series: Series | null, selectedDate?: string | null): SeriesStats {
  if (!series?.dates?.length) return EMPTY

  const points: Array<{ date: string, day: number, value: number }> = []
  for (let i = 0; i < series.dates.length; i++) {
    const value = series.values[i]
    const date = series.dates[i]
    if (date == null || value == null || !Number.isFinite(value)) continue
    points.push({ date, day: dayNumber(date), value })
  }
  if (!points.length) return EMPTY

  let max = points[0]!
  let min = points[0]!
  let sum = 0
  for (const p of points) {
    if (p.value > max.value) max = p
    if (p.value < min.value) min = p
    sum += p.value
  }

  const current = selectedDate
    ? points.find(p => p.date === selectedDate) ?? null
    : null

  // Ties share the better rank: two identical warmest weeks are both 1st, which
  // is what a reader means by "the warmest on record" when there are two.
  let rank: number | null = null
  if (current) {
    let above = 0
    for (const p of points) if (p.value > current.value) above++
    rank = above + 1
  }

  // 365 days back from the series' own last bucket, not from today — the archive
  // ends about a day behind and a fixed "now" would silently drop that bucket.
  const cutoff = points[points.length - 1]!.day - 365
  const recent = points.filter(p => p.day > cutoff)
  const recentMean = recent.length
    ? recent.reduce((a, p) => a + p.value, 0) / recent.length
    : null
  const activeRecent = recent.length
    ? { hits: recent.filter(p => p.value >= 1).length, of: recent.length }
    : null

  return {
    n: points.length,
    current: current && { date: current.date, value: current.value },
    rank,
    max: { date: max.date, value: max.value },
    min: { date: min.date, value: min.value },
    mean: sum / points.length,
    recentMean,
    trendPerDecade: trend(points),
    activeRecent,
  }
}

/**
 * Ordinary least-squares slope, scaled to units per decade.
 *
 * Regressed on the **date**, not on the bucket index, so it is unaffected by a
 * gap in the archive and reads the same at daily, weekly and monthly. Returns
 * null below two points, or where every point shares a date.
 */
function trend(points: Array<{ day: number, value: number }>): number | null {
  if (points.length < 2) return null
  const n = points.length
  let sx = 0
  let sy = 0
  for (const p of points) { sx += p.day; sy += p.value }
  const mx = sx / n
  const my = sy / n
  let num = 0
  let den = 0
  for (const p of points) {
    const dx = p.day - mx
    num += dx * (p.value - my)
    den += dx * dx
  }
  if (den === 0) return null
  return (num / den) * 3652.5
}

/** `1st`, `2nd`, `13th`. */
export function ordinal(n: number): string {
  const mod100 = n % 100
  if (mod100 >= 11 && mod100 <= 13) return `${n}th`
  return `${n}${({ 1: 'st', 2: 'nd', 3: 'rd' } as Record<number, string>)[n % 10] ?? 'th'}`
}

/**
 * Whether a categorical series carries whole classes or means of them.
 *
 * A point reads one cell, so its value IS a class — 3 is Severe. A region reads
 * a cos(lat)-weighted area mean over every ocean cell in the box, which is
 * continuous and is not a class. Nothing in the series says which scope produced
 * it, and nothing needs to: integrality is the honest test, and it stays correct
 * for a region that happens to sit at a flat 0.
 *
 * Callers use it to decide what to *name* a value and what to say about it —
 * `TimeseriesChart`'s axis, `StatsPanel`'s headline and its basis note.
 */
export function wholeClasses(series: Series | null): boolean {
  return (series?.values ?? []).every(v => v == null || Number.isInteger(v))
}
