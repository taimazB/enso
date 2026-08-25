/**
 * Daily / weekly / monthly buckets, mirroring the API's `modules/periods.py`.
 *
 * Weeks start on Monday and months are calendar months, exactly as ClickHouse's
 * `toMonday` / `toStartOfMonth` do — the selected date is always snapped to a
 * bucket start here so the chart's x-value and the map's frame line up.
 */

export type Period = 'daily' | 'weekly' | 'monthly'

export const PERIODS: Array<{ label: string, value: Period }> = [
  { label: 'Daily', value: 'daily' },
  { label: 'Weekly', value: 'weekly' },
  { label: 'Monthly', value: 'monthly' },
]

/** Parsed as UTC: a bare `new Date('2024-05-01')` is UTC but `...T00:00:00` is local. */
function utc(iso: string): Date {
  return new Date(`${iso.slice(0, 10)}T00:00:00Z`)
}

function iso(date: Date): string {
  return date.toISOString().slice(0, 10)
}

/** First day of the bucket containing `date`. */
export function bucketStart(date: string, period: Period): string {
  const d = utc(date)
  if (period === 'weekly') {
    // getUTCDay() is 0 on Sunday; shift so Monday is 0.
    d.setUTCDate(d.getUTCDate() - ((d.getUTCDay() + 6) % 7))
  }
  else if (period === 'monthly') {
    d.setUTCDate(1)
  }
  return iso(d)
}

/** Inclusive last day of the bucket containing `date`. */
export function bucketEnd(date: string, period: Period): string {
  const d = utc(bucketStart(date, period))
  if (period === 'weekly') d.setUTCDate(d.getUTCDate() + 6)
  else if (period === 'monthly') d.setUTCMonth(d.getUTCMonth() + 1, 0)
  return iso(d)
}

/** Move `n` whole buckets from the one containing `date`. */
export function shiftBuckets(date: string, period: Period, n: number): string {
  const d = utc(bucketStart(date, period))
  if (period === 'daily') d.setUTCDate(d.getUTCDate() + n)
  else if (period === 'weekly') d.setUTCDate(d.getUTCDate() + n * 7)
  // Safe because monthly buckets always sit on day 1, so no month-end overflow.
  else d.setUTCMonth(d.getUTCMonth() + n)
  return iso(d)
}

/** How many buckets make up roughly a year — the double-arrow step. */
export function bucketsPerYear(period: Period): number {
  return period === 'daily' ? 365 : period === 'weekly' ? 52 : 12
}

/** Human label for the bucket containing `date`, e.g. `May 2024`. */
export function bucketLabel(date: string, period: Period): string {
  const start = utc(bucketStart(date, period))
  if (period === 'monthly') {
    return start.toLocaleDateString('en-GB', { month: 'long', year: 'numeric', timeZone: 'UTC' })
  }
  if (period === 'weekly') {
    const fmt = (d: string) =>
      utc(d).toLocaleDateString('en-GB', { day: 'numeric', month: 'short', timeZone: 'UTC' })
    return `${fmt(iso(start))} – ${fmt(bucketEnd(date, period))}`
  }
  return start.toLocaleDateString('en-GB', { dateStyle: 'medium', timeZone: 'UTC' })
}
