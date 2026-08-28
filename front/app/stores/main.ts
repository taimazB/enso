import { defineStore } from 'pinia'
import { useApi } from '~/composables/useApi'
import type { ColorStop } from '~/utils/colorScale'
import type { Period } from '~/utils/periods'
import type { MonthlyRanking } from '~/utils/ranking'
import { bucketStart } from '~/utils/periods'

/** The two things the map and chart can show. `anom` is derived, not stored. */
export type VariableName = 'sst' | 'anom'

/** A variable's displayed range — what the colour ramp is spread over. */
export interface ColorScaleRange { vmin: number, vmax: number }

export interface DomainMeta {
  subset: { name: string, lat: [number, number], lon: [number, number], shape: [number, number], resolution: number }
  imageBounds: { west: number, south: number, east: number, north: number }
  variables: Record<string, {
    longName: string
    shortName: string
    units: string
    precision: number
    vmin: number
    vmax: number
    colormap: string
    derived: boolean
    /**
     * How `/image` packs this variable's value into the WebP's RGB channels.
     * The images carry data, not colour — Mapbox applies the ramp itself — so
     * these go straight into the raster layer's paint properties. The API
     * computes them; nothing here re-derives the arithmetic.
     */
    encoding: {
      /** `raster-color-mix`: [r, g, b, offset], verbatim. */
      mix: [number, number, number, number]
      /** What the packing can represent, sentinel code included. */
      range: [number, number]
      scale: number
      /** Reserved code for ocean with no value on this variable, or null. */
      sentinel: number | null
      /** `raster-color-range`: the span the 256-entry ramp is tabulated over. */
      colorRange: [number, number]
      /**
       * How far the user may drag the displayed range. Deliberately not
       * `range`: sst's two bytes reach 650 degC, which is arithmetic rather
       * than oceanography. The API decides this; nothing here invents a bound.
       */
      limits: [number, number]
    }
  }>
  /** Keyed by variable: sst's scale is sequential, anom's diverging. */
  colorStops: Record<VariableName, ColorStop[]>
  defaultVariable: VariableName
  /** Ocean with SST but no climatology — the seasonal ice fringe. */
  noClimColor: string
  regions: Array<{ key: string, label: string, lat: [number, number], lon: [number, number], partial: boolean }>
}

export interface Coverage {
  rows: number
  days: number
  start: string | null
  end: string | null
  /** Anomaly is unavailable until all 366 climatology keys are loaded. */
  climatology: { keys: number, complete: boolean } | null
}

export interface Series {
  dates: string[]
  values: Array<number | null>
  period?: Period
  variable?: VariableName
  label?: string | null
  cell?: { lat: number, lon: number }
}

/**
 * Cell the app opens on, so the chart and the ranks are populated before the
 * first map click. North-east Pacific, well inside the subset box and in open
 * water — a land cell would bootstrap into an empty series.
 */
const DEFAULT_POINT = { lat: 48, lon: -128 }

/** localStorage key a variable's display range is remembered under. */
const scaleKey = (variable: VariableName) => `enso.scale.${variable}`

/**
 * The step the *control* moves the range in — deliberately not the encoding's.
 *
 * sst packs at 0.01 degC, which is the right resolution for the data and the
 * wrong one for a slider: an arrow key would move a hundredth of a degree and
 * crossing the range would take 3800 presses. Nothing is lost by coarsening it,
 * because the ramp is tabulated at 256 entries across whatever range it is
 * given — a hundredth of a degree on an endpoint is not a distinguishable
 * colour. anom already steps at 0.1 and keeps it.
 */
function rangeStep(encoding: { scale: number }): number {
  return Math.max(encoding.scale, 0.1)
}

/**
 * Snap to the control's step, so the slider and the number field can never
 * disagree about a value — and round off the float dust the division leaves,
 * which would otherwise reach the slider as a min of -12.700000000000001.
 */
function quantise(value: number, step: number): number {
  return Number((Math.round(value / step) * step).toFixed(6))
}

/** The range in force: an override if there is one, else domain.yml's. */
function resolveScale(
  domain: DomainMeta | null,
  override: ColorScaleRange | undefined,
  variable: VariableName,
): ColorScaleRange {
  if (override) return override
  const meta = domain?.variables?.[variable]
  return { vmin: meta?.vmin ?? 0, vmax: meta?.vmax ?? 1 }
}

