<template>
  <!-- Map and the ranks dock sit side by side rather than one over the other:
       reading a month's ranking is a per-cell question, so the map has to stay
       clickable while the panel is open. It used to be a fullscreen modal, which
       meant close / click / reopen for every cell. -->
  <div class="flex h-full min-h-0">
    <div class="flex min-w-0 grow flex-col">
      <div class="relative grow">
        <AnomalyMap />
        <ColorLegend class="absolute bottom-4 left-1/2 z-10 -translate-x-1/2" />
      </div>

      <div class="relative h-[38%] shrink-0 border-t border-default p-3">
        <TimeseriesChart
          :series="store.pointSeries"
          :loading="store.loadingPoint"
          :empty-message="emptyPointMessage"
          :title="pointTitle"
          :selected-date="store.selectedDate"
          @select="store.setDate($event)"
        />

        <UButton
          class="absolute right-4 top-4 z-20"
          icon="i-mdi-podium-gold"
          size="xs"
          :color="ranksOpen ? 'primary' : 'neutral'"
          :variant="ranksOpen ? 'solid' : 'subtle'"
          label="Monthly ranks"
          :disabled="!store.monthlyRanking && !ranksOpen"
          @click="ranksOpen = !ranksOpen"
        />
      </div>
    </div>

    <SideDock
      v-if="ranksOpen"
      title="Monthly ranks"
      :subtitle="ranksSubtitle"
      storage-key="enso.ranksDock.width"
      @close="ranksOpen = false"
    >
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

      <MonthlyRankingBrowser
        :ranking="store.monthlyRanking"
        :stops="store.domain?.colorStops?.[store.variable] ?? []"
        :loading="store.loadingPoint"
        :empty-message="emptyPointMessage"
        :selected-date="store.selectedDate"
        @select="store.setDate($event)"
      />
    </SideDock>
  </div>
</template>

<script setup lang="ts">
import { useMainStore } from '~/stores/main'

const store = useMainStore()

const periodLabel = computed(
  () => ({ daily: 'daily', weekly: 'weekly mean', monthly: 'monthly mean' })[store.period],
)

const pointTitle = computed(() => {
  const cell = store.pointSeries?.cell
  if (!cell) return ''
  const lon = ((cell.lon + 180) % 360) - 180
  return `${cell.lat.toFixed(3)}°N, ${lon.toFixed(3)}°E · ${periodLabel.value} · click the chart to move the map`
})

const emptyPointMessage = computed(
  () => store.outsideDomain ?? 'Click anywhere on the map to read that cell’s full record.',
)

const ranksOpen = ref(false)
const captionOpen = ref(false)

const ranksSubtitle = computed(() => {
  const cell = store.monthlyRanking?.cell
  if (!cell) return 'Click the map to pick a cell'
  const lon = ((cell.lon + 180) % 360) - 180
  return `${cell.lat.toFixed(3)}°N, ${lon.toFixed(3)}°E`
})

const ranksDescription = computed(() => {
  const span = store.monthlyRanking?.span
  if (!span) return ''
  const top = store.monthlyRanking?.top ?? 10
  // Edge months included, hence the star — the month in progress is ranked
  // alongside the rest rather than waiting for its last day.
  return `Every month from ${span.start.slice(0, 7)} to ${span.end.slice(0, 7)}, `
    + `ranked within its calendar month, warmest first. Pick a month on the left; bars are `
    + `±1 SD of that month's daily values and the top ${top} are bold. A * marks a month `
    + `truncated by the edge of the archive, drawn as an open dot — its mean is over a `
    + `part-month. Click a row to move the map to it.`
})
</script>
