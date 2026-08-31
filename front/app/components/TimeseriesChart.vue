<template>
  <div class="relative size-full">
    <TimeControl />

    <div
      v-if="!hasData"
      class="flex size-full items-center justify-center px-6 text-center text-sm text-muted"
    >
      <UIcon v-if="loading" name="i-mdi-loading" class="size-5 animate-spin" />
      <!-- An outright failure is not the same as nothing to show yet, so it
           does not get the neutral hint's styling. -->
      <span v-else-if="error" class="flex items-center gap-2 text-error">
        <UIcon name="i-mdi-alert-circle-outline" class="size-4 shrink-0" />
        {{ emptyMessage }}
      </span>
      <span v-else>{{ emptyMessage }}</span>
    </div>

    <ClientOnly v-else>
      <!-- Clicking the plot sets the map date, so the whole rail reads as clickable. -->
      <div ref="container" class="size-full cursor-pointer" />
    </ClientOnly>
  </div>
</template>

<script setup lang="ts">
import * as echarts from 'echarts'
import type { Series } from '~/stores/main'
import { NO_CLASS_COLOR, type ColorStop } from '~/utils/colorScale'
import { wholeClasses } from '~/utils/stats'

const props = defineProps<{
  series: Series | null
  loading?: boolean
  emptyMessage?: string
  /** Render `emptyMessage` as a failure rather than as a neutral hint. */
  error?: boolean
  title?: string
  /** Bucket currently on the map, marked on the x-axis. */
  selectedDate?: string | null
  /**
   * The active variable's colour stops, already spread over the displayed range
   * (`store.activeStops`). The line is coloured with exactly these, so a value
   * has the same colour here as it does on the map — including after the user
   * drags the colour range. Empty falls back to a plain line.
   */
  stops?: ColorStop[]
  /**
   * Whether zero is meaningful for the charted variable. It is for `anom` (the
   * dashed baseline) and not for `sst`, where drawing it would also drag the
   * y-axis down to 0 and squash a 25-30 degC record into the top tenth of the pane.
   */
  zeroLine?: boolean
  /**
   * Unit suffix for the tooltip and the y-axis name — '°C' for the two
   * temperature variables, '' for the marine-heatwave category. It was hard-coded
   * as '°C', which reads as "Cat 3 °C" on a variable that has no unit.
   */
  unit?: string
  /**
   * An ordinal class rather than a measurement. Changes the ramp from continuous
   * to piecewise and pins the y-axis to whole categories — a "2.5" tick on an
   * axis whose values are only ever integers invites reading a value that
   * cannot occur.
   */
  categorical?: boolean
}>()

const emit = defineEmits<{ select: [date: string] }>()

// ECharts' default axis ink is #6E7079, which is a mid grey meant for a light
// theme — against this dashboard's dark pane the ticks and the unit name read as
// disabled text. The labels are the only thing that says what the line's values
// ARE, so they are drawn bright; the lines and gridlines they sit on stay faint,
// which is what keeps the plot the loudest thing in the pane.
const AXIS_LABEL = '#e2e8f0'
const AXIS_LINE = 'rgba(148, 163, 184, 0.35)'
const AXIS_SPLIT = 'rgba(148, 163, 184, 0.12)'

/** Bright ticks and unit name, faint rules — shared by both axes. */
const axisStyle = {
  axisLabel: { color: AXIS_LABEL },
  nameTextStyle: { color: AXIS_LABEL },
  axisLine: { lineStyle: { color: AXIS_LINE } },
  axisTick: { lineStyle: { color: AXIS_LINE } },
} as const

const container = ref<HTMLElement | null>(null)
let chart: echarts.ECharts | null = null

const hasData = computed(() => (props.series?.dates.length ?? 0) > 0)

/** Bucket starts as epoch ms, ascending — the snap targets for a click. */
const stamps = computed(() => (props.series?.dates ?? []).map(d => Date.parse(`${d.slice(0, 10)}T00:00:00Z`)))

/** The series' own bucket nearest to an x value, so a click can only land on a real frame. */
function nearestDate(x: number): string | null {
  const values = stamps.value
  if (!values.length) return null
  let lo = 0
  let hi = values.length - 1
  while (lo < hi) {
    const mid = (lo + hi) >> 1
    if (values[mid]! < x) lo = mid + 1
    else hi = mid
  }
  // `lo` is the first bucket at or after the click; its left neighbour may be closer.
  const prev = Math.max(0, lo - 1)
  const best = Math.abs(values[lo]! - x) < Math.abs(values[prev]! - x) ? lo : prev
  return props.series?.dates[best] ?? null
}

