<template>
  <!-- `@container`: this now lives in a dock the user can drag narrower or
       wider, so what fits is a question about this element, not the viewport. -->
  <div class="@container flex size-full min-h-0 flex-col">
    <div
      v-if="!hasData"
      class="flex min-h-48 w-full items-center justify-center px-6 text-center text-sm text-muted"
    >
      <UIcon v-if="loading" name="i-mdi-loading" class="size-5 animate-spin" />
      <span v-else>{{ emptyMessage }}</span>
    </div>

    <ClientOnly v-else>
      <div class="flex size-full min-h-0 gap-4">
        <!-- The rail: twelve months at a glance, one of them open on the right.
             Names are HTML rather than canvas text so they stay crisp, selectable
             and focusable, and the thumbnail beside each is the same spine the
             detail draws — the rail is scanned for shape and colour, not read. -->
        <nav
          ref="rail"
          class="w-40 shrink-0 space-y-1 overflow-y-auto pr-1 @md:w-56"
          aria-label="Calendar months"
        >
          <button
            v-for="(rows, i) in months"
            :key="i"
            :ref="el => setCard(i, el as HTMLElement | null)"
            type="button"
            class="w-full rounded-md border px-2 pb-1 pt-1.5 text-left transition-colors"
            :class="activeMonth === i + 1
              ? 'border-accented bg-elevated'
              : 'border-transparent hover:bg-elevated/60'"
            :aria-current="activeMonth === i + 1 ? 'true' : undefined"
            :disabled="!rows.length"
            @click="activeMonth = i + 1"
          >
            <div class="flex items-baseline gap-2">
              <span
                class="text-sm font-semibold"
                :class="activeMonth === i + 1 ? 'text-highlighted' : 'text-default'"
                :style="mapMonth === i + 1 ? { color: ACCENT } : undefined"
              >{{ MONTHS[i] }}</span>
              <span v-if="rows.length" class="ml-auto hidden text-[10px] tabular-nums text-dimmed @lg:inline">
                warmest {{ rows[0]!.year }}<template v-if="rows[0]!.partial">&nbsp;*</template>
              </span>
            </div>
            <div :ref="el => setThumb(i, el as HTMLElement | null)" class="h-14 w-full" />
          </button>
        </nav>

        <!-- The open month. Sized to the pane, so it normally needs no scrolling
             at all; only a very short pane pushes the pitch to its floor and lets
             this overflow. -->
        <section class="flex min-w-0 grow flex-col">
          <div class="mb-1 flex shrink-0 items-center gap-3">
            <h3 class="text-base font-semibold text-highlighted">{{ MONTHS[activeMonth - 1] }}</h3>
            <span class="truncate text-xs text-muted">
              {{ activeRows.length }} years ranked, warmest first
            </span>
            <ColorLegend class="ml-auto hidden @lg:block" />
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
      </div>
    </ClientOnly>
  </div>
</template>

<script setup lang="ts">
import * as echarts from 'echarts'
import type { ColorStop } from '~/utils/colorScale'
import type { MonthlyRanking } from '~/utils/ranking'
import {
  ACCENT,
  MONTHS,
  detailHeightFor,
  detailOption,
  detailPitch,
  monthsOf,
  partialNote,
  thumbOption,
  xDomainOf,
} from '~/utils/ranking'

const props = defineProps<{
  ranking: MonthlyRanking | null
  /** `/domain`'s diverging stops, so a dot here is the colour that cell has on the map. */
  stops: ColorStop[]
  loading?: boolean
  emptyMessage?: string
  /** Bucket on the map. Its year is ringed amber, and its month opens first. */
  selectedDate?: string | null
}>()

const emit = defineEmits<{ select: [date: string] }>()

const months = computed(() => monthsOf(props.ranking))
const hasData = computed(() => months.value.some(rows => rows.length > 0))
/** One domain across all twelve, so the rail's spines are comparable. */
const railDomain = computed(() => xDomainOf(months.value))
const topN = computed(() => props.ranking?.top ?? 10)
const selectedYear = computed(() => (props.selectedDate ? Number(props.selectedDate.slice(0, 4)) : null))
/** The month the map is on — accented in the rail, whether or not it is open. */
const mapMonth = computed(() => (props.selectedDate ? Number(props.selectedDate.slice(5, 7)) : null))

/**
 * Which month the detail pane shows. Seeded from the map and re-seeded whenever
 * the map moves to another month, but *not* bound to it: picking a month here
 * only changes what is on screen, it must not drag the map somewhere else.
 */
const activeMonth = ref(mapMonth.value ?? 1)
watch(mapMonth, (m) => { if (m) activeMonth.value = m })

const activeRows = computed(() => months.value[activeMonth.value - 1] ?? [])
/**
 * The open month scales to itself.
 *
 * With twelve panels on screen at once a shared domain was the only way a bar's
 * length could mean the same thing everywhere; now the rail does that job, and
 * the detail is where one month is *read*. At a cell whose August reaches +6 °C,
 * a shared domain leaves January using a third of the pane.
 */
