<template>
  <!-- The one mode switch in the app, and it lives on the map because that is
       what it changes: Point reads the cell you clicked, the other half reads a
       named box drawn on the same canvas. It sits under the projection pair so
       every control that reframes what the map is showing is in one column.

       The region half is a MENU, not a second toggle. There are eight regions
       and only one is ever drawn, so a plain toggle needed a picker on a row of
       its own underneath it — two controls for one decision. As a dropdown the
       button states the region currently being read and opening it is how you
       change it, which is the same gesture either way. -->
  <UFieldGroup size="xs" class="rounded-lg shadow-lg">
    <UButton
      icon="i-mdi-map-marker"
      label="Point"
      :color="store.scope === 'point' ? 'primary' : 'neutral'"
      :variant="store.scope === 'point' ? 'solid' : 'subtle'"
      :title="store.selectedPoint ? 'The clicked cell' : 'Click the map to pick a cell'"
      @click="store.setScope('point')"
    />
    <UDropdownMenu :items="items" :content="{ align: 'start' }">
      <UButton
        icon="i-mdi-vector-rectangle"
        trailing-icon="i-mdi-chevron-down"
        :label="store.activeRegionMeta?.label ?? 'Region'"
        :color="store.scope === 'region' ? 'primary' : 'neutral'"
        :variant="store.scope === 'region' ? 'solid' : 'subtle'"
        title="Area mean over a named region — pick one"
      />
    </UDropdownMenu>
  </UFieldGroup>
</template>

<script setup lang="ts">
import { useMainStore } from '~/stores/main'

const store = useMainStore()

/**
 * Picking a region IS switching to region scope — `store.selectRegion()` sets
 * both, so there is no state where the menu names a box the panel is not
 * reading. The active one is marked by weight and ink rather than a tick column,
 * which keeps every row on the same left edge.
 */
const items = computed(() => (store.domain?.regions ?? []).map(r => ({
  label: r.label,
  class: store.scope === 'region' && r.key === store.activeRegion
    ? 'font-semibold text-primary'
    : undefined,
  onSelect: () => { store.selectRegion(r.key) },
})))
</script>
