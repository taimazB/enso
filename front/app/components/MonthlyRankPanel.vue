<template>
  <!-- `@container`: this lives in a dock the user can drag narrower or wider, so
       what fits is a question about this element, not the viewport. -->
  <div class="@container relative flex w-full min-h-0 flex-col">
    <div
      v-if="!hasData"
      class="flex min-h-32 w-full items-center justify-center px-6 text-center text-sm text-muted"
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
      <section class="flex min-w-0 min-h-0 grow flex-col">
        <!-- The month is the map's, not a selection of its own: the panel sits
             under the numbers for the bucket on screen, so a month picker here
             would be a second date control disagreeing with the time bar. -->
        <div class="mb-1 flex shrink-0 items-baseline gap-2">
          <h3 class="shrink-0 text-sm font-semibold text-highlighted">{{ MONTHS[month - 1] }}</h3>
          <span class="truncate text-xs text-muted">
            {{ rows.length }} years, {{ rankOrder }} first{{ ranking?.areaMean ? ' · area mean' : '' }}
          </span>
          <!-- Five encodings share this plot — dot, whisker, label weight, ring,
               fill — and none of them is self-evident on a first look. On demand
               rather than always on: it is read once and then in the way. -->
          <!-- `@update:open` rather than a click handler on the button: the
               popover closes by the same event, and counting both would double
               every read. -->
          <UPopover :content="{ side: 'bottom', align: 'end' }" @update:open="onGuideToggle">
            <!-- The word is spent only where the dock is wide enough for it;
                 dragged in, the icon alone carries it. Rendered as slot content
                 rather than through `label`, so the responsive class is plain
                 Tailwind on an element we own. -->
            <UButton
              icon="i-mdi-help-circle-outline"
              variant="ghost"
              color="neutral"
              size="xs"
              class="ml-auto shrink-0"
              title="How to read this chart"
              aria-label="How to read this chart"
            >
              <span class="hidden @[22rem]:inline">How to read</span>
            </UButton>

            <template #content>
              <div class="w-72 p-3 text-xs">
                <p class="mb-2 text-default">{{ guide.summary }}</p>
                <ul class="space-y-1.5">
                  <li v-for="item in guide.items" :key="item.glyph" class="flex gap-2">
                    <svg viewBox="0 0 28 12" class="mt-px h-3 w-7 shrink-0" aria-hidden="true">
                      <rect
                        v-if="item.glyph === 'top'"
                        x="0" y="0" width="28" height="12" rx="2"
                        fill="rgba(148, 163, 184, 0.12)"
                      />
                      <template v-if="item.glyph === 'whisker'">
                        <line x1="3" y1="6" x2="25" y2="6" :stroke="glyphColor" stroke-width="1.5" opacity="0.6" />
                        <line x1="3" y1="2.5" x2="3" y2="9.5" :stroke="glyphColor" stroke-width="1.5" opacity="0.6" />
                        <line x1="25" y1="2.5" x2="25" y2="9.5" :stroke="glyphColor" stroke-width="1.5" opacity="0.6" />
                      </template>
                      <text
                        v-if="item.glyph === 'top'"
                        x="14" y="9" text-anchor="middle"
                        font-size="8" font-weight="bold" fill="#e2e8f0"
                      >1.</text>
                      <circle
                        v-else
                        cx="14" cy="6" r="4"
                        :fill="item.glyph === 'partial' ? 'transparent' : glyphColor"
                        :stroke="item.glyph === 'selected'
                          ? ACCENT
                          : (item.glyph === 'partial' ? glyphColor : RING)"
                        :stroke-width="item.glyph === 'selected' ? 2 : 1.5"
                      />
                    </svg>
                    <span class="text-muted">{{ item.text }}</span>
                  </li>
                </ul>
                <p class="mt-2 text-dimmed">{{ guide.footer }}</p>
              </div>
            </template>
          </UPopover>
          <UButton
            icon="i-mdi-download"
            variant="ghost"
            color="neutral"
            size="xs"
            class="shrink-0"
            title="Download every month's ranking as CSV"
            aria-label="Download the rankings as CSV"
            @click="exportRanking()"
          />
        </div>
        <!-- The archive's edge months are ranked with the rest rather than
             hidden, so this is where the star is cashed in. -->
        <p v-if="note" class="mb-1 shrink-0 text-[11px] text-dimmed">{{ note }}</p>
        <!-- Only the plot scrolls, and `detailPitch()` is spent out of this
             element's height — so measuring it must not include the heading. -->
        <div ref="pane" class="min-h-0 grow overflow-y-auto">
          <div ref="detail" class="w-full cursor-pointer" :style="{ height: `${detailHeight}px` }" />
        </div>
      </section>
    </ClientOnly>

    <!-- A refetch keeps the previous series on screen rather than blanking it —
         the axes and the zoom window stay put, which is what makes toggling
         variable or period readable. But then nothing said a request was in
         flight, and the stale line was indistinguishable from the answer. So
         the spinner above covers "nothing yet" and this covers "something, but
         not this one": the plot is dimmed rather than hidden, since the point is
         that what you are looking at is about to be replaced. -->
    <div
      v-if="loading && hasData"
      class="pointer-events-none absolute inset-0 z-10 flex items-center justify-center bg-default/40"
    >
      <UIcon name="i-mdi-loading" class="size-5 animate-spin text-muted" />
    </div>
  </div>
