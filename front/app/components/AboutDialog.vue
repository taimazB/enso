<template>
  <!-- The default slot is the trigger; `#body` is the panel. Opening is tracked
       once, on open only — a modal closes by the same event and counting both
       would double every read (the same reason `MonthlyRankPanel`'s guide
       popover guards on `open`). -->
  <UModal v-model:open="open" title="About this dashboard" :ui="{ content: 'max-w-2xl' }">
    <UButton
      icon="i-mdi-information-outline"
      variant="ghost"
      color="neutral"
      size="xs"
      aria-label="About this dashboard"
      title="About this dashboard"
    >
      <span class="hidden sm:inline">About</span>
    </UButton>

    <template #body>
      <div class="space-y-5 text-sm">
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

watch(open, (isOpen) => {
  if (isOpen) trackEvent('about_opened')
})
</script>
