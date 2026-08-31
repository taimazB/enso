<template>
  <div
    v-if="stops.length"
    class="rounded-lg border border-default bg-elevated/90 px-3 py-2 shadow-lg backdrop-blur"
  >
    <div class="mb-1 flex items-center gap-1.5">
      <span class="text-[11px] font-medium text-muted">{{ title }}</span>
      <!-- Marks a range that is not domain.yml's, so a map read at ±1 is never
           mistaken for one read at the default ±3. -->
      <span v-if="isCustom && !categorical" class="text-[11px] text-primary">custom</span>
    </div>

    <!--
      A categorical scale is a key, not a ramp. Its five classes have names, and
      the names are the point — "Cat 3" means Severe, which is what the map is
      being read for. There is also nothing here to re-range: the classes are the
      values, so the popover, the slider and the tick row are all absent rather
      than disabled.
    -->
    <div v-if="categorical" class="flex w-48 flex-col gap-0.5">
      <div
        v-for="stop in stops"
        :key="stop.value"
        class="flex items-center gap-1.5 text-[11px] text-muted"
      >
        <span
          class="h-2.5 w-4 shrink-0 rounded-sm"
          :style="{ background: stop.color }"
        />
        <span class="tabular-nums text-default">{{ stop.value }}</span>
        <span>{{ stop.label }}</span>
      </div>
    </div>

    <UPopover v-else :content="{ side: 'top', align: 'center' }">
      <!--
        A real button, not the bar with a click handler: this is the only way to
        reach the range control, so it has to be focusable and it has to say what
        it does.
      -->
      <button
        type="button"
        class="block w-48 cursor-pointer rounded ring-offset-2 ring-offset-elevated focus:outline-none focus-visible:ring-2 focus-visible:ring-primary"
        :aria-label="`Adjust the ${meta?.shortName ?? ''} colour range`"
        title="Adjust the colour range"
      >
        <span class="block h-2.5 w-full rounded" :style="{ background: gradient }" />
        <span class="mt-1 flex justify-between text-[11px] text-muted">
          <span v-for="(tick, i) in ticks" :key="i">{{ formatTick(tick) }}</span>
        </span>
      </button>

      <template #content>
        <div class="w-64 p-3">
          <div class="mb-2 flex items-center justify-between">
            <span class="text-xs font-medium">Colour range</span>
            <UButton
              v-if="isCustom"
              label="Reset"
              size="xs"
              variant="subtle"
              color="neutral"
              @click="store.resetScale(store.variable)"
            />
          </div>

          <!--
            The images carry data rather than colour, so this is a paint change:
            no frame is refetched and no cached image is invalidated. That is
            what makes a slider — which fires on every drag frame — affordable.
          -->
          <USlider
            :model-value="[scale.vmin, scale.vmax]"
            :min="bounds.vmin"
            :max="bounds.vmax"
            :step="step"
            size="xs"
            class="my-3"
            aria-label="Colour range"
            @update:model-value="onSlide"
          />

          <div class="flex items-center gap-2">
            <UInput
              :model-value="scale.vmin"
              type="number"
              size="xs"
              class="w-full"
              :step="step"
              :min="bounds.vmin"
              :max="bounds.vmax"
              aria-label="Range minimum"
              @update:model-value="commit($event, scale.vmax)"
            />
            <span class="text-xs text-muted">to</span>
            <UInput
              :model-value="scale.vmax"
              type="number"
              size="xs"
              class="w-full"
              :step="step"
              :min="bounds.vmin"
              :max="bounds.vmax"
              aria-label="Range maximum"
              @update:model-value="commit(scale.vmin, $event)"
            />
          </div>

          <p class="mt-2 text-[11px] text-muted">
            Default {{ formatTick(defaults.vmin) }} to {{ formatTick(defaults.vmax) }}.
            Limits {{ formatTick(bounds.vmin) }} to {{ formatTick(bounds.vmax) }}.
          </p>
        </div>
      </template>
    </UPopover>

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

/**
 * The active variable's stops, spread over the range in force — sst is
 * sequential, anom diverging. The colours are the server's; only the value each
 * one sits at follows the user's range.
 */
const stops = computed(() => store.activeStops)
const meta = computed(() => store.domain?.variables?.[store.variable])
const categorical = computed(() => store.activeIsCategorical)

const scale = computed(() => store.activeScale)
const bounds = computed(() => store.scaleBoundsFor(store.variable))
const isCustom = computed(() => store.scaleIsCustom(store.variable))
/** domain.yml's range — what Reset returns to. */
const defaults = computed(() => ({ vmin: meta.value?.vmin ?? 0, vmax: meta.value?.vmax ?? 1 }))
/**
 * Coarser than the encoding on purpose — see `rangeStep` in the store. The
 * slider and both number fields share it, so a value typed in can never sit off
 * the slider's grid and get silently snapped when the popover re-renders.
 */
const step = computed(() => store.scaleStepFor(store.variable))

const title = computed(() => {
  const v = meta.value
  if (!v) return ''
  // The degree sign belongs to a temperature, not to a category. `mhw` reads
  // "MHW (category)"; the old expression produced "(°category)".
  const unit = store.unitLabelFor(store.variable) || v.units
  return `${v.shortName} (${unit})`
})

/**
 * Positions the stops by index, not by value — which stays correct because
 * `stopsFor` re-labels an evenly spaced ramp and keeps it evenly spaced. A
 * non-uniform stop list would need positioning by value instead.
 */
const gradient = computed(() => {
  const list = stops.value
  if (!list.length) return ''
  const parts = list.map((s, i) => `${s.color} ${((i / (list.length - 1)) * 100).toFixed(1)}%`)
  return `linear-gradient(to right, ${parts.join(', ')})`
})

const ticks = computed(() => {
  const { vmin, vmax } = scale.value
  const mid = (vmin + vmax) / 2
  return [vmin, (vmin + mid) / 2, mid, (mid + vmax) / 2, vmax]
})

/** All clamping lives in the store, so slider and keyboard agree exactly. */
function commit(vmin: unknown, vmax: unknown) {
  store.setScale(store.variable, Number(vmin), Number(vmax))
}

function onSlide(value: number | number[] | undefined) {
  if (Array.isArray(value)) commit(value[0], value[1])
}

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

// localStorage is browser-only, and this component mounts after /domain has
// landed — so the remembered ranges are clamped against a known encoding.
onMounted(() => store.loadScales())
</script>