</template>

<script setup lang="ts">
import * as echarts from 'echarts'
import { trackEvent } from '~/composables/useAnalytics'
import type { ColorStop } from '~/utils/colorScale'
import type { MonthlyRanking } from '~/utils/ranking'
import { cellSlug, downloadCsv, rankingCsv, slug } from '~/utils/csv'
import { type Period, bucketStart, shiftBuckets } from '~/utils/periods'
import {
  ACCENT,
  MONTHS,
  RING,
  detailHeightFor,
  detailOption,
  detailPitch,
  monthsOf,
  partialNote,
  readingGuide,
  xDomainOf,
} from '~/utils/ranking'
import { NO_CLASS_COLOR, colorScale } from '~/utils/colorScale'

const props = defineProps<{
  ranking: MonthlyRanking | null
  /** `/domain`'s diverging stops, so a dot here is the colour that cell has on the map. */
  stops: ColorStop[]
  loading?: boolean
  emptyMessage?: string
  /** Render `emptyMessage` as a failure rather than as a neutral hint. */
  error?: boolean
  /** Bucket on the map. Its month is the one drawn, and its year is ringed amber. */
  selectedDate?: string | null
  /** Unit suffix for the detail tooltip and x-axis; '' where there is none. */
  unit?: string
  /**
   * Whether the ranked value is a mean over an ordinal class (`mhw`). Drops the
   * signed '+', which only means something on a scale centred at zero.
   */
  categorical?: boolean
  /**
   * Whether zero is meaningful for the ranked variable — the anomaly's baseline,
   * and the heatwave category's floor, but nothing at all on an absolute SST.
   * It decides whether the x-domain is anchored there; see `xDomainOf`.
   */
  zeroLine?: boolean
  /**
   * What rank 1 means — 'warmest' for a temperature, 'most severe' for the
   * heatwave category, which is not a temperature at all.
   */
  rankOrder?: string
  /** Decimals the ranked means are written with on export; the panel's own. */
  precision?: number
  /** The bucket size the map is on, so a clicked row lands inside its month. */
  period?: Period
}>()

const emit = defineEmits<{ select: [date: string] }>()

/**
 * Save the whole ranking table — all twelve months, not the one on screen.
 *
 * The panel shows one month because that is the one the map is on; a file has no
 * such constraint, and a `month` column is what makes the table filterable. The
 * subject is in the filename because a ranking is only ever about one of them,
 * and two files are otherwise indistinguishable — the same twelve months and the
 * same years, with different numbers. A cell and a region are named differently
 * for the same reason they are not comparable numbers: one is a point reading,
 * the other an area mean.
 */
/**
 * Only the opening half of the popover's toggle is an event: closing it is not
 * a second read, and the panel is where five encodings are explained, so how
 * often this is opened is the measure of whether the plot reads on its own.
 */
function onGuideToggle(open: boolean) {
  if (open) trackEvent('ranking_guide_opened', { scope: props.ranking?.region ? 'region' : 'point' })
}

function exportRanking() {
  const ranking = props.ranking
  if (!ranking) return
  const variable = ranking.variable ?? 'anom'
  const subject = ranking.region
    ? slug(ranking.label ?? ranking.region)
    : ranking.cell ? cellSlug(ranking.cell) : 'selection'
  trackEvent('csv_downloaded', {
    kind: 'ranking',
    variable,
    scope: ranking.region ? 'region' : 'point',
    months: 12,
  })
  downloadCsv(
    `${variable}_monthly-ranks_${subject}.csv`,
    rankingCsv(ranking, props.precision ?? 2),
  )
}

/**
 * What `sd` is the spread of, which is not the same statistic in the two scopes.
 *
 * At a cell it is day-to-day spread of daily values; over a region it is the
 * spread of daily *area* means, which is much narrower because spatial averaging
 * cancels the noise one cell keeps. Labelled rather than renamed — it is the
 * same statistic over a different series, and silently reusing "sd" invites
 * comparing a region's against a cell's.
 */
const sdLabel = computed(() => (props.ranking?.areaMean ? 'sd of daily means' : 'sd'))

/**
 * The calendar month drawn: the map's own, with no local state beside it.
 *
 * The rail of twelve thumbnails that used to choose this is gone — the panel now
 * sits under the numbers for the bucket on screen, and those already answer
 * "which month". Stepping the map's date to another month redraws this.
 */
const month = computed(() => (props.selectedDate ? Number(props.selectedDate.slice(5, 7)) : 1))
const selectedYear = computed(() => (props.selectedDate ? Number(props.selectedDate.slice(0, 4)) : null))

