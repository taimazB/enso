<template>
  <div class="flex h-full flex-col">
    <div class="relative grow">
      <AnomalyMap />
      <ColorLegend class="absolute bottom-4 left-1/2 z-10 -translate-x-1/2" />
    </div>

    <div class="h-[38%] shrink-0 border-t border-default p-3">
      <TimeseriesChart
        :series="store.pointSeries"
        :loading="store.loadingPoint"
        :empty-message="emptyPointMessage"
        :title="pointTitle"
        :selected-date="store.selectedDate"
        @select="store.setDate($event)"
      />
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
</script>