/**
 * How far the user may drag the range: the API's `limits`, with the low end
 * stepping over a sentinel where there is one so a user's vmin can never land
 * on the grey no-climatology entry and overwrite it with a scale colour.
 */
function scaleBounds(domain: DomainMeta | null, variable: VariableName): ColorScaleRange {
  const enc = domain?.variables?.[variable]?.encoding
  if (!enc) return { vmin: 0, vmax: 1 }
  const step = rangeStep(enc)
  const floor = enc.sentinel !== null
    ? Math.max(enc.limits[0], enc.range[0] + enc.scale)
    : enc.limits[0]
  // Rounded *inwards*, so a bound can never quantise to a value the encoding
  // cannot hold or, for a sentinel, down onto the sentinel's own code. The
  // quotient is settled before the rounding: 12.7 / 0.1 is 126.99999999999999
  // in binary floating point, and a bare Math.floor would quietly shave the top
  // step off the anomaly's range.
  const inward = (value: number, round: (n: number) => number) =>
    Number((round(Number((value / step).toFixed(6))) * step).toFixed(6))
  return { vmin: inward(floor, Math.ceil), vmax: inward(enc.limits[1], Math.floor) }
}

/** The control's step for a variable, or a safe default before /domain lands. */
function stepFor(domain: DomainMeta | null, variable: VariableName): number {
  const enc = domain?.variables?.[variable]?.encoding
  return enc ? rangeStep(enc) : 0.1
}

/**
 * Spread the server's evenly spaced colours over a new range. The colours are
 * untouched; only the value each one sits at moves. See `stopsFor`.
 */
function rescaleStops(stops: ColorStop[], { vmin, vmax }: ColorScaleRange): ColorStop[] {
  if (stops.length < 2) return stops
  const span = vmax - vmin
  return stops.map((stop, i) => ({
    color: stop.color,
    value: vmin + (i / (stops.length - 1)) * span,
  }))
}

