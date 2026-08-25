<template>
  <div class="relative size-full">
    <div ref="container" class="size-full" />

    <div
      v-if="!token"
      class="absolute inset-0 z-10 flex items-center justify-center bg-elevated/90 p-6 text-center text-sm text-muted"
    >
      Set <code class="text-highlighted">NUXT_PUBLIC_MAPBOX_TOKEN</code> in
      <code class="text-highlighted">.env.dev</code> to show the basemap.
    </div>
  </div>
</template>

<script setup lang="ts">
import mapboxgl from 'mapbox-gl'
import 'mapbox-gl/dist/mapbox-gl.css'
import { useMainStore } from '~/stores/main'

const store = useMainStore()
const api = useApi()
const token = useRuntimeConfig().public.mapboxToken

const container = ref<HTMLElement | null>(null)
let map: mapboxgl.Map | null = null
let marker: mapboxgl.Marker | null = null

const SOURCE_ID = 'anom-image'
const LAYER_ID = 'anom-layer'
/** Northern edge of the opening view; see the `bounds` comment below. */
const INITIAL_NORTH = 68

/** Corner coordinates for a Mapbox image source: TL, TR, BR, BL. */
function imageCoordinates(): [[number, number], [number, number], [number, number], [number, number]] {
  const b = store.domain!.imageBounds
  return [
    [b.west, b.north],
    [b.east, b.north],
    [b.east, b.south],
    [b.west, b.south],
  ]
}

function currentUrl(): string | null {
  return store.selectedDate ? api.imageUrl(store.selectedDate, store.period) : null
}

function addRaster() {
  const url = currentUrl()
  if (!map || !url || map.getSource(SOURCE_ID)) return

  map.addSource(SOURCE_ID, { type: 'image', url, coordinates: imageCoordinates() })
  map.addLayer({
    id: LAYER_ID,
    type: 'raster',
    source: SOURCE_ID,
    paint: { 'raster-opacity': 0.85, 'raster-fade-duration': 0 },
  })
}

onMounted(() => {
  if (!token || !container.value || !store.domain) return

  mapboxgl.accessToken = token
  const b = store.domain.imageBounds

  map = new mapboxgl.Map({
    container: container.value,
    style: 'mapbox://styles/mapbox/dark-v11',
    // Fitting the whole domain would open at a near-global zoom: in Mercator the
    // 70-90N strip alone is taller than everything below it, so the interesting
    // mid-latitude Pacific ends up a sliver. Open on the signal instead — the
    // raster still covers the full domain once the user zooms out.
    bounds: [[b.west, b.south], [b.east, Math.min(b.north, INITIAL_NORTH)]],
    fitBoundsOptions: { padding: 20 },
    projection: { name: 'globe' },
  })

  map.addControl(new mapboxgl.NavigationControl({ showCompass: false }), 'top-right')

  // The style may already be loaded by the time this runs (a warm style cache),
  // in which case 'load' has fired and would never fire again.
  if (map.isStyleLoaded()) addRaster()
  else map.once('load', addRaster)

  map.on('click', (event) => {
    const { lat, lng } = event.lngLat
    store.selectPoint(Number(lat.toFixed(4)), Number(lng.toFixed(4)))
    marker ??= new mapboxgl.Marker({ color: '#f8fafc' })
    marker.setLngLat([lng, lat]).addTo(map!)
  })
})

// Swapping the URL in place keeps the layer and its paint properties, so
// stepping through days — or switching to a weekly/monthly mean — does not
// flash the basemap between frames.
watch(() => [store.selectedDate, store.period], () => {
  const url = currentUrl()
  if (!map || !url) return
  const source = map.getSource(SOURCE_ID) as mapboxgl.ImageSource | undefined
  if (source) source.updateImage({ url, coordinates: imageCoordinates() })
  else if (map.isStyleLoaded()) addRaster()
})

onBeforeUnmount(() => {
  marker?.remove()
  map?.remove()
  map = null
})
</script>
