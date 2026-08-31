<template>
  <div class="relative size-full">
    <div ref="container" class="size-full" />

    <!-- Everything that changes what the map is showing, in one column: how it
         is projected, then what is being read off it. Stacked with a gap rather
         than positioned individually so neither has to know the other's height. -->
    <div class="absolute left-2 top-2 z-10 flex flex-col items-start gap-2">
      <UFieldGroup v-if="token" size="xs" class="rounded-lg shadow-lg">
        <UButton
          v-for="item in PROJECTIONS"
          :key="item.value"
          :icon="item.icon"
          :label="item.label"
          :color="projection === item.value ? 'primary' : 'neutral'"
          :variant="projection === item.value ? 'solid' : 'subtle'"
          :title="item.title"
          @click="setProjection(item.value)"
        />
      </UFieldGroup>

      <ScopeControl />
    </div>

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
import type { ColorStop } from '~/utils/colorScale'

const store = useMainStore()
const api = useApi()
const token = useRuntimeConfig().public.mapboxToken

const container = ref<HTMLElement | null>(null)
let map: mapboxgl.Map | null = null
let marker: mapboxgl.Marker | null = null
let resize: ResizeObserver | null = null

const SOURCE_ID = 'field-image'
const LAYER_ID = 'field-layer'

/**
 * The same frame, placed one world to the west — the globe's other half.
 *
 * Mercator draws world copies, so a single quad at 100..290 crosses the
 * antimeridian and is drawn whole. **The globe has no world copies**: measured
 * in Chromium, that quad clips dead at 180 and the entire eastern Pacific goes
 * missing. Placing the identical image a second time at -260..-70 puts the other
 * half where it belongs; each source draws only the part of itself that falls
 * inside -180..180, so the two abut at the dateline and never overlap.
 *
 * It is added on the globe only. In Mercator both quads resolve to the same
 * ground, and two layers at 0.85 opacity would double up into a darker box.
 */
const WEST_SOURCE_ID = 'field-image-west'
const WEST_LAYER_ID = 'field-layer-west'
const WORLD = 360

const REGION_SOURCE_ID = 'region-box'
const REGION_FILL_ID = 'region-box-fill'
const REGION_LINE_ID = 'region-box-line'
/** Amber, matching the chart's MAP markLine — "where the app is looking". */
const REGION_COLOR = '#05df72'

type ProjectionName = 'globe' | 'mercator'

const PROJECTIONS = [
  { value: 'globe' as const, icon: 'i-mdi-earth', label: 'Globe', title: 'Globe — true areas, the basin as it sits on the planet' },
  { value: 'mercator' as const, icon: 'i-mdi-map-outline', label: 'Flat', title: 'Mercator — the whole box flat and visible at once' },
]

/** Remembered across sessions, like the dock width and the colour ranges. */
const PROJECTION_KEY = 'enso.map.projection'

/**
 * Where the globe opens: the North Pacific, centred just east of the dateline.
 *
 * A globe cannot be framed with `fitBounds` — half the box is behind the limb at
 * any zoom that fits it — so it gets a centre and a zoom instead. Mercator keeps
 * fitting the full box, which is the shape it is good at.
 */
const GLOBE_VIEW = { center: [-128, 48] as [number, number], zoom: 3 }

const projection = ref<ProjectionName>('globe')

/**
 * Whether the style has finished loading and layers may be added.
 *
 * Tracked here rather than asked of Mapbox: `isStyleLoaded()` answers a
 * different question — whether every source has settled — and is routinely
 * false on a fully drawn map.
 */
