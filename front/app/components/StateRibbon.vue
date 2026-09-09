<template>
  <!-- The answer to the question a first-time visitor arrives with, before they
       have touched a control. Everything else on the page answers a question
       they have already framed — this cell, that region, this date — and none of
       it says whether anything is happening out there right now.

       A strip under the header rather than a card in the dock: it describes the
       whole basin, not the current selection, so it must not move when the
       selection does. Both halves are buttons, because the natural next gesture
       after reading a finding is to look at it, and the thing they select is
       exactly the thing the sentence is about. -->
  <div
    v-if="enso || heatwave"
    class="flex flex-wrap items-center gap-x-5 gap-y-1.5 border-b border-default bg-elevated/40 px-4 py-1.5 text-xs"
  >
    <button
      v-if="enso"
      type="button"
      class="group flex cursor-pointer items-center gap-2 text-left"
      :title="`Show ${enso.label} — ${ensoTitle}`"
      @click="showEnso()"
    >
      <span
        class="rounded px-1.5 py-0.5 text-[11px] font-semibold uppercase tracking-wide"
        :class="phaseClass"
      >{{ phaseWord }}</span>
      <span class="text-muted group-hover:text-default">
        <!-- The month leads, not the season: it is the most recent thing the
             archive knows and the number the rank is about. The ONI-style
             season index is the formal basis for the phase word beside it, and
             is said second. -->
        <span class="font-medium text-highlighted tabular-nums">{{ signed(enso.latestMonth.value) }}{{ unit }}</span>
        in {{ enso.label }} for {{ monthName }}<span v-if="rankPhrase">, {{ rankPhrase }}</span>
      </span>
    </button>

    <button
      v-if="heatwave"
      type="button"
      class="group flex cursor-pointer items-center gap-2 text-left"
      :title="`Show marine heatwave extent over the ${heatwave.label.toLowerCase()}`"
      @click="showHeatwave()"
    >
      <span
        class="rounded bg-orange-500/15 px-1.5 py-0.5 text-[11px] font-semibold uppercase tracking-wide text-orange-400"
      >Heatwave</span>
      <span class="text-muted group-hover:text-default">
        <span class="font-medium text-highlighted tabular-nums">{{ heatwave.extent }}%</span>
        of the Pacific<span v-if="extentPhrase">, {{ extentPhrase }}</span>
      </span>
    </button>

    <div class="ml-auto flex items-center gap-2 text-dimmed">
      <span v-if="asOf" class="tabular-nums">as of {{ asOf }}</span>

      <!-- On demand, not always on. Both sentences above are written to be read
           without it; this is where the caveats that would otherwise clutter
           them live — chiefly that the index is not NOAA's ONI, which is the one
           thing here that would be wrong to leave unsaid. -->
      <UPopover :ui="{ content: 'max-w-sm' }">
        <UButton
          icon="i-mdi-information-outline"
          variant="ghost"
          color="neutral"
          size="xs"
          aria-label="How these two numbers are calculated"
          @click="trackEvent('state_guide_opened', {})"
        />
        <template #content>
          <div class="space-y-2.5 p-3 text-xs text-muted">
            <p v-if="enso">
              <span class="font-medium text-highlighted">ENSO.</span>
              {{ enso.label }} is the mean sea surface temperature over
              5°S–5°N, 170°W–120°W. The phase follows the three-month running mean
              of its anomaly — <span class="tabular-nums">{{ signed(enso.index) }}{{ unit }}</span>
              for {{ enso.season }} — against NOAA's ±{{ enso.threshold }}{{ unit }} threshold.
              {{ enso.seasons }} consecutive season<span v-if="enso.seasons !== 1">s</span> so far;
              NOAA calls five in a row an episode.
            </p>
            <p v-if="enso" class="text-dimmed">
              This is not NOAA's official ONI. That index uses a base period that
              shifts every five years; this archive has one fixed
              {{ enso.baseline }} climatology, so the value here runs warmer than
              the official one and the two will not agree to the tenth.
            </p>
            <p v-if="heatwave">
              <span class="font-medium text-highlighted">Heatwave extent.</span>
              The share of this domain's ocean <em>area</em> at NOAA marine
              heatwave category 1 or above, area-weighted by latitude. Normal is
              the {{ heatwave.baseline }} mean for this time of year, over a
              ±{{ heatwave.windowDays }}-day window.
            </p>
          </div>
        </template>
      </UPopover>
    </div>
  </div>
</template>

<script setup lang="ts">
import { trackEvent } from '~/composables/useAnalytics'
import { useMainStore } from '~/stores/main'

const store = useMainStore()

const enso = computed(() => store.pacific?.enso ?? null)
const heatwave = computed(() => store.pacific?.heatwave ?? null)

const unit = '°C'

