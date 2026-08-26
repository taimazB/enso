<template>
  <div
    v-if="stops.length"
    class="rounded-lg border border-default bg-elevated/90 px-3 py-2 shadow-lg backdrop-blur"
  >
    <div class="mb-1 text-[11px] font-medium text-muted">{{ title }}</div>
    <div class="h-2.5 w-48 rounded" :style="{ background: gradient }" />
    <div class="mt-1 flex justify-between text-[11px] text-muted">
      <span v-for="tick in ticks" :key="tick">{{ formatTick(tick) }}</span>
    </div>
    <!--
      Only the anomaly has a third state. SST is defined on every ocean cell in
      the box, but the 1991-2020 climatology stops at the seasonal ice edge, so
      about 3% of the ocean has a temperature and no anomaly. Those cells are
      flat grey on the map, which needs saying — otherwise they read as land.
    -->
    <div
      v-if="store.variable === 'anom' && store.domain?.noClimColor"
      class="mt-1.5 flex items-center gap-1.5 text-[11px] text-muted"
    >
      <span
        class="h-2.5 w-2.5 rounded-sm border border-default"
        :style="{ background: store.domain.noClimColor }"
      />
      <span>no climatology</span>
    </div>
  </div>
</template>

<script setup lang="ts">
import { useMainStore } from '~/stores/main'

const store = useMainStore()

/** The active variable's stops — sst is sequential, anom diverging. */
const stops = computed(() => store.domain?.colorStops?.[store.variable] ?? [])
const meta = computed(() => store.domain?.variables?.[store.variable])

const title = computed(() => {
  const v = meta.value
  return v ? `${v.shortName} (°${v.units === 'degC' ? 'C' : v.units})` : ''
})

const gradient = computed(() => {
  const list = stops.value
  if (!list.length) return ''
  const parts = list.map((s, i) => `${s.color} ${((i / (list.length - 1)) * 100).toFixed(1)}%`)
  return `linear-gradient(to right, ${parts.join(', ')})`
})

const ticks = computed(() => {
  const v = meta.value
  if (!v) return []
  const mid = (v.vmin + v.vmax) / 2
  return [v.vmin, (v.vmin + mid) / 2, mid, (mid + v.vmax) / 2, v.vmax]
})

/**
 * A signed `+` only makes sense on a scale centred at zero. SST runs -2..32,
 * where "+17" would be noise; the anomaly runs -3..+3, where the sign is the
 * whole point.
 */
function formatTick(tick: number): string {
  const rounded = Math.round(tick * 10) / 10
  const signed = store.variable === 'anom' && rounded > 0
  return `${signed ? '+' : ''}${rounded}`
}
</script>