export const useMainStore = defineStore('main', {
  state: () => ({
    domain: null as DomainMeta | null,
    coverage: null as Coverage | null,
    /**
     * Bucket shown on the map, as its first day. Always snapped to `period`, so
     * the map frame and the chart's x-value refer to the same span of days.
     */
    selectedDate: null as string | null,
    /**
     * Averaging window for both the map imagery and the chart. Opens on
     * `weekly`: a single day of anomaly is noisy enough that the first frame
     * reads as speckle, and the weekly mean shows the pattern the map is for.
     */
    period: 'weekly' as Period,
    /**
     * Field shown on the map and charted. Opens on `sst`, not `anom`: SST is
     * the stored variable and is defined everywhere in the box, while the
     * anomaly is undefined over the ~3% ice fringe and needs the climatology
     * to be fully loaded.
     */
    variable: 'sst' as VariableName,
    /** Last clicked map point, or null before the first click. */
    selectedPoint: null as { lat: number, lon: number } | null,
    pointSeries: null as Series | null,
    /** Per-calendar-month year rankings at the selected cell. Always monthly. */
    monthlyRanking: null as MonthlyRanking | null,
    loadingPoint: false,
    /** Set when the clicked point falls outside the ingested box. */
    outsideDomain: null as string | null,
    /**
     * Per-variable display range override; absent means domain.yml's vmin/vmax.
     *
     * The images carry data, not colour — Mapbox applies the ramp itself — so
     * the displayed range is a client-side setting and changing it recolours
     * the map without refetching a single frame. That is the whole reason the
     * value is packed into the WebP rather than baked in as colour.
     */
    scales: {} as Partial<Record<VariableName, ColorScaleRange>>,
  }),

  getters: {
    /** The range in force for a variable: the override, else domain.yml's. */
    scaleFor: state => (variable: VariableName): ColorScaleRange =>
      resolveScale(state.domain, state.scales[variable], variable),

    /** The active variable's range. Watch this to repaint. */
    activeScale: state => resolveScale(state.domain, state.scales[state.variable], state.variable),

    /**
     * The variable's colour stops, re-labelled onto the range in force.
     *
     * `/domain`'s stops are the colormap sampled at 33 *evenly spaced* points
     * between the variable's vmin and vmax (`shared/render.py:colormap_stops`),
     * so stop `i` is simply the colormap at `t = i / (n - 1)`. Spreading those
     * same colours over a new range is therefore exact, and the colormap stays
     * defined in exactly one place — matplotlib, server-side. Nothing here
     * evaluates a colormap.
     */
    stopsFor: state => (variable: VariableName): ColorStop[] =>
      rescaleStops(
        state.domain?.colorStops?.[variable] ?? [],
        resolveScale(state.domain, state.scales[variable], variable),
      ),

    activeStops: state => rescaleStops(
      state.domain?.colorStops?.[state.variable] ?? [],
      resolveScale(state.domain, state.scales[state.variable], state.variable),
    ),

    /**
     * `raster-color-range`: the span the 256-entry ramp is tabulated over.
     *
     * The client-side twin of the same conditional in `api/SERVER.py`'s
     * /domain, and it must stay conditional. A variable **with** a sentinel has
     * to keep tabulating its whole encoding range so code 0 (no climatology)
     * lands in a slot of its own — so `anom`'s tabulation never follows the
     * display range. One without spends all 256 entries on the display range,
     * which is where they are useful, so `sst`'s does follow.
     */
    colorRangeFor: state => (variable: VariableName): [number, number] => {
      const enc = state.domain?.variables?.[variable]?.encoding
      if (!enc) return [0, 1]
      if (enc.sentinel !== null) return enc.range
      const { vmin, vmax } = resolveScale(state.domain, state.scales[variable], variable)
      return [vmin, vmax]
    },

    /**
     * The hard limits a user may drag the range to: what the encoding can
     * represent. The low end steps over a sentinel where there is one, so a
     * user's vmin can never land on the grey no-climatology entry and overwrite
     * it with a scale colour.
     */
    scaleBoundsFor: state => (variable: VariableName): ColorScaleRange =>
      scaleBounds(state.domain, variable),

    /** The increment the range control moves in. See `rangeStep`. */
    scaleStepFor: state => (variable: VariableName): number => stepFor(state.domain, variable),

    /** True when the variable is showing something other than domain.yml's range. */
    scaleIsCustom: state => (variable: VariableName): boolean => state.scales[variable] !== undefined,
  },

  actions: {
    async loadMetadata() {
      if (this.domain && this.coverage) return
      const api = useApi()
      // Started before the first await, not after: `useApi()` reaches for the
      // Nuxt instance, which SSR only keeps for the synchronous part of a call
      // chain. Awaiting first and selecting after loses it (NUXT_E1001) and the
      // fetch never lands. The default cell needs nothing from /domain anyway,
      // so it goes out alongside them.
      const point = this.selectedPoint
        ? null
        // Non-fatal: a failure here costs the opening chart, not the whole page.
        : this.selectPoint(DEFAULT_POINT.lat, DEFAULT_POINT.lon).catch(() => {})

      const [domain, coverage] = await Promise.all([
        api.get<DomainMeta>('/domain'),
        api.get<Coverage>('/coverage'),
      ])
      this.domain = domain
      this.coverage = coverage
      if (!this.selectedDate && coverage.end) this.setDate(coverage.end)
      await point
    },

    /**
     * Set a variable's displayed colour range.
     *
     * Every consumer — the map ramp, the legend, the ranking dots — reads this
     * through `stopsFor`, so they cannot disagree about what a colour means.
     * All clamping lives here rather than in the control, so a value typed into
     * the number field and one dragged on the slider are constrained the same.
     */
    setScale(variable: VariableName, vmin: number, vmax: number) {
      const enc = this.domain?.variables?.[variable]?.encoding
      if (!enc || !Number.isFinite(vmin) || !Number.isFinite(vmax)) return

      const bounds = scaleBounds(this.domain, variable)
      const step = rangeStep(enc)
      let lo = Math.min(Math.max(quantise(vmin, step), bounds.vmin), bounds.vmax)
      let hi = Math.min(Math.max(quantise(vmax, step), bounds.vmin), bounds.vmax)

      // A zero-width — or inverted — range makes the ramp's interpolation
      // degenerate and the map undrawable, so the two ends are held at least one
      // step apart. Whichever end was not just moved gives way.
      if (hi - lo < step) {
        if (lo + step <= bounds.vmax) hi = lo + step
        else lo = hi - step
      }

      const range = { vmin: lo, vmax: hi }
      this.scales[variable] = range
      try {
        localStorage.setItem(scaleKey(variable), JSON.stringify(range))
      }
      catch { /* private mode, or storage full — the range still applies this session */ }
    },

    /** Drop the override, back to domain.yml's vmin/vmax. */
    resetScale(variable: VariableName) {
      delete this.scales[variable]
      try {
        localStorage.removeItem(scaleKey(variable))
      }
      catch { /* see setScale */ }
    },

    /**
     * Restore remembered ranges. Client-only — `loadMetadata()` runs during SSR,
     * where there is no localStorage, so this is called from the control's
     * `onMounted` instead. Everything read back is re-clamped through
     * `setScale`, since `domain.yml`'s encoding may have moved since it was
     * written.
     */
    loadScales() {
      for (const variable of Object.keys(this.domain?.variables ?? {}) as VariableName[]) {
        try {
          const raw = localStorage.getItem(scaleKey(variable))
          if (!raw) continue
          const saved = JSON.parse(raw) as Partial<ColorScaleRange>
          if (typeof saved?.vmin === 'number' && typeof saved?.vmax === 'number') {
            this.setScale(variable, saved.vmin, saved.vmax)
          }
        }
        catch { /* unparseable or unreadable: fall back to the default range */ }
      }
    },

    /** Set the map date, snapped to the current period's bucket. */
    setDate(date: string) {
      this.selectedDate = bucketStart(date, this.period)
    },

    /** Switch averaging window; re-snaps the date and reloads the point series. */
    setPeriod(period: Period) {
      if (period === this.period) return
      this.period = period
      if (this.selectedDate) this.setDate(this.selectedDate)
      if (this.selectedPoint) {
        const { lat, lon } = this.selectedPoint
        return this.selectPoint(lat, lon)
      }
    },

    /**
     * Switch the field shown. Drives the chart request and the image URL
     * together, exactly as `period` does, so the map and the chart never show
     * different variables. The date is untouched — a variable change is not a
     * change of when.
     */
    setVariable(variable: VariableName) {
      if (variable === this.variable) return
      this.variable = variable
      if (this.selectedPoint) {
        const { lat, lon } = this.selectedPoint
        return this.selectPoint(lat, lon)
      }
    },

    async selectPoint(lat: number, lon: number) {
      // The ranking is period-independent, so a period toggle — which re-enters
      // here with the same cell — must not refetch it and flash the grid. It is
      // NOT variable-independent, though: ranking years by SST rather than by
      // anomaly is a different question, so a variable change must refetch.
      const sameCell = this.selectedPoint?.lat === lat
        && this.selectedPoint?.lon === lon
        && this.monthlyRanking !== null
        && this.monthlyRanking.variable === this.variable

      this.selectedPoint = { lat, lon }
      this.outsideDomain = null
      this.loadingPoint = true
      try {
        const api = useApi()
        const [series, ranking] = await Promise.all([
          api.post<Series>('/timeseries', { lat, lon, period: this.period, variable: this.variable }),
          sameCell
            ? Promise.resolve(this.monthlyRanking!)
            : api.post<MonthlyRanking>('/monthlyRanking', { lat, lon, variable: this.variable }),
        ])
        this.pointSeries = series
        this.monthlyRanking = ranking
      }
      catch (error: unknown) {
        // The API answers an out-of-box point with a structured 400 rather than
        // an error — show it as an empty state, not a failure.
        const body = (error as { response?: { data?: { error?: { code?: string }, detail?: string } } }).response?.data
        if (body?.error?.code === 'outside_domain') {
          this.pointSeries = null
          this.monthlyRanking = null
          this.outsideDomain = body.detail ?? 'Outside the ingested domain'
        }
        else { throw error }
      }
      finally {
        this.loadingPoint = false
      }
    },
  },
})