/** The date both findings are as of — they share one, the archive's last day. */
const asOf = computed(() => {
  const iso = heatwave.value?.date ?? store.coverage?.end
  if (!iso) return ''
  // 'en-GB' pinned, not the visitor's locale, and for a reason that only shows
  // up under SSR: this line is rendered on the server too, where Node's default
  // locale is whatever the container has, and a client that formats it
  // differently is a hydration mismatch. `utils/periods.ts` pins the same one.
  return new Date(`${iso}T00:00:00Z`).toLocaleDateString('en-GB', {
    day: 'numeric', month: 'short', year: 'numeric', timeZone: 'UTC',
  })
})

const PHASE_WORDS: Record<string, string> = {
  el_nino: 'El Niño',
  la_nina: 'La Niña',
  neutral: 'Neutral',
}

/**
 * The phase, with its strength where there is one.
 *
 * NOAA's five-season rule is what separates an *episode* from *conditions*, and
 * the badge honours it: below five seasons this says "El Niño conditions"
 * rather than declaring an event that has not met the definition yet. Neutral
 * takes no strength — a "weak neutral" is not a thing.
 */
const phaseWord = computed(() => {
  const e = enso.value
  if (!e) return ''
  const name = PHASE_WORDS[e.phase] ?? e.phase
  if (e.phase === 'neutral') return name
  return e.episode ? `${e.strength} ${name}` : `${name} conditions`
})

const phaseClass = computed(() => ({
  el_nino: 'bg-red-500/15 text-red-400',
  la_nina: 'bg-sky-500/15 text-sky-400',
  neutral: 'bg-elevated text-muted',
}[enso.value?.phase ?? 'neutral']))

const monthName = computed(() => {
  const iso = enso.value?.latestMonth.month
  if (!iso) return ''
  return new Date(`${iso}T00:00:00Z`).toLocaleDateString('en-GB', {
    month: 'long', timeZone: 'UTC',
  })
})

function signed(value: number): string {
  return `${value > 0 ? '+' : ''}${value.toFixed(2)}`
}

/**
 * "the warmest August in 42 years", when the month is actually near the top.
 *
 * Only for the first three: a rank of 19 is not a finding, and a ribbon that
 * reports one every month teaches the reader to stop looking at it. The word
 * follows the sign, so a record cold month reads as coldest rather than as a
 * warmest-of-the-bottom.
 */
const rankPhrase = computed(() => {
  const m = enso.value?.latestMonth
  if (!m || m.rank > 3) return ''
  const warm = m.value >= 0
  const nth = m.rank === 1
    ? (warm ? 'the warmest' : 'the coldest')
    : `${m.rank === 2 ? '2nd' : '3rd'} ${warm ? 'warmest' : 'coldest'}`
  const so_far = m.partial ? ' so far' : ''
  return `${nth} ${monthName.value} in ${m.of} years${so_far}`
})

/**
 * The comparison, which is the finding — 47% is a number, and 47% against a
 * normal of 15% is a fact. Falls back to the archive rank when there is no
 * baseline to compare against, and to nothing when there is neither.
 */
const extentPhrase = computed(() => {
  const h = heatwave.value
  if (!h) return ''
  if (h.normal != null && h.ratio != null) {
    // "about the same as" rather than "1x normal", which reads as a unit.
    if (h.ratio >= 1.15) return `${h.ratio}× the normal ${h.normal}% for this time of year`
    if (h.ratio <= 0.85) return `below the normal ${h.normal}% for this time of year`
    return `about the normal ${h.normal}% for this time of year`
  }
  return `${h.rank} highest of ${h.of.toLocaleString('en-GB')} days on record`
})

const ensoTitle = computed(() =>
  `${phaseWord.value} · ${signed(enso.value?.index ?? 0)}${unit} for ${enso.value?.season}`)

/**
 * Both halves select what their sentence is about. The variable is set too, not
 * just the region: reading "El Niño" and landing on a marine-heatwave chart of
 * Niño 3.4 would be a non-sequitur, and the ribbon's whole point is that the
 * next click needs no thought.
 */
function showEnso() {
  const e = enso.value
  if (!e) return
  trackEvent('state_ribbon_clicked', { half: 'enso', region: e.region, phase: e.phase })
  if (store.variableReady('anom')) store.setVariable('anom')
  store.selectRegion(e.region)
}

function showHeatwave() {
  const h = heatwave.value
  if (!h) return
  trackEvent('state_ribbon_clicked', { half: 'heatwave', region: h.region })
  // Guarded: `mhw` is gated on its own archive being complete, and a ribbon
  // built from a rollup that exists is not proof that the gate has opened.
  if (store.variableReady('mhw')) store.setVariable('mhw')
  store.selectRegion(h.region)
}
</script>
