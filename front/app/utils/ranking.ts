/**
 * The monthly-ranking browser's data shapes, layout maths, and ECharts options.
 *
 * Kept out of the component for the same reason `periods.ts` is: it is pure
 * arithmetic over the API's response, so both charts can be rendered and checked
 * without a browser, and `MonthlyRankingBrowser.vue` stays presentational.
 *
 * Two options are built here, and they are deliberately the same picture at two
 * sizes: `thumbOption()` is the rail's 12 miniatures (marks only — the month name
 * is HTML beside it, where it stays crisp and selectable), `detailOption()` is
 * the one month the rail has selected, at a legible pitch.
 */

import type {
  CustomSeriesRenderItemAPI,
  CustomSeriesRenderItemParams,
  EChartsOption,
  TooltipComponentFormatterCallbackParams,
} from 'echarts'
import type { ColorStop } from './colorScale'
import { NO_CLASS_COLOR, colorScale } from './colorScale'

/** The rectangle a cartesian grid occupies, which `params.coordSys` widens away. */
interface GridRect { x: number, y: number, width: number, height: number }

/** One item's tooltip payload — our own data row. */
interface ItemParams { value: number[] }

/** One year's showing in a given calendar month, at one cell. */
export interface RankingRow {
  year: number
  /** Mean daily anomaly over the month, degC. */
  mean: number
  /** Standard deviation of the daily values inside the month. */
  sd: number | null
  /** Days behind the mean (30 for March 1986 — OISST is missing the 18th). */
  n: number
  /** 1 = warmest year on record for this calendar month. */
  rank: number
  /**
   * Truncated by the edge of the archive — the month still filling up, or the
   * one the record starts mid-way through. Ranked with the rest, but starred:
   * its mean is over a part-month and its rank will move as the days land.
   */
  partial?: boolean
}

export interface MonthlyRanking {
  cell: { gy: number, lat: number, lon: number }
  /** Which field the years are ranked by. */
  variable?: 'sst' | 'anom' | 'mhw'
  units?: string
  /** Every month ranked, edge months included — first of the first to last of the last. */
  span: { start: string, end: string } | null
  /** Last day with data, which is where a trailing partial month has got to. */
  through?: string | null
  /** How many ranks count as "top" — drives the emphasis, not the row count. */
  top: number
  /** Keyed by month number as a string, '1'..'12', each already in rank order. */
  months: Record<string, RankingRow[]>
}

export const MONTHS = [
  'January', 'February', 'March', 'April', 'May', 'June',
  'July', 'August', 'September', 'October', 'November', 'December',
] as const

// --- Shared look -------------------------------------------------------------

/** Dark halo separating a dot from its neighbours without naming a theme token. */
const RING = 'rgba(0, 0, 0, 0.55)'
/** The app's "where the map is" accent, same as TimeseriesChart's MAP markLine. */
export const ACCENT = '#fbbf24'
const ZERO_LINE = { color: '#94a3b8', type: 'dashed' as const, width: 1 }

// --- Detail-panel layout -----------------------------------------------------
// All px. The panel is sized to the pane it is given: `detailPitch()` spends the
// available height on the rows, so one month normally needs no scrolling at all —
// which is the whole reason for showing one month at a time rather than twelve.
/** Room for a "23. 2026 *" y-label — the star on an edge month costs a few px. */
const LABEL_W = 74
const TOP_PAD = 10
const AXIS_H = 34
const RIGHT_PAD = 18
/** Below this a dot plus its gap stops being readable; the pane scrolls instead. */
const MIN_PITCH = 9
/**
 * Above this the panel stops looking like a plot and starts looking like a list
 * of widely-spaced dots. It is high enough that a full 45-year month spends the
 * whole of a tall dock on rows — 45 x 40 is 1800px — so in practice the cap only
 * bites on a *short* month, where spreading a handful of years over the pane
 * would exaggerate the gaps between them.
 */
const MAX_PITCH = 40

export function monthsOf(ranking: MonthlyRanking | null): RankingRow[][] {
  return MONTHS.map((_, m) => ranking?.months?.[String(m + 1)] ?? [])
}