function markLine(): echarts.SeriesOption['markLine'] {
  const data: Array<Record<string, unknown>> = []
  if (props.zeroLine) {
    data.push({ yAxis: 0, lineStyle: { color: '#94a3b8', type: 'dashed', width: 1 }, label: { show: false } })
  }
  if (props.selectedDate) {
    data.push({
      xAxis: props.selectedDate,
      lineStyle: { color: '#fbbf24', width: 1 },
      label: {
        show: true,
        formatter: 'MAP',
        position: 'insideEndTop',
        color: '#fbbf24',
        fontSize: 10,
      },
    })
  }
  return { silent: true, symbol: 'none', animation: false, data }
}

/**
 * Colour the line by value, on the *active variable's* scale.
 *
 * The stops arrive already spread over the displayed range, and the server
 * samples the colormap at evenly spaced points — which is exactly how a
 * continuous visualMap lays `inRange.color` out between min and max — so the
 * line takes the same colour the map gives that value, palette and range
 * included. This used to be a hard-coded diverging ±3 ramp, which is right for
 * `anom` and nonsense for `sst`, where every ocean temperature above 3 °C sat
 * pinned at the red end.
 */
function visualMap(): echarts.EChartsOption['visualMap'] {
  const stops = props.stops ?? []
  if (stops.length < 2) return undefined
  if (props.categorical) {
    // Piecewise, for the same reason the map's ramp is a `step`: there is no
    // colour between two classes because there is no value between them. A
    // continuous map would blend Cat 2's amber into Cat 3's orange across a
    // boundary the data never crosses gradually.
    //
    // The 0 bucket is deliberately included and grey: at a point, "no heatwave"
    // is a real reading and most of a series is made of it, so it has to be
    // drawn — unlike on the map, where transparent means "look elsewhere".
    //
    // The pieces are half-open BANDS rather than exact values, and that is
    // load-bearing. `{ value: 1 }` matches a value that is exactly 1, and
    // ECharts renders a value-piece list as a y-axis gradient in which every
    // class colour occupies a *zero-width* band and everything between them is
    // `stop-opacity: 0` — so the line was drawn transparent and the pane read
    // as an empty chart that still answered clicks. A region never lands on an
    // integer at all (its value is a cos(lat)-weighted area mean of classes,
    // 0.202), so nothing was ever visible there. Bands also give that mean the
    // reading the map's `step` ramp gives it: [1, 2) is Cat 1's colour, and
    // below Cat 1 is no class rather than the first one.
    return {
      show: false,
      type: 'piecewise',
      seriesIndex: 0,
      pieces: [
        { lt: stops[0]!.value, color: NO_CLASS_COLOR },
        ...stops.map((s: ColorStop, i: number) => (i === stops.length - 1
          ? { gte: s.value, color: s.color }
          : { gte: s.value, lt: stops[i + 1]!.value, color: s.color })),
      ],
    }
  }
  return {
    show: false,
    type: 'continuous',
    min: stops[0]!.value,
    max: stops[stops.length - 1]!.value,
    seriesIndex: 0,
    inRange: { color: stops.map((s: ColorStop) => s.color) },
  }
}

/**
 * Whether the series' values are whole classes, or means of them.
 *
 * A point reads a class — 3 is Severe — and its axis is the five classes. A
 * region reads a cos(lat)-weighted **area mean** of classes (0.202), which is
 * continuous, is not a category, and sits near the floor of a 0..5 axis.
 * `StatsPanel` draws the same distinction and reads the same helper, so the
 * axis name and the panel's headline can never disagree about which it is.
 */
const isWholeClasses = computed(() => wholeClasses(props.series))

/**
 * Tooltip text for one value.
 *
 * A category is printed as a whole number and named — "3 (Severe)" — because the
 * number alone is what NOAA's five classes are read *by*, and 0 is the common
 * case and needs saying out loud rather than looking like a missing point.
 */
function formatValue(value: unknown): string {
  if (value == null) return '—'
  const n = Number(value)
  if (!props.categorical) return `${n.toFixed(2)}${props.unit ? ` ${props.unit}` : ''}`
  // A region's number is a mean of classes and must not be named as one — nor
  // printed raw, where a float lands as `0.20200000000000001`.
  if (!Number.isInteger(n)) return n.toFixed(2)
  const label = (props.stops ?? []).find((s: ColorStop) => s.value === n)?.label
  return n === 0 ? '0 (no heatwave)' : `${n}${label ? ` (${label})` : ''}`
}

