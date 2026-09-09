<template>
  <!-- One dialog, two triggers: the guide and the credits are the same panel
       opened on a different tab, so there is one place to edit the copy and no
       second modal component. Opening is tracked once, on open only — a modal
       closes by the same event and counting both would double every read (the
       same reason `MonthlyRankPanel`'s guide popover guards on `open`). -->
  <UButton
    icon="i-mdi-help-circle-outline"
    variant="ghost"
    color="neutral"
    size="xs"
    aria-label="How to use this dashboard"
    title="How to use this dashboard"
    @click="openOn('guide')"
  >
    <span class="hidden sm:inline">Guide</span>
  </UButton>

  <UButton
    icon="i-mdi-information-outline"
    variant="ghost"
    color="neutral"
    size="xs"
    aria-label="About this dashboard"
    title="About this dashboard"
    @click="openOn('about')"
  >
    <span class="hidden sm:inline">About</span>
  </UButton>

  <UModal v-model:open="open" :title="title" :ui="{ content: 'max-w-2xl' }">
    <template #body>
      <div v-if="tab === 'guide'" class="space-y-5 text-sm">
        <p class="text-muted">
          Everything on screen is one selection &mdash; a place, a variable and a
          date &mdash; seen four ways: the map, the chart, the numbers and the
          rankings. Change it anywhere and all four follow.
        </p>

        <!-- Every step carries a small replica of the control it is about,
             built from the same Nuxt UI components the app uses rather than
             from a screenshot: a picture would drift the first time a variant
             or a label changed, and would need re-shooting per theme. The
             replicas are inert (`pointer-events-none`, `aria-hidden`) — the
             real control is a few pixels away and this one only has to be
             recognisable. Where the state is free, they mirror the live store,
             so the figure names the region actually loaded and greys a field
             that genuinely is not ready. -->
        <ol class="space-y-4">
          <li v-for="(step, i) in steps" :key="step.title" class="flex gap-3">
            <span
              class="mt-0.5 flex size-5 shrink-0 items-center justify-center rounded-full bg-elevated text-[11px] font-semibold text-highlighted"
            >{{ i + 1 }}</span>
            <div class="min-w-0 grow space-y-1">
              <p class="flex items-center gap-1.5 font-medium text-highlighted">
                {{ step.title }}
              </p>
              <p class="text-muted">{{ step.text }}</p>

              <div
                class="overflow-x-auto rounded-md border border-default bg-default/60 px-2.5 py-2"
                aria-hidden="true"
              >
                <div class="pointer-events-none w-max select-none">
                  <!-- Live, like the other figures: it names the phase and the
                       extent actually on screen rather than a frozen example
                       that would read as stale the day ENSO turns over. -->
                  <div
                    v-if="step.figure === 'ribbon'"
                    class="flex items-center gap-2 text-xs"
                  >
                    <span
                      class="rounded px-1.5 py-0.5 text-[11px] font-semibold uppercase tracking-wide"
                      :class="phaseChip.class"
                    >{{ phaseChip.label }}</span>
                    <span class="text-muted">Niño 3.4</span>
                    <span class="h-4 w-px bg-accented" />
                    <span class="rounded bg-orange-500/15 px-1.5 py-0.5 text-[11px] font-semibold uppercase tracking-wide text-orange-400">Heatwave</span>
                    <span class="text-muted">
                      <span class="tabular-nums text-highlighted">{{ store.pacific?.heatwave?.extent ?? '—' }}%</span>
                      of the Pacific
                    </span>
                  </div>

                  <UFieldGroup v-else-if="step.figure === 'field'" size="xs">
                    <UButton
                      v-for="v in FIELDS"
                      :key="v.value"
                      :label="v.label"
                      :color="store.variable === v.value ? 'primary' : 'neutral'"
                      :variant="store.variable === v.value ? 'solid' : 'subtle'"
                      :disabled="!store.variableReady(v.value)"
                    />
                  </UFieldGroup>

                  <UFieldGroup v-else-if="step.figure === 'place'" size="xs">
                    <UButton
                      icon="i-mdi-map-marker"
                      label="Point"
                      :color="store.scope === 'point' ? 'primary' : 'neutral'"
                      :variant="store.scope === 'point' ? 'solid' : 'subtle'"
                    />
                    <UButton
                      icon="i-mdi-vector-rectangle"
                      trailing-icon="i-mdi-chevron-down"
                      :label="store.activeRegionMeta?.label ?? 'Region'"
                      :color="store.scope === 'region' ? 'primary' : 'neutral'"
                      :variant="store.scope === 'region' ? 'solid' : 'subtle'"
                    />
                  </UFieldGroup>

                  <div v-else-if="step.figure === 'time'" class="flex items-center gap-2">
                    <UFieldGroup size="xs">
                      <UButton
                        v-for="pd in PERIODS"
                        :key="pd.value"
                        :label="pd.label"
                        :color="store.period === pd.value ? 'primary' : 'neutral'"
                        :variant="store.period === pd.value ? 'solid' : 'subtle'"
                      />
                    </UFieldGroup>
                    <div class="h-5 w-px bg-accented" />
                    <UButton icon="i-mdi-chevron-left" variant="ghost" color="neutral" size="xs" />
                    <span
                      class="rounded-md px-2 py-1 text-xs tabular-nums text-default ring ring-accented"
                    >{{ store.selectedDate ?? '2026-08-24' }}</span>
                    <UButton icon="i-mdi-chevron-right" variant="ghost" color="neutral" size="xs" />
                    <UButton icon="i-mdi-skip-next" variant="ghost" color="neutral" size="xs" />
                    <div class="h-5 w-px bg-accented" />
                    <UButton icon="i-mdi-play" variant="ghost" color="neutral" size="xs" />
                    <span class="h-1 w-16 rounded-full bg-primary/60" />
                    <span class="text-xs tabular-nums text-muted">4 fps</span>
                  </div>

                  <svg
                    v-else-if="step.figure === 'chart'"
                    viewBox="0 0 300 56" class="h-14 w-[300px]"
                  >
                    <polyline
                      :points="sparkline"
                      fill="none"
                      :stroke="PRIMARY"
                      stroke-width="1.25"
                      stroke-linejoin="round"
                    />
                    <line x1="196" y1="2" x2="196" y2="40" :stroke="ACCENT" stroke-width="1" stroke-dasharray="3 3" />
                    <text x="199" y="10" font-size="7" :fill="ACCENT">MAP</text>
                    <rect x="0" y="46" width="300" height="8" rx="2" class="fill-current text-muted/15" />
                    <rect x="96" y="46" width="150" height="8" rx="2" class="fill-current text-muted/35" />
                    <circle cx="96" cy="50" r="3.5" class="fill-current text-muted/70" />
                    <circle cx="246" cy="50" r="3.5" class="fill-current text-muted/70" />
                  </svg>

                  <div v-else-if="step.figure === 'colour'" class="w-48">
                    <span class="mb-1 flex items-center gap-1.5">
                      <span class="text-[11px] font-medium text-muted">{{ legendTitle }}</span>
                      <span
                        class="ml-auto flex items-center gap-0.5 rounded px-1 py-px text-[10px] text-muted ring-1 ring-default"
                      >
                        <UIcon name="i-mdi-tune-variant" class="size-3" />
                        Customize
                      </span>
                    </span>
                    <span class="block h-2.5 w-full rounded" :style="{ background: legendGradient }" />
                    <span class="mt-1 flex justify-between text-[11px] text-muted">
                      <span v-for="(t, n) in legendTicks" :key="n">{{ t }}</span>
                    </span>
                  </div>

                  <svg
                    v-else-if="step.figure === 'ranks'"
                    viewBox="0 0 300 56" class="h-14 w-[300px]"
                  >
                    <g v-for="(row, n) in rankRows" :key="row.label">
                      <text
                        x="46" :y="14 + n * 16" text-anchor="end" font-size="8"
                        class="fill-current" :class="n === 0 ? 'text-highlighted' : 'text-muted'"
                      >{{ row.label }}</text>
                      <line
                        :x1="row.x - 22" :y1="11 + n * 16" :x2="row.x + 22" :y2="11 + n * 16"
                        class="stroke-current text-muted/50" stroke-width="1"
                      />
                      <circle
                        :cx="row.x" :cy="11 + n * 16" r="4"
                        :fill="row.open ? 'transparent' : row.color"
                        :stroke="row.ring ? ACCENT : row.color"
                        :stroke-width="row.ring ? 2 : 1.5"
                      />
                    </g>
                  </svg>

                  <div v-else class="flex items-center gap-3">
                    <UButton icon="i-mdi-download" label="Download data" variant="ghost" color="neutral" size="xs" />
                    <UButton icon="i-mdi-download" variant="ghost" color="neutral" size="xs" />
                  </div>
                </div>
              </div>
            </div>
          </li>
        </ol>

        <section class="space-y-2">
          <h3 class="text-xs font-semibold uppercase tracking-wide text-highlighted">
            Worth knowing
          </h3>
          <ul class="space-y-1.5 text-muted">
            <li v-for="note in notes" :key="note" class="flex gap-2">
              <span class="mt-1.5 size-1 shrink-0 rounded-full bg-accented" />
              <span>{{ note }}</span>
            </li>
          </ul>
        </section>
      </div>

      <div v-else class="space-y-5 text-sm">
        <section class="space-y-2">
          <p class="text-default">
            Daily sea surface temperature, its anomaly against the 1991&ndash;2020
            climatology, and marine heatwave category for the Pacific
            &mdash; 60&deg;S&ndash;65&deg;N, 100&deg;E&ndash;290&deg;E &mdash; at
            0.05&deg; resolution, from 1985 to the present.
          </p>
          <p class="text-muted">
            Click the map for a cell, or pick a named region (the Ni&ntilde;o boxes,
            the Blob, the PDO domain) to read its area mean. Every series on screen
            can be downloaded as CSV.
          </p>
        </section>

        <section class="space-y-2">
          <h3 class="text-xs font-semibold uppercase tracking-wide text-highlighted">Data</h3>
          <ul class="space-y-1 text-muted">
            <li>
              <ULink
                to="https://coralreefwatch.noaa.gov/product/5km/index_5km_sst.php"
                target="_blank"
                class="text-primary"
              >NOAA Coral Reef Watch CoralTemp v3.1</ULink>
              &mdash; daily global 5&nbsp;km SST.
            </li>
            <li>
              <ULink
                to="https://coralreefwatch.noaa.gov/product/marine_heatwave/index.php"
                target="_blank"
                class="text-primary"
              >NOAA CRW Marine Heatwave v1.0.1</ULink>
              &mdash; daily heatwave category, Moderate through Beyond extreme.
            </li>
            <li v-if="store.coverage">
              Ingested here: {{ store.coverage.start }} &ndash; {{ store.coverage.end }}.
            </li>
          </ul>
        </section>

        <section class="space-y-2">
          <h3 class="text-xs font-semibold uppercase tracking-wide text-highlighted">Credits</h3>
          <p class="text-muted">
            Built on the Northeast Pacific marine heatwave monitoring work of
            Andrea Hilborn and colleagues at
            <ULink to="https://www.dfo-mpo.gc.ca" target="_blank" class="text-primary">
              Fisheries and Oceans Canada
            </ULink>
            (Institute of Ocean Sciences) &mdash;
            <ULink :to="PRIOR_WORK" target="_blank" class="text-primary"
              >IOS-OSD-DPG/Pacific_SST_Monitoring</ULink>.
          </p>
          <p class="text-muted">
            This tool builds off of an earlier version of the Marine Heatwave
            Monitor tool developed by
            <ULink to="https://hakai.org" target="_blank" class="text-primary"
              >The Hakai Institute</ULink>.
          </p>
          <p class="text-muted">
            Published by
            <ULink to="https://cioospacific.ca" target="_blank" class="text-primary"
              >CIOOS Pacific</ULink>.
          </p>
        </section>

        <section class="space-y-3">
          <h3 class="text-xs font-semibold uppercase tracking-wide text-highlighted">
            Funding &amp; support
          </h3>
          <p class="text-muted">
            With the support of MEOPAR, Fisheries and Oceans Canada, and Ocean
            Networks Canada.
          </p>
          <!-- The logos sit on a light chip rather than bare on the dark ground:
               two of the three are dark-ink marks (the DFO signature is the
               federal FIP, which may not be recoloured), so inverting or
               tinting them is not an option. -->
          <ul class="flex flex-wrap items-center gap-3">
            <li v-for="s in supporters" :key="s.name">
              <ULink
                :to="s.href"
                target="_blank"
                class="flex h-14 items-center rounded-md bg-white px-3 py-2 transition-opacity hover:opacity-80"
                :aria-label="s.name"
                :title="s.name"
              >
                <!-- The federal signature is ~12:1 and the other two are square,
                     so height alone would let it take the whole row: each mark
                     carries its own cap. -->
                <img :src="s.logo" :alt="s.name" class="h-auto w-auto object-contain" :class="s.size" >
              </ULink>
            </li>
          </ul>
        </section>

        <p class="text-xs text-dimmed">
          Version {{ version }}. Anonymous usage analytics only &mdash; no accounts,
          no personal data.
        </p>
      </div>
    </template>
  </UModal>
</template>

<script setup lang="ts">
import { useMainStore } from '~/stores/main'
import { trackEvent } from '~/composables/useAnalytics'

/** The prior work this dashboard is modelled on. */
const PRIOR_WORK = 'https://github.com/IOS-OSD-DPG/Pacific_SST_Monitoring'

/**
 * The guide is a numbered walk through one session rather than a tour of the
 * controls: the order is the order the questions arrive in — what am I looking
 * at, where, when, and then what the panels around it are saying.
 */
/**
 * The ribbon figure's badge, matching `StateRibbon`'s own colours.
 *
 * Live rather than a frozen example, so the figure cannot show a red El Niño
 * chip during a La Niña — the one way a decorative replica can actively mislead.
 */
const phaseChip = computed(() => ({
  el_nino: { label: 'El Niño', class: 'bg-red-500/15 text-red-400' },
  la_nina: { label: 'La Niña', class: 'bg-sky-500/15 text-sky-400' },
  neutral: { label: 'Neutral', class: 'bg-elevated text-muted' },
}[store.pacific?.enso?.phase ?? 'neutral']))

const steps = [
  {
    title: 'Start with the headline',
    figure: 'ribbon',
    text: 'The strip under the title is the state of the basin right now, and needs no setting up: '
      + 'the El Niño / La Niña phase from the Niño 3.4 anomaly, and how much of the '
      + 'Pacific is in a marine heatwave against what is normal for the date. Click either half to '
      + 'open it in the panels below; the (i) beside it says exactly how each is calculated.',
  },
  {
    title: 'Choose the field',
    figure: 'field',
    text: 'Temperature, Anomaly (against the 1991\u20132020 climatology) or Marine heatwave category, '
      + 'in the bar under the map. A field whose archive is not fully loaded stays disabled and says why.',
  },
  {
    title: 'Choose a place',
    figure: 'place',
    text: 'Click anywhere on the map to read that 5 km cell, or switch to Region and pick a named box '
      + '\u2014 the Ni\u00f1o indices, the Blob, the Bering Sea, the PDO domain \u2014 to read its '
      + 'area mean. The amber outline is exactly what is being averaged.',
  },
  {
    title: 'Move through time',
    figure: 'time',
    text: 'Daily, weekly or monthly frames; step with the arrows, type a date, or press play. Playback '
      + 'runs to the end of the archive and stops there rather than looping.',
  },
  {
    title: 'Read the chart, and steer with it',
    figure: 'chart',
    text: 'The chart is the whole record at the selected place. Click it to send the map to that date '
      + '\u2014 the amber line marks the frame on screen \u2014 and drag the handles beneath it to zoom '
      + 'into a span of years.',
  },
  {
    title: 'Re-colour the map',
    figure: 'colour',
    text: 'Click the legend to open the colour range: preset bands, two handles, exact numbers, Reset. '
      + 'The map recolours instantly and fetches nothing, because each frame carries values rather than '
      + 'colours. Your range is remembered per field.',
  },
  {
    title: 'Compare years',
    figure: 'ranks',
    text: 'The panel on the left ranks every year\u2019s version of the month the map is on. Its own '
      + '\u201cHow to read\u201d popover explains the dots and the whiskers; clicking a row moves the map '
      + 'to that year.',
  },
  {
    title: 'Take the numbers with you',
    figure: 'csv',
    text: 'Two CSV buttons: one saves the series as plotted, the other all twelve months of rankings. '
      + 'Both export what is already on screen, so the file and the chart cannot disagree.',
  },
]

/** The behaviours that surprise people, each of which is deliberate. */
const notes = [
  'Weeks start on Monday and every bucket is labelled by its first day, on the map and in the CSV alike.',
  'A weekly or monthly heatwave frame is the worst category reached in that span, not an average \u2014 '
    + 'there is no category between two categories. A region\u2019s heatwave series is an area mean, so it '
    + 'is a severity index rather than a class.',
  'Grey ocean has no climatology \u2014 the seasonal ice fringe \u2014 so it has a temperature but no '
    + 'anomaly. Land is transparent, not grey.',
  'The map opens as a globe; the Globe / Flat pair at its top-left switches, and the choice is remembered.',
  'Drag the left dock\u2019s edge to give the panels or the map more room.',
]

/** The three field toggles, in the order TimeControl draws them. */
const FIELDS = [
  { value: 'anom' as const, label: 'Anomaly' },
  { value: 'sst' as const, label: 'SST' },
  { value: 'mhw' as const, label: 'MHW' },
]

const PERIODS = [
  { value: 'daily' as const, label: 'Daily' },
  { value: 'weekly' as const, label: 'Weekly' },
  { value: 'monthly' as const, label: 'Monthly' },
]

/** Figure ink. Amber is "where the map is" everywhere in this app. */
const ACCENT = '#f59e0b'
const PRIMARY = '#22d3ee'

const supporters = [
  { name: 'MEOPAR', href: 'https://meopar.ca', logo: '/meopar-logo.png', size: 'max-h-10 max-w-24' },
  {
    name: 'Fisheries and Oceans Canada',
    href: 'https://www.dfo-mpo.gc.ca',
    logo: '/dfo-logo.svg',
    size: 'max-h-6 max-w-64',
  },
  { name: 'Ocean Networks Canada', href: 'https://www.oceannetworks.ca', logo: '/onc-logo.svg', size: 'max-h-10 max-w-10' },
]

const store = useMainStore()
const version = useRuntimeConfig().public.version
const open = ref(false)
const tab = ref<'guide' | 'about'>('guide')

const title = computed(() =>
  tab.value === 'guide' ? 'How to use this dashboard' : 'About this dashboard')

function openOn(which: 'guide' | 'about') {
  tab.value = which
  open.value = true
}

/**
 * The colour-range figure is drawn from the live scale rather than a canned
 * gradient, so the bar in the guide is the bar on screen — including a range
 * the user has already dragged.
 */
const legendStops = computed(() => store.stopsFor(store.variable))

const legendTitle = computed(() =>
  store.domain?.variables?.[store.variable]?.shortName ?? 'Anomaly')

const legendGradient = computed(() => {
  const list = legendStops.value
  if (!list.length) return 'linear-gradient(to right, #2166ac, #f7f7f7, #b2182b)'
  return `linear-gradient(to right, ${list
    .map((s, i) => `${s.color} ${((i / (list.length - 1)) * 100).toFixed(1)}%`)
    .join(', ')})`
})

const legendTicks = computed(() => {
  const list = legendStops.value
  if (list.length < 2) return ['-3', '0', '+3']
  const lo = list[0]!.value
  const hi = list[list.length - 1]!.value
  return [lo, (lo + hi) / 2, hi].map(v => (v > 0 ? `+${v.toFixed(0)}` : v.toFixed(0)))
})

/**
 * Three rows of the ranking, standing in for forty-odd: the top one, an
 * ordinary one, and the year the map is on (amber ring) which is also the
 * archive's partial edge month (open dot). Colours come off the live scale so
 * the sample dots are colours in play, the same rule the panel's own guide
 * popover follows.
 */
const rankRows = computed(() => {
  const list = legendStops.value
  const at = (f: number) => list.length
    ? list[Math.min(list.length - 1, Math.round(f * (list.length - 1)))]!.color
    : '#94a3b8'
  return [
    { label: '1. 1997', x: 250, color: at(0.9), ring: false, open: false },
    { label: '2. 2015', x: 232, color: at(0.8), ring: false, open: false },
    { label: '21. 2026 *', x: 150, color: at(0.5), ring: true, open: true },
  ]
})

/** A stand-in series: enough shape to read as a record, not real data. */
const sparkline = Array.from({ length: 60 }, (_, i) => {
  const x = (i / 59) * 300
  const y = 24
    - 9 * Math.sin(i / 3.1)
    - 5 * Math.sin(i / 1.3)
    - (i / 59) * 6
  return `${x.toFixed(1)},${y.toFixed(1)}`
}).join(' ')

watch(open, (isOpen) => {
  if (isOpen) trackEvent(tab.value === 'guide' ? 'guide_opened' : 'about_opened')
})
</script>
