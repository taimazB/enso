<template>
  <!-- The panel sits beside the map, not over it. Reading numbers is a
       per-selection question, so the map has to stay clickable while the panel
       is open — it was a fullscreen modal first, which meant close / click /
       reopen for every cell. It is on the LEFT because the map's own controls
       (projection, legend, time bar) grew on the right and below. -->
  <div class="flex h-full min-h-0">
    <SideDock
      v-if="dockOpen"
      side="left"
      :title="subjectTitle"
      storage-key="enso.dock.width"
      @close="dockOpen = false"
    >
      <template #header>
        <!-- The panel names its subject and nothing more. CHOOSING that subject
             is the map's job — the scope switch sits over the canvas next to the
             projection pair, because picking a cell means clicking the map and
             picking a region means seeing the box move. Duplicating the control
             here would give one decision two homes. -->
        <h2 class="truncate text-sm font-semibold text-highlighted">{{ subjectTitle }}</h2>
        <p class="truncate text-xs text-muted">{{ subjectSubtitle }}</p>
      </template>

      <!-- Tabs choose a VIEW of the current scope, never a second scope: both
           read whatever the switch above names. -->
      <UFieldGroup size="xs" class="mb-2 w-full shrink-0">
        <UButton
          v-for="t in TABS"
          :key="t.value"
          class="grow justify-center"
          :color="tab === t.value ? 'primary' : 'neutral'"
          :variant="tab === t.value ? 'solid' : 'subtle'"
          :icon="t.icon"
          :label="t.label"
          @click="tab = t.value"
        />
      </UFieldGroup>

      <StatsPanel
        v-if="tab === 'numbers'"
        :series="store.activeSeries"
        :loading="store.activeSeriesLoading"
        :empty-message="emptyPointMessage"
        :error="!!store.activeError"
        :stops="store.activeStops"
        :categorical="store.activeIsCategorical"
        :unit="store.activeUnitLabel"
        :precision="store.domain?.variables?.[store.variable]?.precision"
        :signed="store.variable === 'anom'"
        :variable-label="variableLabel"
        :period="store.period"
        :selected-date="store.selectedDate"
      />

      <!-- Ranks are a per-cell question and there is no region equivalent yet,
           so region scope gets an honest empty state that offers the switch
           rather than a disabled tab or, worse, a cell's ranking shown under a
           region's heading. -->
      <div
        v-else-if="store.scope === 'region'"
        class="flex grow flex-col items-center justify-center gap-3 px-6 text-center text-sm text-muted"
      >
        <p>Monthly ranks are per cell — there is no region ranking yet.</p>
        <UButton size="xs" color="neutral" variant="subtle" icon="i-mdi-map-marker" label="Show the clicked cell" @click="store.setScope('point')" />
      </div>

      <template v-else>
        <!-- Clamped, because the detail panel wants every pixel of height it can
             get; the whole caption is one click away for anyone who needs it. -->
        <button
          v-if="ranksDescription"
          type="button"
          class="mb-2 shrink-0 cursor-pointer text-left text-xs leading-snug text-muted hover:text-default"
          :class="captionOpen ? '' : 'line-clamp-2'"
          :title="captionOpen ? 'Show less' : 'Show more'"
          @click="captionOpen = !captionOpen"
        >
          {{ ranksDescription }}
        </button>

        <!-- `min-h-0 grow` rather than the component's own height: the caption
             above it is a sibling of variable height, so the plot has to take
             what is left over rather than a fixed share of the dock. -->
        <MonthlyRankingBrowser
          class="min-h-0 grow"
          :ranking="store.monthlyRanking"
          :stops="store.activeStops"
          :loading="store.loadingPoint"
          :empty-message="emptyPointMessage"
          :error="!!store.activeError"
          :selected-date="store.selectedDate"
          :unit="store.activeUnitLabel"
          :categorical="store.activeIsCategorical"
          :precision="store.domain?.variables?.[store.variable]?.precision"
          :rank-order="rankOrder"
          @select="store.setDate($event)"
        />
      </template>
    </SideDock>

    <!-- Closed, the dock leaves a strip rather than vanishing: a panel with no
         visible way back is a panel users do not find twice. -->
    <button
      v-else
      type="button"
      class="flex w-7 shrink-0 cursor-pointer flex-col items-center gap-2 border-r border-default bg-default py-3 text-muted transition-colors hover:text-default"
      title="Show the panel"
      @click="dockOpen = true"
    >
      <UIcon name="i-mdi-chevron-right" class="size-4" />
      <span class="text-xs [writing-mode:vertical-rl]">Panel</span>
    </button>

    <div class="flex min-w-0 grow flex-col">
      <div class="relative grow">
        <AnomalyMap />
        <ColorLegend class="absolute bottom-4 left-1/2 z-10 -translate-x-1/2" />
      </div>

      <div class="relative h-[38%] shrink-0 border-t border-default p-3">
        <TimeseriesChart
          :series="store.activeSeries"
          :loading="store.activeSeriesLoading"
          :empty-message="emptyPointMessage"
          :error="!!store.activeError"
          :title="chartTitle"
          :selected-date="store.selectedDate"
          :stops="store.activeStops"
          :zero-line="store.variable === 'anom'"
          :unit="store.activeUnitLabel"
          :categorical="store.activeIsCategorical"
          @select="store.setDate($event)"
        />
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { useMainStore } from '~/stores/main'