/** Vertical px per year for `rowCount` rows in `available` px of pane. */
export function detailPitch(rowCount: number, available: number): number {
  const each = (available - TOP_PAD - AXIS_H) / Math.max(1, rowCount)
  return Math.max(MIN_PITCH, Math.min(MAX_PITCH, each))
}

/** Natural height of the detail panel at that pitch. */
export function detailHeightFor(rowCount: number, pitch: number): number {
  return TOP_PAD + AXIS_H + pitch * Math.max(1, rowCount)
}

function signed(value: number): string {
  return `${value > 0 ? '+' : ''}${value.toFixed(2)}`
}

/** Days in a 1-based calendar month — day 0 of the next one. */
export function daysInMonth(year: number, month: number): number {
  return new Date(Date.UTC(year, month, 0)).getUTCDate()
}

/**
 * The starred rows of a month, if any — at most the archive's two edge months.
 *
 * The star is the whole point of showing them: a part-month mean competes with
 * whole ones (August 2026 lands at rank 2 on 24 days at 45.125°N), so the row is
 * there to be read, with the caveat attached rather than the row removed.
 */
export function partialNote(rows: RankingRow[], month: number): string | null {
  const r = rows.find(row => row.partial)
  if (!r) return null
  return `* ${MONTHS[month - 1]} ${r.year} is incomplete — ranked on ${r.n} `
    + `of ${daysInMonth(r.year, month)} days, so its place will move.`
}

// --- Shared x-domain ---------------------------------------------------------

export interface XDomain { min: number, max: number }

/**
 * One x-domain across all twelve months, used by the rail and the detail alike.
 *
 * Per-month autoscaling would make a mild month look as violent as an extreme
 * one. Sharing it is what lets the rail be read as a rail: the diagonals lean
 * differently because the months differ, not because each was drawn to fit.
 */
export function xDomainOf(months: RankingRow[][]): XDomain {
  let lo = 0
  let hi = 0
  for (const rows of months) {
    for (const r of rows) {
      lo = Math.min(lo, r.mean - (r.sd ?? 0))
      hi = Math.max(hi, r.mean + (r.sd ?? 0))
    }
  }
  const pad = (hi - lo) * 0.04 || 0.5
  return { min: lo - pad, max: hi + pad }
}

// --- Rail thumbnail ----------------------------------------------------------

export interface ThumbOptionInput {
  rows: RankingRow[]
  /** `/domain`'s diverging stops, so a dot here is the colour that cell has on the map. */
  stops: ColorStop[]
  domain: XDomain
  /** Year on the map; ringed amber here too, so the rail shows where it sits every month. */
  selectedYear?: number | null
  /**
   * Whether the ranked value is a mean over an ordinal class. Below the first
   * class it is drawn neutral rather than clamped: a mean heatwave category of
   * 0.0 is "no heatwave all month", and painting it Cat 1's yellow says exactly
   * the opposite of what happened.
   */
  categorical?: boolean
}

/**
 * A month reduced to its ranked spine: one dot per year, no axes, no labels.
 *
 * Deliberately not a shrunken copy of the detail panel — at ~1.4px per year the
 * whiskers would be mush and the tick labels unreadable. What survives the size
 * is the shape (how far the diagonal leans, where it crosses zero) and the
 * colour, which is exactly what the rail is being scanned for.
 */
export function thumbOption({ rows, stops, domain, selectedYear, categorical }: ThumbOptionInput): EChartsOption {
  const scale = colorScale(stops, categorical ? NO_CLASS_COLOR : undefined)
  return {
    animation: false,
    backgroundColor: 'transparent',
    grid: { left: 5, right: 5, top: 4, bottom: 4 },
    xAxis: { type: 'value', min: domain.min, max: domain.max, show: false },
    // A value axis rather than a category one: with no labels to place there is
    // nothing a category buys, and this keeps the spine's spacing exact.
    yAxis: { type: 'value', min: 0, max: Math.max(1, rows.length - 1), inverse: true, show: false },
    series: [{
      type: 'scatter',
      silent: true,
      data: rows.map((r, i) => ({
        value: [r.mean, i],
        symbolSize: selectedYear != null && r.year === selectedYear ? 6 : 3.5,
        itemStyle: {
          color: scale(r.mean),
          // No labels here to carry a star, so a truncated month's dot is drawn
          // faint instead — the rail is scanned for the spine's shape, and this
          // says "provisional" without changing where the dot sits.
          opacity: r.partial ? 0.45 : 1,
          ...(selectedYear != null && r.year === selectedYear
            ? { borderColor: ACCENT, borderWidth: 1.5 }
            : {}),
        },
      })),
      markLine: {
        silent: true,
        symbol: 'none',
        animation: false,
        label: { show: false },
        data: [{ xAxis: 0, lineStyle: ZERO_LINE }],
      },
    }],
  }
}