function option(): echarts.EChartsOption {
  const points = (props.series?.dates ?? []).map((date, i) => [date, props.series!.values[i]])
  const ramp = visualMap()
  return {
    animation: false,
    grid: { top: 24, right: 16, bottom: 44, left: 52 },
    tooltip: { trigger: 'axis', valueFormatter: formatValue },
    xAxis: { type: 'time', ...axisStyle },
    // `scale: true` frees the axis from having to include zero. Without it a
    // tropical SST record — 25 to 30 degC — is drawn against a 0-30 axis and
    // reads as a flat line; with it the axis still picks nice round bounds,
    // just ones that bracket the data.
    //
    // A categorical axis wants the opposite: zero IS meaningful (it is "no
    // heatwave", where most of the series sits), the whole scale is only ever
    // 0..5, and only whole numbers can occur — so it is pinned, with an interval
    // of 1 so no tick lands on a value the data cannot take.
    //
    // Only where the values ARE classes, though: a region's area mean of them
    // spends the whole archive under 1, and pinning that to 0..5 draws it as a
    // flat line along the axis floor. There the axis keeps its zero — "no
    // heatwave" is still the meaningful bottom — and lets the top follow the
    // data.
    yAxis: props.categorical
      ? {
          ...axisStyle,
          type: 'value',
          min: 0,
          max: isWholeClasses.value ? 5 : undefined,
          interval: isWholeClasses.value ? 1 : undefined,
          name: props.unit || (isWholeClasses.value ? 'category' : 'mean category'),
          nameLocation: 'end',
          nameGap: 8,
          splitLine: { show: true, lineStyle: { color: AXIS_SPLIT } },
        }
      : {
          ...axisStyle,
          type: 'value',
          scale: true,
          name: props.unit ?? '°C',
          nameLocation: 'end',
          nameGap: 8,
          splitLine: { show: true, lineStyle: { color: AXIS_SPLIT } },
        },
    // The full record is ~16k daily points; dataZoom is what makes that browsable,
    // and `large` turns on ECharts' batched path so panning stays smooth.
    dataZoom: [
      { type: 'inside', throttle: 50 },
      { type: 'slider', height: 18, bottom: 6, textStyle: { color: AXIS_LABEL } },
    ],
    visualMap: ramp,
    series: [
      {
        type: 'line',
        data: points,
        showSymbol: false,
        large: true,
        // An explicit lineStyle.color *beats* the visualMap rather than losing
        // to it, so the fallback colour is only set when there is no ramp to
        // apply — otherwise the line draws flat cyan and silently ignores the
        // scale.
        lineStyle: ramp ? { width: 1 } : { width: 1, color: '#38bdf8' },
        markLine: markLine(),
      },
    ],
  }
}

/**
 * Clicks land on the ZRender canvas rather than on a symbol: the line is drawn
 * with `showSymbol: false` and `large`, so there is nothing for ECharts' own
 * `'click'` event to hit. Converting the raw pixel and snapping to the nearest
 * bucket is what makes anywhere in the plot area a valid target.
 */
function onZrClick(event: { offsetX?: number, offsetY?: number, event?: { zrX?: number, zrY?: number } }) {
  if (!chart) return
  const px = event.offsetX ?? event.event?.zrX
  const py = event.offsetY ?? event.event?.zrY
  if (px == null || py == null) return
  const pixel: [number, number] = [px, py]
  if (!chart.containPixel('grid', pixel)) return
  const x = chart.convertFromPixel({ gridIndex: 0 }, pixel)?.[0]
  if (x == null) return
  const date = nearestDate(Number(x))
  if (date && date !== props.selectedDate) emit('select', date)
}

function render() {
  if (!container.value) return
  if (!chart) {
    chart = echarts.init(container.value, null, { renderer: 'canvas' })
    chart.getZr().on('click', onZrClick)
  }
  chart.setOption(option(), { notMerge: true })
}

let observer: ResizeObserver | null = null

onMounted(() => {
  observer = new ResizeObserver(() => chart?.resize())
  if (container.value) observer.observe(container.value)
  if (hasData.value) nextTick(render)
})

watch(() => props.series, () => {
  if (!hasData.value) {
    chart?.dispose()
    chart = null
    return
  }
  nextTick(() => {
    if (container.value && observer) observer.observe(container.value)
    render()
  })
})

// Merged in rather than re-rendered: a full `notMerge` setOption would reset the
// dataZoom window, so every click on the chart would throw away the user's zoom.
watch(() => props.selectedDate, () => {
  if (chart && hasData.value) chart.setOption({ series: [{ markLine: markLine() }] })
})

// Dragging the colour range is a recolour and nothing else — same reason the map
// repaints without refetching a frame — so it merges in and leaves the zoom alone.
watch(() => props.stops, () => {
  const ramp = visualMap()
  if (chart && hasData.value && ramp) chart.setOption({ visualMap: ramp })
}, { deep: true })

onBeforeUnmount(() => {
  observer?.disconnect()
  chart?.getZr().off('click', onZrClick)
  chart?.dispose()
  chart = null
})
</script>
