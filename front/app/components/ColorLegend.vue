<template>
  <div
    v-if="stops.length"
    class="rounded-lg border border-default bg-elevated/90 px-3 py-2 shadow-lg backdrop-blur"
  >
    <div class="mb-1 text-[11px] font-medium text-muted">SST anomaly (°C)</div>
    <div class="h-2.5 w-48 rounded" :style="{ background: gradient }" />
    <div class="mt-1 flex justify-between text-[11px] text-muted">
      <span v-for="tick in ticks" :key="tick">{{ tick > 0 ? `+${tick}` : tick }}</span>
    </div>
  </div>
</template>

<script setup lang="ts">
import { useMainStore } from '~/stores/main'

const store = useMainStore()
const stops = computed(() => store.domain?.colorStops ?? [])

const gradient = computed(() => {
  const list = stops.value
  if (!list.length) return ''
  const parts = list.map((s, i) => `${s.color} ${((i / (list.length - 1)) * 100).toFixed(1)}%`)
  return `linear-gradient(to right, ${parts.join(', ')})`
})

const ticks = computed(() => {
  const v = store.domain?.variables?.anom
  if (!v) return []
  return [v.vmin, v.vmin / 2, 0, v.vmax / 2, v.vmax]
})
</script>
