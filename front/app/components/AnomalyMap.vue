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
import { useMainStore, type VariableName } from '~/stores/main'

const store = useMainStore()
const api = useApi()
const token = useRuntimeConfig().public.mapboxToken

const container = ref<HTMLElement | null>(null)
let map: mapboxgl.Map | null = null
let marker: mapboxgl.Marker | null = null
let resize: ResizeObserver | null = null

const SOURCE_ID = 'field-image'
const LAYER_ID = 'field-layer'

/** Move (or create) the pin marking the selected cell. */
function showMarker(lat: number, lon: number) {
  if (!map) return
  marker ??= new mapboxgl.Marker({ color: '#05df72' })
  marker.setLngLat([lon, lat]).addTo(map)
}

/**
 * Corner coordinates for a Mapbox image source: TL, TR, BR, BL.
 *
 * `imageBounds.east` is **unwrapped** — the Pacific box ends at 290, not -70.
 * Mapbox accepts that and places the quad correctly across the antimeridian
 * (verified: `project([290,0])` and `project([-70,0])` return the same pixel).
 * Normalising it into -180..180 here would make west > east and collapse the
 * image to nothing.
 */
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
  return store.selectedDate
    ? api.imageUrl(store.selectedDate, store.period, store.variable)
    : null
}

/**
 * Paint properties that turn a value-encoded WebP into a coloured map.
 *
 * `/image` ships DATA — the value packed into the RGB channels, land in alpha —
 * and Mapbox colours it here with `raster-color`. That is what keeps the
 * palette and the displayed range client-side: the daily NetCDF is pruned to a
 * retention window, so the cached image is eventually the only copy of that
 * field, and a pre-coloured one would have today's colormap baked in for good.
 *
 * `raster-resampling: nearest` is deliberate and load-bearing for `sst`, which
 * packs its value across two channels as `G*256 + B`. Linear filtering blends
 * the two channels independently, so a texel pair straddling a low-byte wrap
 * would decode ~2.56 degC away from either neighbour. Measured, nearest and
 * linear come out identical on the decode (median error 0.156 vs 0.159 degC,
 * both just texel quantisation) — and nearest is visibly cleaner at
 * single-pixel islands, which linear renders as coloured speckle.
 */
function rasterPaint(name: VariableName): Record<string, unknown> {
  const enc = store.domain!.variables[name]!.encoding
  const stops = store.domain!.colorStops[name]

  const ramp: Array<unknown> = ['interpolate', ['linear'], ['raster-value']]
  if (enc.sentinel !== null) {
    // Code 0 is ocean that has no value on this variable — the ice fringe with
    // no climatology. Flat grey: transparent would read as land, and any colour
    // on a diverging scale would read as a real anomaly near zero. It sits one
    // whole encoding step below the first real code, so the ramp cannot blend
    // the two.
    ramp.push(enc.range[0], store.domain!.noClimColor)
    ramp.push(enc.range[0] + enc.scale, stops[0]!.color)
  }
  for (const stop of stops) ramp.push(stop.value, stop.color)

  return {
    'raster-opacity': 0.85,
    'raster-fade-duration': 0,
    'raster-resampling': 'nearest',
    'raster-color-mix': enc.mix,
    'raster-color-range': enc.colorRange,
    'raster-color': ramp,
  }
}

function addRaster() {
  const url = currentUrl()
  if (!map || !url || map.getSource(SOURCE_ID)) return

  map.addSource(SOURCE_ID, { type: 'image', url, coordinates: imageCoordinates() })
  map.addLayer({
    id: LAYER_ID,
    type: 'raster',
    source: SOURCE_ID,
    paint: rasterPaint(store.variable),
  })
}

/** The ramp, mix and range all change with the variable, not just the URL. */
function applyPaint() {
  if (!map || !map.getLayer(LAYER_ID)) return
  const paint = rasterPaint(store.variable)
  for (const [key, value] of Object.entries(paint)) {
    map.setPaintProperty(LAYER_ID, key as never, value as never)
  }
}

onMounted(() => {
  if (!token || !container.value || !store.domain) return

  mapboxgl.accessToken = token
  const b = store.domain.imageBounds

  map = new mapboxgl.Map({
    container: container.value,
    style: 'mapbox://styles/mapbox/dark-v11',
    // The whole box, no clipping. The old INITIAL_NORTH workaround existed
    // because the OISST domain ran to 90N and the 70-90 strip dominated a
    // Mercator fit; this box stops at 65N, so it fits without a sliver.
    bounds: [[b.west, b.south], [b.east, b.north]],
    fitBoundsOptions: { padding: 20 },
    // Mercator, not globe: the domain is one basin rather than the whole
    // planet, and a globe hides half of it behind the limb at the opening zoom.
    projection: { name: 'mercator' },
  })

  map.addControl(new mapboxgl.NavigationControl({ showCompass: false }), 'top-right')

  // The ranks dock takes width from the map, and its drag handle takes it
  // continuously — the canvas has to follow the container rather than the window.
  resize = new ResizeObserver(() => map?.resize())
  resize.observe(container.value)

  // The style may already be loaded by the time this runs (a warm style cache),
  // in which case 'load' has fired and would never fire again.
  if (map.isStyleLoaded()) addRaster()
  else map.once('load', addRaster)

  map.on('click', (event) => {
    const { lat, lng } = event.lngLat
    store.selectPoint(Number(lat.toFixed(4)), Number(lng.toFixed(4)))
    showMarker(lat, lng)
  })

  // The store opens on a default cell, so the pin has to be there before the
  // first click or the chart would be describing an unmarked point.
  if (store.selectedPoint) showMarker(store.selectedPoint.lat, store.selectedPoint.lon)
})

// Swapping the URL in place keeps the layer and its paint properties, so
// stepping through days — or switching to a weekly/monthly mean — does not
// flash the basemap between frames.
watch(() => [store.selectedDate, store.period, store.variable], () => {
  const url = currentUrl()
  if (!map || !url) return
  const source = map.getSource(SOURCE_ID) as mapboxgl.ImageSource | undefined
  if (source) {
    // Repaint before swapping the image: the two encodings pack their value
    // differently, so a frame drawn with the previous variable's mix would
    // decode to nonsense for the one flicker it is on screen.
    applyPaint()
    source.updateImage({ url, coordinates: imageCoordinates() })
  }
  else if (map.isStyleLoaded()) addRaster()
})

onBeforeUnmount(() => {
  resize?.disconnect()
  resize = null
  marker?.remove()
  map?.remove()
  map = null
})
</script>
