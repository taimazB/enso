<template>
  <UApp>
    <div class="flex h-screen flex-col bg-default text-default">
      <header class="flex shrink-0 items-center gap-3 border-b border-default px-4 py-2">
        <a
          href="https://cioospacific.ca"
          target="_blank"
          rel="noopener"
          class="mr-4 shrink-0 opacity-90 transition-opacity hover:opacity-100"
          aria-label="CIOOS Pacific"
        >
          <img src="/cioospacific-logo.svg" alt="CIOOS Pacific" class="h-8 w-auto" >
        </a>
        <div>
          <h1 class="text-sm font-semibold leading-tight">Pacific Sea Surface Temperature</h1>
          <p class="text-xs text-muted leading-tight">
            NOAA Coral Reef Watch CoralTemp v3.1 &middot; daily &middot; 0.05&deg;
            <span v-if="store.variable === 'anom'"> &middot; vs. 1991&ndash;2020</span>
          </p>
        </div>
        <div class="grow" />
        <!-- <UBadge v-if="store.coverage" variant="subtle" color="neutral">
          {{ store.coverage.start }} &ndash; {{ store.coverage.end }}
        </UBadge> -->
        <UBadge variant="subtle" color="neutral">v{{ version }}</UBadge>
        <AboutDialog />
      </header>

      <NuxtPage class="grow overflow-hidden" />
    </div>
  </UApp>
</template>

<script setup lang="ts">
import { useMainStore } from '~/stores/main'

const store = useMainStore()
const version = useRuntimeConfig().public.version

await store.loadMetadata()
</script>
