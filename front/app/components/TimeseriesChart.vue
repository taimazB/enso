<template>
  <div class="relative size-full">
    <TimeControl />

    <div
      v-if="!hasData"
      class="flex size-full items-center justify-center px-6 text-center text-sm text-muted"
    >
      <UIcon v-if="loading" name="i-mdi-loading" class="size-5 animate-spin" />
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

const props = defineProps<{
  series: Series | null
  loading?: boolean
  emptyMessage?: string
  title?: string
  /** Bucket currently on the map, marked on the x-axis. */
  selectedDate?: string | null
}>()

const emit = defineEmits<{ select: [date: string] }>()

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
  const data: Array<Record<string, unknown>> = [
    { yAxis: 0, lineStyle: { color: '#94a3b8', type: 'dashed', width: 1 }, label: { show: false } },
  ]
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

function option(): echarts.EChartsOption {
  const points = (props.series?.dates ?? []).map((date, i) => [date, props.series!.values[i]])
  return {
    animation: false,
    grid: { top: 24, right: 16, bottom: 44, left: 52 },
    tooltip: { trigger: 'axis', valueFormatter: v => (v == null ? '—' : `${Number(v).toFixed(2)} °C`) },
    xAxis: { type: 'time' },
    yAxis: { type: 'value', name: '°C', nameLocation: 'end', nameGap: 8, splitLine: { show: true } },
    // The full record is ~16k daily points; dataZoom is what makes that browsable,
    // and `large` turns on ECharts' batched path so panning stays smooth.
    dataZoom: [
      { type: 'inside', throttle: 50 },
      { type: 'slider', height: 18, bottom: 6 },
    ],
    // A visualMap on the value axis colours warm anomalies red and cool ones
    // blue, matching the map's diverging scale.
    visualMap: {
      show: false,
      type: 'continuous',
      min: -3,
      max: 3,
      seriesIndex: 0,
      inRange: { color: ['#2166ac', '#67a9cf', '#f7f7f7', '#ef8a62', '#b2182b'] },
    },
    series: [
      {
        type: 'line',
        data: points,
        showSymbol: false,
        large: true,
        lineStyle: { width: 1 },
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

onBeforeUnmount(() => {
  observer?.disconnect()
  chart?.getZr().off('click', onZrClick)
  chart?.dispose()
  chart = null
})
</script>