const detailDomain = computed(() => xDomainOf([activeRows.value]))
/** Spells out the `*` on a truncated edge month, when the open month has one. */
const note = computed(() => partialNote(activeRows.value, activeMonth.value))

// --- Rail --------------------------------------------------------------------

const rail = ref<HTMLElement | null>(null)
const cards = ref<(HTMLElement | null)[]>([])
const thumbEls = ref<(HTMLElement | null)[]>([])
const thumbs: (echarts.ECharts | null)[] = Array.from({ length: 12 }, () => null)

function setCard(i: number, el: HTMLElement | null) { cards.value[i] = el }
function setThumb(i: number, el: HTMLElement | null) { thumbEls.value[i] = el }

/** Reactive to the refs appearing, without deep-traversing DOM nodes. */
const thumbCount = computed(() => thumbEls.value.filter(Boolean).length)

function renderThumbs() {
  thumbEls.value.forEach((el, i) => {
    const rows = months.value[i] ?? []
    if (!el || !el.clientWidth || !rows.length) return
    thumbs[i] ??= echarts.init(el, null, { renderer: 'canvas' })
    thumbs[i]!.resize()
    thumbs[i]!.setOption(
      thumbOption({ rows, stops: props.stops, domain: railDomain.value, selectedYear: selectedYear.value }),
      { notMerge: true },
    )
  })
}

// --- Detail ------------------------------------------------------------------

const pane = ref<HTMLElement | null>(null)
const detail = ref<HTMLElement | null>(null)
let chart: echarts.ECharts | null = null
/** Measured height of the scroll pane; the row pitch is spent out of it. */
const paneH = ref(0)

const pitch = computed(() => detailPitch(activeRows.value.length, paneH.value || 640))
const detailHeight = computed(() => detailHeightFor(activeRows.value.length, pitch.value))

function onClick(params: { componentType?: string, value?: unknown }) {
  if (params.componentType !== 'series') return
  const year = (params.value as number[])[4]
  if (year == null) return
  emit('select', `${year}-${String(activeMonth.value).padStart(2, '0')}-01`)
}

function renderDetail() {
  const el = detail.value
  if (!el || !el.clientWidth || !activeRows.value.length) return
  if (!chart) {
    chart = echarts.init(el, null, { renderer: 'canvas' })
    chart.on('click', onClick)
  }
  chart.resize()
  chart.setOption(
    detailOption({
      rows: activeRows.value,
      month: activeMonth.value,
      stops: props.stops,
      domain: detailDomain.value,
      pitch: pitch.value,
      topN: topN.value,
      selectedYear: selectedYear.value,
    }),
    { notMerge: true },
  )
}

// --- Lifecycle ---------------------------------------------------------------

let observer: ResizeObserver | null = null
let resizeTimer: ReturnType<typeof setTimeout> | null = null

/**
 * Coalesced, because the dock's drag handle fires this on every frame and one
 * redraw is twelve thumbnails plus a 45-row panel. 100ms is invisible when the
 * panel merely opens and is the difference between smooth and gluey on a drag.
 */
function onResize() {
  if (resizeTimer) clearTimeout(resizeTimer)
  resizeTimer = setTimeout(() => {
    resizeTimer = null
    paneH.value = pane.value?.clientHeight ?? 0
    renderThumbs()
    nextTick(renderDetail)
  }, 100)
}

/**
 * Watch the elements, not the mount.
 *
 * Everything here sits inside `<ClientOnly>`, which renders nothing until after
 * hydration, so at `onMounted` the refs are still null and observing there
 * silently never happens. Nor does a data watcher save it: the ranking is
 * fetched when the map is clicked, long before the modal opens, so
 * `props.ranking` never changes while this component is mounting. Keying off the
 * refs is what covers both — it fires exactly when the elements appear.
 */
watch([rail, pane, detail, thumbCount], () => {
  observer?.disconnect()
  if (!pane.value && !rail.value) return

  observer = new ResizeObserver(onResize)
  if (pane.value) observer.observe(pane.value)
  if (rail.value) observer.observe(rail.value)

  paneH.value = pane.value?.clientHeight ?? 0
  renderThumbs()
  nextTick(renderDetail)
}, { immediate: true, flush: 'post' })

watch([() => props.ranking, () => props.selectedDate], () => {
  if (!hasData.value) {
    thumbs.forEach((c, i) => { c?.dispose(); thumbs[i] = null })
    chart?.dispose()
    chart = null
    return
  }
  nextTick(() => { renderThumbs(); renderDetail() })
})

// A month swap redraws only the detail; the rail's twelve are unchanged.
watch([activeMonth, detailHeight], () => {
  nextTick(renderDetail)
  cards.value[activeMonth.value - 1]?.scrollIntoView({ block: 'nearest' })
}, { flush: 'post' })

onBeforeUnmount(() => {
  observer?.disconnect()
  observer = null
  if (resizeTimer) clearTimeout(resizeTimer)
  resizeTimer = null
  thumbs.forEach((c, i) => { c?.dispose(); thumbs[i] = null })
  chart?.off('click', onClick)
  chart?.dispose()
  chart = null
})
</script>
