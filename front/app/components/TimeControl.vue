<template>
  <div class="flex items-center gap-2 rounded-lg border border-default bg-elevated/90 px-2 py-1 shadow-lg backdrop-blur">
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

    <!-- The input still picks a day; this is what that day's bucket covers. -->
    <span v-if="store.period !== 'daily' && store.selectedDate" class="pr-1 text-xs text-muted">
      {{ bucketLabel(store.selectedDate, store.period) }}
    </span>
  </div>
</template>

<script setup lang="ts">
import { useMainStore } from '~/stores/main'
import { PERIODS, bucketLabel, bucketStart, bucketsPerYear, shiftBuckets } from '~/utils/periods'

// Note: the period toggle above is a UFieldGroup, not a UButtonGroup — Nuxt UI
// v4 renamed that component, and the old name silently renders nothing.

const store = useMainStore()

const dateInput = computed({
  get: () => store.selectedDate ?? '',
  set: (value: string) => { if (value) store.setDate(clamp(value)) },
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
