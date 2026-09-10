<!--
  One line saying what the value on screen is measured AGAINST.

  It exists because the dashboard carries **two baselines that cannot be
  reconciled**, and nothing in either number reveals it:

    * `anom` is a departure from a 1991-2020 daily mean, computed in this project
      from CoralTemp's own daily climatology;
    * `mhw` is a category NOAA assigned against a 1985-2012 mean and 90th
      percentile, over an 11-day window, before we ever see the file.

  So a reader who switches from Anomaly to MHW has changed baseline without
  being told, and the natural assumption — that a big anomaly is roughly a high
  category — is wrong in a way that is invisible. Measured over this box, the
  P90-minus-mean departure a Cat 1 needs is +1.02 degC on average but runs +0.60
  to +1.62 depending on the cell, so no anomaly value corresponds to a category
  anywhere.

  Deliberately inline and always visible rather than in the About dialog alone:
  the About dialog is read once, if ever, and the baseline changes every time
  the variable toggle is pressed. It follows `store.variable`, so it is never
  describing a field that is no longer on screen — the same rule the `series*`
  getters follow.

  `sst` declares no baseline (an absolute temperature is not measured against
  anything), and the whole row disappears rather than printing an empty phrase.
-->
<template>
  <div v-if="baseline" class="flex items-center gap-1.5 px-1 text-xs text-dimmed">
    <span class="truncate">
      <span class="text-muted">{{ label }}</span>
      {{ ' ' }}{{ store.activeBaselinePhrase }}<span
        v-if="baseline.computedBy === 'noaa'"
      >, applied by NOAA</span>
    </span>

    <UPopover :ui="{ content: 'max-w-sm' }">
      <UButton
        icon="i-mdi-information-outline"
        variant="ghost"
        color="neutral"
        size="xs"
        :aria-label="`What ${label} is measured against`"
        @click="trackEvent('baseline_note_opened', { variable: store.variable })"
      />
      <template #content>
        <div class="space-y-2.5 p-3 text-xs text-muted">
          <p v-if="baseline.note">{{ baseline.note }}</p>

          <!-- The comparison between the two, stated wherever the reader opens
               it. Naming only the current variable's baseline would still leave
               someone free to assume the other one shares it. -->
          <p v-if="other" class="text-dimmed">
            <span class="font-medium text-highlighted">Two baselines.</span>
            {{ label }} uses {{ baseline.period }}; {{ otherLabel }} uses
            {{ other.period }}<span v-if="other.windowDays !== baseline.windowDays">, over
              {{ article(other.windowDays) }} {{ other.windowDays }}-day window against
              {{ baseline.windowDays === 1 ? 'a single day' : `${baseline.windowDays} days` }}</span>.
            They are not two views of the same departure, so a given anomaly does
            not correspond to any particular heatwave category.
          </p>

          <!-- The method is not the data provider's, and saying so is the point
               of carrying structured references rather than a sentence. -->
          <p v-if="baseline.references.length" class="text-dimmed">
            <span class="font-medium text-highlighted">Method.</span>
            The marine heatwave definition and its categories are
            <template v-for="(r, i) in baseline.references" :key="r.url">
              <span v-if="i > 0">{{ i === baseline.references.length - 1 ? ' and ' : ', ' }}</span>
              <ULink :to="r.url" target="_blank" class="text-primary" :title="`${r.title}. ${r.source}`">
                {{ r.authors }} {{ r.year }}
              </ULink>
            </template>, applied to CoralTemp by NOAA Coral Reef Watch.
          </p>
        </div>
      </template>
    </UPopover>
  </div>
</template>

<script setup lang="ts">
import { trackEvent } from '~/composables/useAnalytics'
import { useMainStore } from '~/stores/main'

const store = useMainStore()

/**
 * 'a' or 'an' for a number read aloud — "an 11-day window", "a 7-day window".
 * Only 8, 11, 18 and their compounds take 'an' among the values a window width
 * can plausibly be, and those are exactly the ones whose spoken form starts
 * with a vowel.
 */
function article(n: number): string {
  const s = String(n)
  return /^(8|11|18)/.test(s) ? 'an' : 'a'
}

const baseline = computed(() => store.activeBaseline)

/**
 * What the CHART is showing, not what the map is.
 *
 * `seriesLabel` rather than the variable's own name, following the same rule as
 * `seriesStops` and `seriesUnitLabel`: this note sits under the chart, and in
 * region scope the chart plots "MHW extent" — the share of the region's ocean
 * area in a heatwave — rather than a category. The baseline is the same either
 * way (the extent counts cells that exceed the same threshold), but naming the
 * map's field beside the chart's numbers is the drift `series*` exists to stop.
 */
const label = computed(() => store.seriesLabel)

/**
 * The *other* baseline, for the comparison in the popover.
 *
 * Found by looking for a declared baseline whose period differs, rather than by
 * naming `anom` and `mhw`: a fourth variable with a third baseline should show
 * up here without this component being edited, and one that happened to share a
 * baseline should not be contrasted with itself.
 */
const otherEntry = computed(() => {
  const vars = store.domain?.variables ?? {}
  const mine = baseline.value
  if (!mine) return null
  const hit = Object.entries(vars).find(
    ([name, meta]) => name !== store.variable && meta.baseline
      && meta.baseline.period !== mine.period,
  )
  return hit ? { name: hit[0], meta: hit[1] } : null
})

const other = computed(() => otherEntry.value?.meta.baseline ?? null)
const otherLabel = computed(
  () => otherEntry.value?.meta.shortName ?? otherEntry.value?.name ?? '',
)
</script>
