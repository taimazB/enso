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
import { colorScale } from './colorScale'

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
}

export interface MonthlyRanking {
  cell: { gy: number, lat: number, lon: number }
  /** The complete-month window ranked — narrower than coverage, which ends mid-month. */
  span: { start: string, end: string } | null
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
/** Room for a "23. 2026" y-label. */
const LABEL_W = 66
const TOP_PAD = 10
const AXIS_H = 34
const RIGHT_PAD = 18
/** Below this a dot plus its gap stops being readable; the pane scrolls instead. */
const MIN_PITCH = 9
/** Above this the panel is just airy — 45 rows in ~900px sits around 19. */
const MAX_PITCH = 22

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
}

/**
 * A month reduced to its ranked spine: one dot per year, no axes, no labels.
 *
 * Deliberately not a shrunken copy of the detail panel — at ~1.4px per year the
 * whiskers would be mush and the tick labels unreadable. What survives the size
 * is the shape (how far the diagonal leans, where it crosses zero) and the
 * colour, which is exactly what the rail is being scanned for.
 */
export function thumbOption({ rows, stops, domain, selectedYear }: ThumbOptionInput): EChartsOption {
  const scale = colorScale(stops)
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
        itemStyle: selectedYear != null && r.year === selectedYear
          ? { color: scale(r.mean), borderColor: ACCENT, borderWidth: 1.5 }
          : { color: scale(r.mean) },
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
}

export function detailOption({
  rows, month, stops, domain, pitch, topN, selectedYear,
}: DetailOptionInput): EChartsOption {
  const scale = colorScale(stops)
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
        const [, mean, , , year, sd, n, rank] = (raw as unknown as ItemParams).value
        return [
          `<b>${MONTHS[month - 1]} ${year}</b>`,
          `mean&nbsp;&nbsp;<b>${signed(mean!)} °C</b>`,
          `sd&nbsp;&nbsp;${sd!.toFixed(2)} °C over ${n} days`,
          `rank&nbsp;&nbsp;${rank} of ${rows.length}`,
        ].join('<br/>')
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
        color: '#94a3b8',
        // The domain's own ends are padded, non-round numbers; labelling them
        // puts "-2.18" next to "-2.00". The round ticks between are the scale.
        showMinLabel: false,
        showMaxLabel: false,
        formatter: (v: number) => signed(v),
      },
      axisLine: { show: false },
      axisTick: { show: false },
      splitLine: { show: true, lineStyle: { color: 'rgba(148, 163, 184, 0.12)' } },
    },
    yAxis: {
      type: 'category',
      // Rank 1 belongs at the top; a category axis counts up from the bottom.
      inverse: true,
      data: rows.map(r => `${r.rank}. ${r.year}`),
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
          rest: { color: '#64748b', fontSize: labelSize },
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
                fill: stroke,
                stroke: isSelected ? ACCENT : RING,
                lineWidth: isSelected ? 2 : 1.5,
              },
            },
          ],
        }
      },
    }],
  }
}
