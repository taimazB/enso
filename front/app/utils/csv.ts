/**
 * CSV export of whatever is currently plotted.
 *
 * Pure text-building here and a single DOM-touching `downloadCsv()`, for the
 * same reason `ranking.ts` and `stats.ts` are pure: the interesting part is what
 * ends up in the file, and that can be asserted on without a browser.
 *
 * **The export is of the series the chart drew, not of a fresh request.** The
 * chart's values are already bucketed by the API at the current period, and a
 * second path to the same numbers is a second definition of "the weekly mean" —
 * the drift `shared/buckets.py` exists to prevent. So what a user downloads is
 * exactly the line they are looking at, gaps included.
 */
import type { Series, VariableName } from '~/stores/main'
import type { MonthlyRanking } from '~/utils/ranking'
import { MONTHS } from '~/utils/ranking'
import type { Period } from '~/utils/periods'
import { bucketEnd } from '~/utils/periods'

type Cell = string | number | null | undefined

/**
 * One CSV field. Quoted only when it has to be — the columns here are dates,
 * numbers and short labels, and quoting all of them makes the file harder to
 * read for no gain. A null is an EMPTY field rather than a `NaN` or a `0`: the
 * series' nulls are buckets with no data, and any placeholder value would be
 * read back as a reading.
 */
function field(value: Cell): string {
  if (value == null) return ''
  const text = String(value)
  return /[",\n]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text
}

/** Rows to CSV text. CRLF, which is what RFC 4180 says and what Excel wants. */
export function csvText(rows: Cell[][]): string {
  return rows.map(row => row.map(field).join(',')).join('\r\n') + '\r\n'
}

/**
 * A value at the variable's own precision.
 *
 * Written through `toFixed` rather than raw, because the API's floats arrive as
 * `0.20200000000000001` and a spreadsheet shows that verbatim. Trailing zeros
 * are kept — a fixed number of decimals is what makes a column of numbers
 * readable, and it is the same precision the panel prints.
 */
function number(value: number | null | undefined, precision: number): string {
  return value == null ? '' : value.toFixed(precision)
}

/** Filesystem-safe fragment for a filename: lowercase, punctuation to hyphens. */
export function slug(text: string): string {
  return text
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '')
    || 'export'
}

/**
 * A cell as a filename fragment — `48-00n_128-00w`, the same hemisphere reading
 * `index.vue` prints. Signed degrees would put a `-` in a name that already uses
 * one as its separator, and the 0-360 longitudes the API returns would give
 * `232.00` for a place everyone calls 128 W.
 */
export function cellSlug(cell: { lat: number, lon: number }): string {
  const lon = ((cell.lon + 180) % 360) - 180
  const ns = `${Math.abs(cell.lat).toFixed(2)}${cell.lat < 0 ? 's' : 'n'}`
  const ew = `${Math.abs(lon).toFixed(2)}${lon < 0 ? 'w' : 'e'}`
  return `${ns}_${ew}`.replace(/\./g, '-')
}

export interface SeriesCsvOptions {
  variable: VariableName
  period: Period
  /** Column header for the value, e.g. `anom_degC`. */
  unit?: string
  precision?: number
}

/**
 * The chart's series as CSV.
 *
 * `start_date` / `end_date` rather than a bare `date`, and both are written for
 * every period. A weekly row labelled `2024-05-06` is a mean over seven days,
 * and a file that says only `date` invites reading it as that Monday's reading —
 * the one confusion the whole period mechanism is built to avoid. On a daily
 * series the two columns are equal, which is honest rather than redundant.
 */
export function seriesCsv(series: Series, opts: SeriesCsvOptions): string {
  const period = series.period ?? opts.period
  const variable = series.variable ?? opts.variable
  const precision = opts.precision ?? 2
  const column = opts.unit === '°C' ? `${variable}_degC` : variable
  const rows: Cell[][] = [['start_date', 'end_date', column]]
  for (const [i, date] of series.dates.entries()) {
    rows.push([date, bucketEnd(date, period), number(series.values[i], precision)])
  }
  return csvText(rows)
}

/**
 * Every month's ranking as one CSV, not just the month the rail has open.
 *
 * The browser shows one month at a time because 45 rows twelve times over is not
 * readable on screen — but a file has no such limit, and a `month` column is
 * exactly what makes the whole table filterable in the tool the user is
 * exporting to. `partial` is carried through as a boolean: it is the difference
 * between a settled rank and one that will move, and dropping it would export
 * the archive's edge month as an ordinary datum.
 */
export function rankingCsv(ranking: MonthlyRanking, precision = 2): string {
  const variable = ranking.variable ?? 'anom'
  const column = ranking.units === 'degC' ? `mean_${variable}_degC` : `mean_${variable}`
  const rows: Cell[][] = [['month', 'month_name', 'year', 'rank', column, 'sd', 'days', 'partial']]
  for (let m = 1; m <= 12; m++) {
    for (const row of ranking.months[String(m)] ?? []) {
      rows.push([
        m,
        MONTHS[m - 1],
        row.year,
        row.rank,
        number(row.mean, precision),
        number(row.sd, precision),
        row.n,
        row.partial ? 'true' : 'false',
      ])
    }
  }
  return csvText(rows)
}

/**
 * Hand the text to the browser as a file.
 *
 * A Blob URL rather than a `data:` one: a daily series is ~15k rows and some
 * browsers cap a `data:` URL well below that. Revoked on the next tick, since
 * revoking synchronously after `click()` can race the download in WebKit.
 */
export function downloadCsv(filename: string, text: string): void {
  const url = URL.createObjectURL(new Blob([text], { type: 'text/csv;charset=utf-8' }))
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = filename
  anchor.style.display = 'none'
  document.body.appendChild(anchor)
  anchor.click()
  anchor.remove()
  setTimeout(() => URL.revokeObjectURL(url), 0)
}
