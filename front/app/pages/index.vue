<template>
  <div class="flex h-full flex-col">
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

      <!-- Parked in a fullscreen modal for now: one month of ~45 years, plus a
           rail of the other eleven, needs far more room than the chart rail has.
           Easy to move once we know where it belongs. -->
      <UModal
        v-model:open="ranksOpen"
        fullscreen
        class="absolute right-4 top-4 z-20"
        :title="ranksTitle"
        :description="ranksDescription"
        :ui="{ body: 'flex min-h-0 grow overflow-hidden' }"
      >
        <UButton
          icon="i-mdi-podium-gold"
          size="xs"
          color="neutral"
          variant="subtle"
          label="Monthly ranks"
          :disabled="!store.monthlyRanking"
        />

        <template #body>
          <MonthlyRankingBrowser
            :ranking="store.monthlyRanking"
            :stops="store.domain?.colorStops ?? []"
            :loading="store.loadingPoint"
            :empty-message="emptyPointMessage"
            :selected-date="store.selectedDate"
            @select="store.setDate($event)"
          />
        </template>
      </UModal>
    </div>
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

const ranksTitle = computed(() => {
  const cell = store.monthlyRanking?.cell
  if (!cell) return 'Monthly ranks'
  const lon = ((cell.lon + 180) % 360) - 180
  return `Monthly ranks · ${cell.lat.toFixed(3)}°N, ${lon.toFixed(3)}°E`
})

// Carries the chart's caption as well as its span: the modal header is the only
// part of the dialog that does not scroll, so this is where an explanation of the
// encoding stays visible next to an 1800px figure.
const ranksDescription = computed(() => {
  const span = store.monthlyRanking?.span
  if (!span) return ''
  const top = store.monthlyRanking?.top ?? 10
  // Whole months only, so this stops short of /coverage's end date.
  return `Every complete month from ${span.start.slice(0, 7)} to ${span.end.slice(0, 7)}, `
    + `ranked within its calendar month, warmest first. Pick a month on the left; bars are `
    + `±1 SD of that month's daily values and the top ${top} are bold. Click a row to move `
    + `the map to it.`
})
</script>