let styleReady = false

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
function imageCoordinates(offset = 0): [[number, number], [number, number], [number, number], [number, number]] {
  const b = store.domain!.imageBounds
  return [
    [b.west + offset, b.north],
    [b.east + offset, b.north],
    [b.east + offset, b.south],
    [b.west + offset, b.south],
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
  const meta = store.domain!.variables[name]!
  const enc = meta.encoding
  // Both go through the store rather than /domain directly: the displayed range
  // is a user setting, and `stopsFor` spreads the server's colours over it.
  const stops = store.stopsFor(name)

  const ramp: Array<unknown> = meta.categorical
    ? categoricalRamp(stops)
    : continuousRamp(stops, enc)

  return {
    'raster-opacity': 0.85,
    'raster-fade-duration': 0,
    'raster-resampling': 'nearest',
    'raster-color-mix': enc.mix,
    'raster-color-range': store.colorRangeFor(name),
    'raster-color': ramp,
  }
}

/**
 * A `step`, not an `interpolate`: there is no colour between Cat 2 and Cat 3
 * because there is no value between them. Interpolating would draw a gradient
 * across a boundary that does not exist and make a Cat 2 pixel next to a Cat 4
 * one read as a Cat 3.
 *
 * The thresholds are the class values themselves rather than midpoints, which
 * is exact only because the ramp is tabulated over the encoding's whole 0..255
 * range — entry k is code k, so a Cat 3 lands on the entry at 3 and nowhere
 * near 2.5. See `colorRangeFor` in the store.
 *
 * The output below the first threshold is the first class's own colour, which
 * covers codes 0 and 1. Code 0 never reaches the ramp — land, ice and
 * heatwave-free ocean are all alpha 0 — so nothing is drawn with it either way.
 */
function categoricalRamp(stops: ColorStop[]): Array<unknown> {
  if (!stops.length) return ['step', ['raster-value'], 'rgba(0,0,0,0)']
  const ramp: Array<unknown> = ['step', ['raster-value'], stops[0]!.color]
  for (const stop of stops.slice(1)) ramp.push(stop.value, stop.color)
  return ramp
}

function continuousRamp(
  stops: ColorStop[],
  enc: { sentinel: number | null, range: [number, number], scale: number },
): Array<unknown> {
  const ramp: Array<unknown> = ['interpolate', ['linear'], ['raster-value']]
  if (enc.sentinel !== null) {
    // Code 0 is ocean that has no value on this variable — the ice fringe with
    // no climatology. Flat grey: transparent would read as land, and any colour
    // on a diverging scale would read as a real anomaly near zero. It sits one
    // whole encoding step below the first real code, so the ramp cannot blend
    // the two.
    ramp.push(enc.range[0], store.domain!.noClimColor)
    // The anchor that ends the sentinel's flat grey and starts the scale. It is
    // skipped when the user has dragged vmin down onto it, because the first
    // real stop is then already at that value and already that colour —
    // `interpolate` rejects two entries with the same input, which took the
    // whole layer down rather than just the one pair.
    const anchor = enc.range[0] + enc.scale
    if (stops[0]!.value > anchor) ramp.push(anchor, stops[0]!.color)
  }
  for (const stop of stops) ramp.push(stop.value, stop.color)
  return ramp
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
  syncWestCopy()
}

/** Add or drop the westward copy so it exists exactly on the globe. */
function syncWestCopy() {
  const url = currentUrl()
  if (!map || !map.getLayer(LAYER_ID)) return

  const wanted = projection.value === 'globe'
  const present = Boolean(map.getSource(WEST_SOURCE_ID))
  if (wanted === present) return

  if (wanted && url) {
    map.addSource(WEST_SOURCE_ID, { type: 'image', url, coordinates: imageCoordinates(-WORLD) })
    map.addLayer({
      id: WEST_LAYER_ID,
      type: 'raster',
      source: WEST_SOURCE_ID,
      paint: rasterPaint(store.variable),
    }, LAYER_ID)
  }
  else if (present) {
    map.removeLayer(WEST_LAYER_ID)
    map.removeSource(WEST_SOURCE_ID)
  }
}

/** Every field layer currently on the map, with the offset its quad sits at. */
function fieldLayers(): Array<{ layer: string, source: string, offset: number }> {
  const layers = [{ layer: LAYER_ID, source: SOURCE_ID, offset: 0 }]
  if (map?.getLayer(WEST_LAYER_ID)) layers.push({ layer: WEST_LAYER_ID, source: WEST_SOURCE_ID, offset: -WORLD })
  return layers
}

/**
 * The active region's box, as a densified GeoJSON polygon.
 *
 * TWO THINGS HERE ARE NOT DECORATION.
 *
 * 1. **The edges are densified.** A region box is a rectangle in lat/lon, not in
 *    any projection: its north and south edges follow a parallel, which is a
 *    curve on the globe and on Mercator both. The PDO domain spans 70 degrees of
 *    longitude, so a four-corner polygon would be drawn as a straight chord and
 *    bow off the parallel by a visible margin — putting the box somewhere other
 *    than the data it summarises. A vertex every `EDGE_STEP` degrees keeps it on
 *    the parallel.
 *
 * 2. **Longitudes stay unwrapped**, matching the image source and `/domain`'s own
 *    convention. Every region's east edge is above 180 and three of the eight
 *    cross the antimeridian outright (Nino 4 at 160..210, the Bering Sea at
 *    180..200, the PDO box at 180..250). Normalising east into -180..180 would
 *    make west > east — the same collapse `imageCoordinates()` documents.
 *
 *    **Verified in Chromium, both projections**, because the raster needed a
 *    second westward quad to survive the globe and there was no reason to
 *    assume a polygon would not: Nino 4 and the PDO box each draw as ONE
 *    continuous box across the dateline on the globe and on Mercator. No split
 *    ring, no MultiPolygon, no second copy. Splitting them at 180 was tried and
 *    is not needed.
 *
 *    Do not re-derive that from `queryRenderedFeatures`: on the globe it returns
 *    0 for a box that is plainly drawn on screen. Screenshot it, as CLAUDE.md
 *    says for the canvas.
 */
const EDGE_STEP = 2

function densify(from: number, to: number, at: (v: number) => [number, number]): Array<[number, number]> {
  const steps = Math.max(1, Math.ceil(Math.abs(to - from) / EDGE_STEP))
  return Array.from({ length: steps }, (_, i) => at(from + ((to - from) * i) / steps))
}

function regionPolygon(region: { lat: [number, number], lon: [number, number] }) {
  const [south, north] = [...region.lat].sort((a, b) => a - b)
  const [west, east] = [...region.lon].sort((a, b) => a - b)
  const ring = [
    ...densify(west, east, lon => [lon, north]),
    ...densify(north, south, lat => [east, lat]),
    ...densify(east, west, lon => [lon, south]),
    ...densify(south, north, lat => [west, lat]),
  ]
  ring.push(ring[0]!)
  return { type: 'Feature' as const, properties: {}, geometry: { type: 'Polygon' as const, coordinates: [ring] } }
}

/**
 * Draw the region the app is currently reading, or nothing.
 *
 * Only ONE box is ever on the map, and only in region scope — the box is the
 * visual half of what the numbers panel is showing, so the two are the same
 * selection seen twice rather than two independent controls. Clicking a point
 * moves the app to point scope and the box goes with it.
 */
function syncRegionBox() {
  // NOT `map.isStyleLoaded()`. That reports whether every source in the style
  // has settled, not whether layers can be added, and it stays false long after
  // 'load' has fired and the raster layers are up — measured at 10 s into a
  // loaded page. Guarding on it silently skipped the box forever. `styleReady`
  // is set in the 'load' handler, which is the actual precondition.
  if (!map || !styleReady) return
  const region = store.scope === 'region' ? store.activeRegionMeta : null

  if (!region) {
    if (map.getLayer(REGION_FILL_ID)) map.removeLayer(REGION_FILL_ID)
    if (map.getLayer(REGION_LINE_ID)) map.removeLayer(REGION_LINE_ID)
    if (map.getSource(REGION_SOURCE_ID)) map.removeSource(REGION_SOURCE_ID)
    return
  }

  const data = regionPolygon(region)
  const existing = map.getSource(REGION_SOURCE_ID) as mapboxgl.GeoJSONSource | undefined
  if (existing) {
    existing.setData(data)
    return
  }

  map.addSource(REGION_SOURCE_ID, { type: 'geojson', data })
  // A wash rather than a tint: the field underneath is the thing being read, and
  // a fill heavy enough to notice would shift every colour inside the box.
  map.addLayer({
    id: REGION_FILL_ID,
    type: 'fill',
    source: REGION_SOURCE_ID,
    paint: { 'fill-color': REGION_COLOR, 'fill-opacity': 0.07 },
  })
  map.addLayer({
    id: REGION_LINE_ID,
    type: 'line',
    source: REGION_SOURCE_ID,
    paint: { 'line-color': REGION_COLOR, 'line-width': 2, 'line-opacity': 0.9 },
  })
}

/** The ramp, mix and range all change with the variable, not just the URL. */
function applyPaint() {
  if (!map || !map.getLayer(LAYER_ID)) return
  const paint = rasterPaint(store.variable)
  for (const { layer } of fieldLayers()) {
    for (const [key, value] of Object.entries(paint)) {
      map.setPaintProperty(layer, key as never, value as never)
    }
  }
}

/** Frame the box the way the current projection wants to be framed. */
function frame(animate = true) {
  if (!map) return
  if (projection.value === 'globe') {
    const options = { ...GLOBE_VIEW }
    if (animate) map.easeTo({ ...options, duration: 600 })
    else map.jumpTo(options)
    return
  }
  // The whole box, no clipping. The old INITIAL_NORTH workaround existed
  // because the OISST domain ran to 90N and the 70-90 strip dominated a
  // Mercator fit; this box stops at 65N, so it fits without a sliver.
  const b = store.domain!.imageBounds
  map.fitBounds([[b.west, b.south], [b.east, b.north]], {
    padding: 20,
    duration: animate ? 600 : 0,
  })
}

/**
 * Fly to the active region's box.
 *
 * The box is the visual half of what the numbers panel is reading, so selecting
 * a region moves the camera to it rather than leaving the user to find an amber
 * rectangle somewhere on a 190-degree-wide basin — Nino 1+2 is 10 degrees wide
 * on a box that spans 190, i.e. about 3% of its width.
 *
 * Longitudes stay **unwrapped**, as everywhere else here: three regions cross
 * the antimeridian (Nino 4 at 160..210, the Bering Sea at 180..200, the PDO box
 * at 180..250) and normalising east into -180..180 would make west > east, which
 * `fitBounds` reads as a bounds running the long way round the planet. Mapbox
 * projects 250 the same way it projects the polygon's vertices and wraps the
 * resulting centre itself.
 *
 * `fitBounds` is used on the globe too — unlike `frame()`, which cannot use it
 * because the whole domain is 190 degrees wide and half of it is behind the limb
 * at any zoom that fits it. A region box is at most 70 degrees (the PDO domain),
 * which fits on the visible face.
 *
 * `maxZoom` is what stops the smallest boxes from filling the screen: Nino 1+2
 * fitted exactly would leave no coastline around it to say where it is, and the
 * point of the flight is context, not magnification.
 */
const REGION_PADDING = 60
const REGION_MAX_ZOOM = 4.5

function frameRegion(animate = true) {
  const region = store.scope === 'region' ? store.activeRegionMeta : null
  if (!map || !region) return

  const [south, north] = [...region.lat].sort((a, b) => a - b)
  const [west, east] = [...region.lon].sort((a, b) => a - b)
  map.fitBounds([[west, south], [east, north]], {
    padding: REGION_PADDING,
    maxZoom: REGION_MAX_ZOOM,
    duration: animate ? 1200 : 0,
  })
}

function setProjection(name: ProjectionName) {
  if (name === projection.value) return
  projection.value = name
  map?.setProjection({ name })
  syncWestCopy()
  // Re-frame after the swap: a view that fits the box flat is not a view that
  // shows it on a sphere, and vice versa. In region scope the region is what is
  // being read, so the swap keeps looking at it rather than pulling back out to
  // the whole basin.
  if (store.scope === 'region' && store.activeRegionMeta) frameRegion()
  else frame()
  try {
    localStorage.setItem(PROJECTION_KEY, name)
  }
  catch { /* private mode — the choice still applies this session */ }
}

onMounted(() => {
  if (!token || !container.value || !store.domain) return

  try {
    const saved = localStorage.getItem(PROJECTION_KEY)
    if (saved === 'globe' || saved === 'mercator') projection.value = saved
  }
  catch { /* see setProjection */ }

  mapboxgl.accessToken = token
  const b = store.domain.imageBounds
  const globe = projection.value === 'globe'

  map = new mapboxgl.Map({
    container: container.value,
    style: 'mapbox://styles/mapbox/dark-v11',
    minZoom: 1,
    maxZoom:6,
    // Globe by default, opened on the North Pacific. `bounds` and `center`/`zoom`
    // are alternatives, not both — see `frame()` for why each projection gets
    // its own.
    ...(globe
      ? GLOBE_VIEW
      : { bounds: [[b.west, b.south], [b.east, b.north]] as [[number, number], [number, number]], fitBoundsOptions: { padding: 20 } }),
    projection: { name: projection.value },
  })

  map.addControl(new mapboxgl.NavigationControl({ showCompass: false }), 'top-right')

  // Dev-only handle for browser verification. The map cannot be checked by
  // reading its canvas — it runs with `preserveDrawingBuffer: false`, so
  // `drawImage` yields a blank frame — so a driven browser needs the instance
  // itself to query layers and rendered features. Stripped from production by
  // `import.meta.dev`.
  if (import.meta.dev) {
    const w = window as unknown as { __map?: mapboxgl.Map, __store?: typeof store }
    w.__map = map
    w.__store = store
  }

  // The ranks dock takes width from the map, and its drag handle takes it
  // continuously — the canvas has to follow the container rather than the window.
  resize = new ResizeObserver(() => map?.resize())
  resize.observe(container.value)

  // The style may already be loaded by the time this runs (a warm style cache),
  // in which case 'load' has fired and would never fire again.
  const draw = () => { styleReady = true; addRaster(); syncRegionBox() }
  if (map.isStyleLoaded()) draw()
  else map.once('load', draw)

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
  if (map.getSource(SOURCE_ID)) {
    // Repaint before swapping the image: the two encodings pack their value
    // differently, so a frame drawn with the previous variable's mix would
    // decode to nonsense for the one flicker it is on screen.
    applyPaint()
    for (const { source, offset } of fieldLayers()) {
      const image = map.getSource(source) as mapboxgl.ImageSource | undefined
      image?.updateImage({ url, coordinates: imageCoordinates(offset) })
    }
  }
  else if (map.isStyleLoaded()) addRaster()
})

// A range change is a repaint and nothing more — deliberately separate from the
// watcher above, which also swaps the image. The frame on screen carries the
// value, not the colour, so re-ranging never needs another byte from /image;
// calling updateImage here would flash the basemap and refetch a frame the
// browser already has, for a result identical to setting the paint property.
watch(() => store.activeScale, applyPaint, { deep: true })

// The box follows the scope and the chosen region together — it is one
// selection drawn twice, not a layer with a toggle of its own — and the camera
// follows the box. Leaving point scope deliberately moves nothing: the pin is
// already where the user clicked, so a flight there would be a jolt with no
// destination.
watch(() => [store.scope, store.activeRegion], () => {
  syncRegionBox()
  frameRegion()
})

onBeforeUnmount(() => {
  resize?.disconnect()
  resize = null
  marker?.remove()
  map?.remove()
  map = null
})
</script>