const rows = computed(() => monthsOf(props.ranking)[month.value - 1] ?? [])
const hasData = computed(() => rows.value.length > 0)
const topN = computed(() => props.ranking?.top ?? 10)
/**
 * One month, scaled to itself — there is no rail left to be comparable with,
 * and on a variable with no meaningful zero it is not anchored there either: a
 * cell whose August sits at 6-8 degC was drawing every year jammed against the
 * right edge of a pane three quarters of which was empty.
 */
const domain = computed(() => xDomainOf([rows.value], props.zeroLine || props.categorical))
/** Spells out the `*` on a truncated edge month, when this month has one. */
const note = computed(() => partialNote(rows.value, month.value))

/**
 * What every mark on the plot means, built from the ranking on screen so the
 * wording follows the variable and the scope rather than describing a chart
 * that is not the one being looked at.
 */
const guide = computed(() => readingGuide({
  ranking: props.ranking,
  month: month.value,
  rows: rows.value,
  topN: topN.value,
  rankOrder: props.rankOrder,
  unit: props.unit,
  sdLabel: sdLabel.value,
}))

/**
 * A colour the guide's glyphs are drawn in: the active scale at the middle of
 * the domain on screen, so the sample dot is a colour actually in play rather
 * than a neutral grey that means "no class" on the heatwave scale.
 */
const glyphColor = computed(() => {
  const scale = colorScale(props.stops, props.categorical ? NO_CLASS_COLOR : undefined)
  return scale((domain.value.min + domain.value.max) / 2)
})

const pane = ref<HTMLElement | null>(null)
const detail = ref<HTMLElement | null>(null)
let chart: echarts.ECharts | null = null
/** Measured height of the scroll pane; the row pitch is spent out of it. */
const paneH = ref(0)

const pitch = computed(() => detailPitch(rows.value.length, paneH.value || 640))
const detailHeight = computed(() => detailHeightFor(rows.value.length, pitch.value))

/**
 * Where the map goes when a row is clicked, expressed so that it stays in the
 * month clicked.
 *
 * The store snaps whatever it is given to a bucket start, and the Monday of the
 * week containing the 1st usually sits in the *previous* month — which, now that
 * this panel follows the map's month rather than a selection of its own, would
 * bounce it straight off the month just clicked. The first Monday inside the
 * month is the nearest bucket that does not.
 */
function dateFor(year: number): string {
  const first = `${year}-${String(month.value).padStart(2, '0')}-01`
  if (props.period !== 'weekly') return first
  const start = bucketStart(first, 'weekly')
  return start < first ? shiftBuckets(start, 'weekly', 1) : start
}

function onClick(params: { componentType?: string, value?: unknown }) {
  if (params.componentType !== 'series') return
  const year = (params.value as number[])[4]
  if (year == null) return
  emit('select', dateFor(year))
}

function renderDetail() {
  const el = detail.value
  if (!el || !el.clientWidth || !rows.value.length) return
  if (!chart) {
    chart = echarts.init(el, null, { renderer: 'canvas' })
    chart.on('click', onClick)
  }
  chart.resize()
  chart.setOption(
    detailOption({
      rows: rows.value,
      month: month.value,
      stops: props.stops,
      domain: domain.value,
      pitch: pitch.value,
      topN: topN.value,
      selectedYear: selectedYear.value,
      unit: props.unit,
      categorical: props.categorical,
      signedScale: props.zeroLine,
      sdLabel: sdLabel.value,
    }),
    { notMerge: true },
  )
}

// --- Lifecycle ---------------------------------------------------------------

let observer: ResizeObserver | null = null
let resizeTimer: ReturnType<typeof setTimeout> | null = null

/**
 * Coalesced, because the dock's drag handle fires this on every frame and one
 * redraw is a 45-row panel. 100ms is invisible when the panel merely opens and
 * is the difference between smooth and gluey on a drag.
 */
function onResize() {
  if (resizeTimer) clearTimeout(resizeTimer)
  resizeTimer = setTimeout(() => {
    resizeTimer = null
    paneH.value = pane.value?.clientHeight ?? 0
    nextTick(renderDetail)
  }, 100)
}

/**
 * Watch the elements, not the mount.
 *
 * Everything here sits inside `<ClientOnly>`, which renders nothing until after
 * hydration, so at `onMounted` the refs are still null and observing there
 * silently never happens. Nor does a data watcher save it: the ranking is
 * fetched when the map is clicked, which need not be while this is mounting.
 * Keying off the refs is what covers both — it fires exactly when they appear.
 */
watch([pane, detail], () => {
  observer?.disconnect()
  if (!pane.value) return

  observer = new ResizeObserver(onResize)
  observer.observe(pane.value)

  paneH.value = pane.value.clientHeight
  nextTick(renderDetail)
}, { immediate: true, flush: 'post' })

watch([() => props.ranking, () => props.selectedDate, detailHeight], () => {
  if (!hasData.value) {
    chart?.dispose()
    chart = null
    return
  }
  nextTick(renderDetail)
}, { flush: 'post' })

onBeforeUnmount(() => {
  observer?.disconnect()
  observer = null
  if (resizeTimer) clearTimeout(resizeTimer)
  resizeTimer = null
  chart?.off('click', onClick)
  chart?.dispose()
  chart = null
})
</script>