// --- Detail panel ------------------------------------------------------------

export interface DetailOptionInput {
  rows: RankingRow[]
  /** 1-based calendar month, for the tooltip's heading. */
  month: number
  stops: ColorStop[]
  domain: XDomain
  /** Vertical px per year, from `detailPitch()`. */
  pitch: number
  /** Ranks at or above which a row is emphasised. */
  topN: number
  selectedYear?: number | null
  /**
   * Unit suffix for the tooltip — '°C' for the temperature variables, '' for
   * the marine-heatwave category, which has none. Passed in rather than assumed:
   * this file is pure so it can be asserted on headlessly, and a hard-coded
   * degree sign was printing "0.52 °C" at a variable measured in categories.
   */
  unit?: string
  /**
   * Whether the ranked value is a mean over an ordinal class. `/monthlyRanking`
   * averages the daily category deliberately — a max would put half the archive
   * on Cat 1 and rank nothing — so the number is a severity index rather than a
   * category, and it is signed only when the underlying scale is.
   */
  categorical?: boolean
}

export function detailOption({
  rows, month, stops, domain, pitch, topN, selectedYear, unit = '\u00B0C', categorical = false,
}: DetailOptionInput): EChartsOption {
  // A leading '+' only means something on a scale centred at zero. The anomaly
  // is; a mean heatwave category, which cannot go below zero, is not.
  const scale = colorScale(stops, categorical ? NO_CLASS_COLOR : undefined)
  const value = (n: number) => (categorical ? n.toFixed(2) : signed(n))
  const suffix = unit ? ` ${unit}` : ''
  const panelH = pitch * Math.max(1, rows.length)
  const labelSize = pitch >= 14 ? 11 : 9
  const dotR = Math.min(5, Math.max(3, pitch / 3.5))

  return {
    animation: false,
    backgroundColor: 'transparent',
    tooltip: {
      trigger: 'item',
      appendToBody: true,
      formatter: (raw: TooltipComponentFormatterCallbackParams) => {
        const [, mean, , , year, sd, n, rank, partial] = (raw as unknown as ItemParams).value
        const lines = [
          `<b>${MONTHS[month - 1]} ${year}${partial ? ' *' : ''}</b>`,
          `mean&nbsp;&nbsp;<b>${value(mean!)}${suffix}</b>`,
          `sd&nbsp;&nbsp;${sd!.toFixed(2)}${suffix} over ${n} days`,
          `rank&nbsp;&nbsp;${rank} of ${rows.length}`,
        ]
        if (partial) {
          lines.push(
            `<span style="opacity:0.75">incomplete — ${n} of `
            + `${daysInMonth(year!, month)} days</span>`,
          )
        }
        return lines.join('<br/>')
      },
    },
    grid: { left: LABEL_W, top: TOP_PAD, right: RIGHT_PAD, height: panelH },
    xAxis: {
      type: 'value',
      min: domain.min,
      max: domain.max,
      position: 'bottom',
      axisLabel: {
        fontSize: 10,
        color: '#cbd5e1',
        // The domain's own ends are padded, non-round numbers; labelling them
        // puts "-2.18" next to "-2.00". The round ticks between are the scale.
        showMinLabel: false,
        showMaxLabel: false,
        // The panel is as wide as the dock the user left it at; at the narrow
        // end the round ticks would otherwise run into each other.
        hideOverlap: true,
        formatter: (v: number) => value(v),
      },
      axisLine: { show: false },
      axisTick: { show: false },
      splitLine: { show: true, lineStyle: { color: 'rgba(148, 163, 184, 0.12)' } },
    },
    yAxis: {
      type: 'category',
      // Rank 1 belongs at the top; a category axis counts up from the bottom.
      inverse: true,
      data: rows.map(r => `${r.rank}. ${r.year}${r.partial ? ' *' : ''}`),
      axisLine: { show: false },
      axisTick: { show: false },
      splitLine: { show: false },
      axisLabel: {
        // Emphasis is carried by weight and ink, never by hue: colour in this
        // chart already means anomaly, and reusing it for rank would collide.
        margin: 8,
        // Without this ECharts thins 45 categories down to whichever it likes.
        interval: 0,
        formatter: (value: string, index: number) => {
          const row = rows[index]
          if (!row) return value
          if (selectedYear != null && row.year === selectedYear) return `{sel|${value}}`
          return row.rank <= topN ? `{top|${value}}` : `{rest|${value}}`
        },
        rich: {
          sel: { color: ACCENT, fontWeight: 'bold' as const, fontSize: labelSize },
          top: { color: '#e2e8f0', fontWeight: 'bold' as const, fontSize: labelSize },
          // Still the quietest of the three — the rank encoding is the
          // *contrast* between these, not any one of them being unreadable.
          rest: { color: '#94a3b8', fontSize: labelSize },
        },
      },
    },
    series: [{
      type: 'custom',
      name: MONTHS[month - 1],
      // dims: 0 lo, 1 mean, 2 hi, 3 category, then metadata the tooltip reads.
      encode: { x: [0, 1, 2], y: 3 },
      data: rows.map((r, i) => [
        r.mean - (r.sd ?? 0), r.mean, r.mean + (r.sd ?? 0), i, r.year, r.sd ?? 0, r.n, r.rank,
        r.partial ? 1 : 0,
      ]),
      markLine: {
        silent: true,
        symbol: 'none',
        animation: false,
        label: { show: false },
        data: [{ xAxis: 0, lineStyle: ZERO_LINE }],
      },
      renderItem: (params: CustomSeriesRenderItemParams, api: CustomSeriesRenderItemAPI) => {
        const cs = params.coordSys as unknown as GridRect
        const cat = api.value(3) as number
        const mean = api.value(1) as number
        const rank = api.value(7) as number
        const isSelected = selectedYear != null && (api.value(4) as number) === selectedYear
        const isPartial = (api.value(8) as number) === 1
        const [cx, cy] = api.coord([mean, cat]) as number[]
        const loX = (api.coord([api.value(0) as number, cat]) as number[])[0]!
        const hiX = (api.coord([api.value(2) as number, cat]) as number[])[0]!
        const cap = Math.min(4, pitch / 3)
        const stroke = scale(mean)
        const whisker = { stroke, lineWidth: 1.5, opacity: 0.6 }

        return {
          type: 'group',
          children: [
            // Full-width band: the click's hit target (far larger than the mark),
            // and for the top N also the tint that carries the emphasis into the
            // plot area instead of leaving it on the labels alone.
            {
              type: 'rect',
              shape: { x: cs.x, y: cy - pitch / 2, width: cs.width, height: pitch },
              style: { fill: rank <= topN ? 'rgba(148, 163, 184, 0.08)' : 'rgba(0, 0, 0, 0)' },
            },
            { type: 'line', shape: { x1: loX, y1: cy, x2: hiX, y2: cy }, style: whisker },
            { type: 'line', shape: { x1: loX, y1: cy - cap, x2: loX, y2: cy + cap }, style: whisker },
            { type: 'line', shape: { x1: hiX, y1: cy - cap, x2: hiX, y2: cy + cap }, style: whisker },
            {
              type: 'circle',
              shape: { cx, cy, r: dotR },
              style: {
                // An open dot for a part-month: the same colour and position, so
                // it still reads on the scale, but visibly not yet a datum on the
                // same footing as the closed months around it. The `*` on its
                // label says why.
                fill: isPartial ? 'transparent' : stroke,
                stroke: isSelected ? ACCENT : (isPartial ? stroke : RING),
                lineWidth: isSelected ? 2 : 1.5,
              },
            },
          ],
        }
      },
    }],
  }
}
