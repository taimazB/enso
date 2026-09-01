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

      <!-- Numbers and the monthly ranking are one view, not two: the tab pair
           that used to switch between them made two halves of the same answer
           about the same cell take turns. The ranking is the calendar month the
           map is on — the rail of twelve is gone, since the numbers above
           already say which bucket is being described. -->
      <div class="flex min-h-0 grow flex-col gap-3 overflow-y-auto">
        <StatsPanel
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

        <!-- One panel, both scopes. A region's ranking is the same question over
             a different daily series — the API defines the ranking once and
             feeds it either a cell's record or `region_daily`'s area means — so
             this is `activeRanking`, not a second component.

             `min-h-0 grow`: the numbers above take their natural height, and the
             plot is spent out of whatever the dock has left — `detailPitch()`
             measures this pane, so 45 years normally fit without scrolling. -->
        <MonthlyRankPanel
          v-if="store.activeRanking || store.activeSeriesLoading"
          class="min-h-0 grow border-t border-default pt-3"
          :ranking="store.activeRanking"
          :stops="store.activeStops"
          :loading="store.activeSeriesLoading"
          :empty-message="emptyPointMessage"
          :error="!!store.activeError"
          :selected-date="store.selectedDate"
          :unit="store.activeUnitLabel"
          :categorical="store.activeIsCategorical"
          :zero-line="store.variable === 'anom'"
          :precision="store.domain?.variables?.[store.variable]?.precision"
          :rank-order="rankOrder"
          :period="store.period"
          @select="store.setDate($event)"
        />
      </div>

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

/**
 * Open. The panel needs no selection to have something to say — the app lands in
 * region scope for exactly that reason — so there is nothing to be gained by
 * making the first thing a user sees a closed dock.
 */
const dockOpen = ref(true)

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

</script>
