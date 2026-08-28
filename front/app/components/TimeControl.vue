<template>
  <div class="flex items-center gap-2 rounded-lg border border-default bg-elevated/90 px-2 py-1 shadow-lg backdrop-blur">
    <!--
      UFieldGroup, not UButtonGroup: Nuxt UI v4 renamed it and the old name
      resolves to an empty comment node instead of erroring, so the control
      simply vanishes from the DOM.
    -->
    <UFieldGroup size="xs">
      <UButton
        v-for="item in VARIABLES"
        :key="item.value"
        :label="item.label"
        :color="store.variable === item.value ? 'primary' : 'neutral'"
        :variant="store.variable === item.value ? 'solid' : 'subtle'"
        :disabled="!store.variableReady(item.value)"
        :title="store.variableReady(item.value) ? item.title : item.pending"
        @click="store.setVariable(item.value)"
      />
    </UFieldGroup>

    <div class="h-5 w-px bg-accented" />

    <UFieldGroup size="xs">
      <UButton
        v-for="item in PERIODS"
        :key="item.value"
        :label="item.label"
        :color="store.period === item.value ? 'primary' : 'neutral'"
        :variant="store.period === item.value ? 'solid' : 'subtle'"
        @click="store.setPeriod(item.value)"
      />
    </UFieldGroup>

    <div class="h-5 w-px bg-accented" />

    <div class="flex items-center gap-1">
      <UButton
        icon="i-mdi-chevron-double-left"
        variant="ghost"
        color="neutral"
        size="xs"
        :disabled="atStart"
        title="Back one year"
        @click="step(-bucketsPerYear(store.period))"
      />
      <UButton
        icon="i-mdi-chevron-left"
        variant="ghost"
        color="neutral"
        size="xs"
        :disabled="atStart"
        @click="step(-1)"
      />

      <UInput
        v-model="dateInput"
        type="date"
        size="xs"
        :min="store.coverage?.start ?? undefined"
        :max="store.coverage?.end ?? undefined"
        class="w-36"
      />

      <UButton
        icon="i-mdi-chevron-right"
        variant="ghost"
        color="neutral"
        size="xs"
        :disabled="atEnd"
        @click="step(1)"
      />
      <UButton
        icon="i-mdi-chevron-double-right"
        variant="ghost"
        color="neutral"
        size="xs"
        :disabled="atEnd"
        title="Forward one year"
        @click="step(bucketsPerYear(store.period))"
      />
    </div>

    <div class="h-5 w-px bg-accented" />

    <!-- Playback runs until stopped: the playhead is just repeated store.setDate(),
         so stepping or clicking the chart mid-run relocates it rather than fighting
         it, and the end of coverage wraps round to the start. Typing in the date
         field does stop it — otherwise the value being typed into is a moving
         target. -->
    <div class="flex items-center gap-2">
      <UButton
        :icon="playing ? 'i-mdi-pause' : 'i-mdi-play'"
        :color="playing ? 'primary' : 'neutral'"
        :variant="playing ? 'solid' : 'ghost'"
        size="xs"
        :disabled="!canPlay"
        :title="playing ? 'Stop' : 'Play'"
        @click="toggle()"
      />
      <USlider
        v-model="fps"
        :min="MIN_FPS"
        :max="MAX_FPS"
        :step="1"
        size="xs"
        class="w-20"
        :aria-label="`Animation speed, ${fps} frames per second`"
      />
      <span class="w-12 shrink-0 text-xs tabular-nums text-muted">{{ fps }} fps</span>
    </div>

    <!-- The input still picks a day; this is what that day's bucket covers. -->
    <span v-if="store.period !== 'daily' && store.selectedDate" class="pr-1 text-xs text-muted">
      {{ bucketLabel(store.selectedDate, store.period) }}
    </span>
  </div>
</template>

<script setup lang="ts">
import { useMainStore } from '~/stores/main'
import { PERIODS, bucketLabel, bucketStart, bucketsPerYear, shiftBuckets } from '~/utils/periods'
import { MAX_FPS, MIN_FPS, usePlayback } from '~/composables/usePlayback'

const store = useMainStore()

// Anomaly first, and it is what the app opens on: the question this dashboard
// exists for is "how far from normal is it", and absolute SST is the reference
// view you switch to. MHW comes last because it is the narrowest question of the
// three — not "how warm" but "is this officially a heatwave, and how bad" — and
// it is the one most likely to be unavailable, being a separate archive.
//
// `pending` is the tooltip shown while a variable's precondition is unmet. Each
// says what is missing rather than just greying out, because in both cases the
// failure mode without the gate is silent wrong data, not an error — see
// `variableReady` in the store.
const VARIABLES = [
  {
    value: 'anom' as const,
    label: 'Anomaly',
    title: 'Difference from the 1991-2020 daily climatology',
    pending: 'Anomaly needs the full 366-day climatology, which is still loading',
  },
  {
    value: 'sst' as const,
    label: 'SST',
    title: 'Sea surface temperature',
  },
  {
    value: 'mhw' as const,
    label: 'MHW',
    title: 'NOAA marine heatwave category, 1 (Moderate) to 5 (Beyond extreme)',
    pending: 'Marine heatwave needs its own archive, which has not finished ingesting',
  },
]
const { playing, fps, canPlay, toggle, stop } = usePlayback()

const dateInput = computed({
  get: () => store.selectedDate ?? '',
  set: (value: string) => { if (value) { stop(); store.setDate(clamp(value)) } },
})

// Compared bucket-wise: in a weekly or monthly view the first and last buckets
// usually start before / end after the ingested range, so comparing raw dates
// would leave the arrows enabled forever.
const atStart = computed(() => store.selectedDate === snapped(store.coverage?.start))
const atEnd = computed(() => store.selectedDate === snapped(store.coverage?.end))

function snapped(iso: string | null | undefined): string | null {
  return iso ? bucketStart(iso, store.period) : null
}

function clamp(iso: string): string {
  const { start, end } = store.coverage ?? {}
  if (start && iso < start) return start
  if (end && iso > end) return end
  return iso
}

function step(buckets: number) {
  if (!store.selectedDate) return
  store.setDate(clamp(shiftBuckets(store.selectedDate, store.period, buckets)))
}
</script>