const store = useMainStore()

const TABS = [
  { value: 'numbers' as const, label: 'Numbers', icon: 'i-mdi-numeric' },
  { value: 'ranks' as const, label: 'Monthly ranks', icon: 'i-mdi-podium-gold' },
]

/**
 * Opens on Numbers, and open. The panel needs no selection to have something to
 * say — the app lands in region scope for exactly that reason — so there is
 * nothing to be gained by making the first thing a user sees an empty dock.
 */
const tab = ref<'numbers' | 'ranks'>('numbers')
const dockOpen = ref(true)
const captionOpen = ref(false)

const periodLabel = computed(
  () => ({ daily: 'daily', weekly: 'weekly mean', monthly: 'monthly mean' })[store.period],
)

/**
 * `shortName`, not `longName`. domain.yml's long names all begin "Daily ...",
 * which is the *source's* cadence and collides with the period the panel is
 * actually showing — "Daily sea surface temperature anomaly - 24 Aug to 30 Aug"
 * invites reading a weekly mean as a single day.
 */
const variableLabel = computed(() => {
  const meta = store.domain?.variables?.[store.variable]
  return meta?.shortName || meta?.longName || store.variable
})

/**
 * A cell as hemispheres, not signed degrees.
 *
 * Cells come back on the 0-360 convention the database stores, so most of this
 * box's longitudes are above 180 and have to be unwrapped for display. Printing
 * the signed result as "°E" gave `-168.125°E`, which is a compass direction
 * contradicting its own sign.
 */
function formatCell(cell: { lat: number, lon: number } | undefined): string {
  if (!cell) return ''
  const lon = ((cell.lon + 180) % 360) - 180
  return `${Math.abs(cell.lat).toFixed(2)}°${cell.lat < 0 ? 'S' : 'N'}, `
    + `${Math.abs(lon).toFixed(2)}°${lon < 0 ? 'W' : 'E'}`
}

/**
 * What the panel is describing, said in the panel's own header.
 *
 * The scope switch is on the map now, so the header is a label rather than a
 * control — but it still has to be there: the dock can be dragged wide enough
 * that its numbers sit a long way from the button that chose them.
 */
const subjectTitle = computed(() => (store.scope === 'region'
  ? store.activeRegionMeta?.label ?? 'Region'
  : formatCell(store.pointSeries?.cell) || 'No cell selected'))

const subjectSubtitle = computed(() => (store.scope === 'region'
  ? 'Area mean over the region'
  : store.pointSeries?.cell ? 'Nearest grid cell' : 'Click the map to pick one'))

const pointTitle = computed(() => {
  const cell = formatCell(store.pointSeries?.cell)
  if (!cell) return ''
  return `${cell} · ${periodLabel.value} · click the chart to move the map`
})

/**
 * The region's own caption. No cell coordinates and no "click the chart" hint —
 * clicking still moves the map, but a region series is read for its shape over
 * time rather than for locating a place, and the box on the map already says
 * where it is.
 */
const regionTitle = computed(() => {
  const label = store.activeRegionMeta?.label
  if (!label) return ''
  return `${label} · area mean · ${periodLabel.value} · click the chart to move the map`
})

const chartTitle = computed(() =>
  store.scope === 'region' ? regionTitle.value : pointTitle.value,
)

/**
 * What the three panels say when they have no series to draw, in priority
 * order: a failure first, then the informational out-of-box case, then the
 * hint. `activeError` is scope-aware, so a failed region load reads as one too.
 */
const emptyPointMessage = computed(
  () => store.activeError
    ?? store.outsideDomain
    ?? 'Click anywhere on the map to read that cell’s full record.',
)

/**
 * What rank 1 means for the active variable. "Warmest" is right for a
 * temperature and wrong for a heatwave category, where the ranking is by mean
 * severity — nothing about it is a temperature.
 */
const rankOrder = computed(() => (store.variable === 'mhw' ? 'most severe' : 'warmest'))

const ranksDescription = computed(() => {
  const span = store.monthlyRanking?.span
  if (!span) return ''
  const top = store.monthlyRanking?.top ?? 10
  // Edge months included, hence the star — the month in progress is ranked
  // alongside the rest rather than waiting for its last day.
  return `Every month from ${span.start.slice(0, 7)} to ${span.end.slice(0, 7)}, `
    + `ranked within its calendar month, ${rankOrder.value} first. `
    + (store.variable === 'mhw'
      ? 'A month is scored by its MEAN daily heatwave category, not its worst day — '
      + 'a max would put most years on Cat 1 and rank nothing. '
      : '')
    + `Pick a month on the left; bars are `
    + `±1 SD of that month's daily values and the top ${top} are bold. A * marks a month `
    + `truncated by the edge of the archive, drawn as an open dot — its mean is over a `
    + `part-month. Click a row to move the map to it.`
})
</script>
