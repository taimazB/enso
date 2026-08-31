import { defineStore } from 'pinia'
import { useApi } from '~/composables/useApi'
import type { ColorStop } from '~/utils/colorScale'
import type { Period } from '~/utils/periods'
import type { MonthlyRanking } from '~/utils/ranking'
import { bucketStart } from '~/utils/periods'

/**
 * The three things the map and chart can show. `anom` is derived rather than
 * stored; `mhw` is stored in its own sparse table and is **categorical**, which
 * changes how it is coloured, ranged and formatted throughout.
 */
export type VariableName = 'sst' | 'anom' | 'mhw'

/**
 * Whether the chart and the numbers panel are reading a clicked cell or a named
 * region.
 *
 * ONE mode switch for the whole app, not one per panel. It drives the chart and
 * the numbers panel together, exactly as `variable` and `period` already drive
 * the chart and the map image together — so the two can never end up describing
 * different things, and the dock's tabs choose a *view* of the current scope
 * rather than a second scope of their own.
 */
export type Scope = 'point' | 'region'

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
    colormap: string | null
    derived: boolean
    /**
     * An ordinal class rather than a measurement. Three consequences, all
     * handled by reading this rather than by testing `name === 'mhw'`: the map
     * ramp is a `step` instead of an `interpolate`, the colour-range control is
     * hidden (there is nothing between two classes to re-range), and nothing
     * prints a unit suffix at it.
     */
    categorical: boolean
    /** The classes, in NOAA's own colours. Empty for a continuous variable. */
    categories: Array<{ value: number, color: string, label: string }>
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
  /**
   * Keyed by variable: sst's scale is sequential, anom's diverging, and mhw's
   * five discrete classes.
   */
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
  /**
   * The MHW archive, ingested separately. It is published about 90 minutes after
   * CoralTemp, so it can trail by a day when a run lands between the two.
   *
   * `complete` gates the variable for a sharper reason than the climatology
   * gates `anom`. `mhw_daily` is sparse — only category >= 1 has a row — so the
   * API restores the zeros by joining against the SST table, and that join
   * cannot tell "no heatwave here" from "this date was never ingested". A
   * half-backfilled archive therefore reports a confident category 0 for every
   * missing year rather than a gap, and a monthly ranking would rank forty
   * fabricated zeroes against one real month. The API decides; nothing here
   * second-guesses it.
   */
  mhw: { rows: number, days: number, start: string | null, end: string | null, complete: boolean } | null
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

/**
 * Region the app opens on. Nino 3.4 is the index the repo is named for, and it
 * is the one number this dashboard exists to show — so it is what the numbers
 * panel reads before anyone has clicked anything.
 */
const DEFAULT_REGION = 'nino34'

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
function rescaleStops(
  stops: ColorStop[],
  { vmin, vmax }: ColorScaleRange,
  categorical = false,
): ColorStop[] {
  // A categorical variable's stops ARE its classes — value 1 is Cat 1 — and its
  // range is not adjustable, so there is nothing to spread. Re-labelling them
  // would currently be the identity (five stops over 1..5 land back on 1..5),
  // which is exactly why it must be skipped explicitly rather than left to
  // coincidence: it stops being the identity the moment a class is added.
  if (categorical || stops.length < 2) return stops
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
     * Field shown on the map and charted. Opens on `anom`, which is the
     * question the dashboard is for — absolute SST is the reference view you
     * switch to. The anomaly needs the full 366-key climatology, though, so
     * `loadMetadata()` falls back to `sst` if /coverage says it is still
     * loading: a partly-loaded climatology blanks the missing dates rather than
     * failing, and the Anomaly button is disabled in that state anyway.
     */
    variable: 'anom' as VariableName,
    /**
     * Whether the chart and the numbers panel read a point or a region.
     *
     * Opens on `region`, and that is a first-run decision rather than a
     * preference: a point panel has nothing to show until someone clicks the
     * map, so opening there means opening on an empty "click something" state —
     * the tutorial this app is trying not to need. A region needs no selection,
     * so the app can land showing a named number beside the map. Clicking the
     * map then moves to `point`, which is what teaches the toggle.
     */
    scope: 'region' as Scope,
    /** Named region the region scope is reading. Seeded by `loadMetadata()`. */
    activeRegion: null as string | null,
    regionSeries: null as Series | null,
    loadingRegion: false,
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
        state.domain?.variables?.[variable]?.categorical,
      ),

    activeStops: state => rescaleStops(
      state.domain?.colorStops?.[state.variable] ?? [],
      resolveScale(state.domain, state.scales[state.variable], state.variable),
      state.domain?.variables?.[state.variable]?.categorical,
    ),

    /**
     * Whether a variable can be offered at all, given what is loaded.
     *
     * Two of the three have a precondition, and neither fails loudly if it is
     * ignored — which is exactly why the toggle is disabled rather than left to
     * produce a plausible-looking wrong answer:
     *
     * - `anom` is derived per day-of-year, so a partly-loaded climatology
     *   silently blanks whichever dates are missing.
     * - `mhw` is stored sparsely and its zeros are restored by a join, so a
     *   partly-loaded archive silently reports category 0 — a real value, and
     *   the wrong one — for every date it has not reached.
     *
     * `sst` is the stored field the other two are built from and is always
     * available, which is what makes it the fallback.
     */
    variableReady: state => (variable: VariableName): boolean => {
      if (variable === 'anom') return state.coverage?.climatology?.complete !== false
      if (variable === 'mhw') return state.coverage?.mhw?.complete === true
      return true
    },

    /** The active region's metadata from /domain, or null before it loads. */
    activeRegionMeta: state =>
      state.domain?.regions?.find(r => r.key === state.activeRegion) ?? null,

    /**
     * The series the chart draws: whichever the current scope names.
     *
     * Read through here rather than by branching in the component, so the chart
     * stays presentational and the scope has exactly one definition.
     */
    activeSeries: (state): Series | null =>
      state.scope === 'region' ? state.regionSeries : state.pointSeries,

    activeSeriesLoading: state =>
      state.scope === 'region' ? state.loadingRegion : state.loadingPoint,

    /** Whether a variable is an ordinal class rather than a measurement. */
    isCategorical: state => (variable: VariableName): boolean =>
      state.domain?.variables?.[variable]?.categorical ?? false,

    activeIsCategorical: state =>
      state.domain?.variables?.[state.variable]?.categorical ?? false,

    /**
     * The unit suffix to print after a value, or '' where there is none.
     *
     * `mhw` is a category, not a measurement: "Cat 3 degC" is nonsense, and the
     * chart tooltip, the legend title and the ranking rows all used to hard-code
     * the degree sign.
     */
    unitLabelFor: state => (variable: VariableName): string =>
      state.domain?.variables?.[variable]?.units === 'degC' ? '\u00B0C' : '',

    activeUnitLabel: state =>
      state.domain?.variables?.[state.variable]?.units === 'degC' ? '\u00B0C' : '',

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
      const meta = state.domain?.variables?.[variable]
      const enc = meta?.encoding
      if (!enc) return [0, 1]
      // A variable whose CODES have to land one per ramp entry tabulates its
      // whole encoding range: `anom` so its sentinel gets a slot of its own, and
      // `mhw` so that code k is entry k. Tabulating mhw's five classes over 1..5
      // would put code 2 at entry 63.75, where a Cat 2 picks up Cat 1's colour.
      if (enc.sentinel !== null || meta?.categorical) return enc.range
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
        // Not a user gesture: this populates the chart for the first click that
        // has not happened yet, and must leave the app in its region-first
        // opening state.
        : this.selectPoint(DEFAULT_POINT.lat, DEFAULT_POINT.lon, { enterScope: false }).catch(() => {})

      const [domain, coverage] = await Promise.all([
        api.get<DomainMeta>('/domain'),
        api.get<Coverage>('/coverage'),
      ])
      this.domain = domain
      this.coverage = coverage
      if (!this.selectedDate && coverage.end) this.setDate(coverage.end)
      // Seeded from /domain rather than assumed: the default key is only used
      // if the API actually offers it, so removing a region from domain.yml
      // cannot leave the app opening on a 404.
      if (!this.activeRegion && domain.regions?.length) {
        this.activeRegion = domain.regions.some(r => r.key === DEFAULT_REGION)
          ? DEFAULT_REGION
          : domain.regions[0]!.key
      }
      await point
      // Only now is it known which variables are actually available. Nothing to
      // do in the normal case; where a derived or separately-ingested variable
      // is not ready this drops back to `sst` — the one that is always there,
      // being the stored field the others are built from — and refetches the
      // opening point on it. Opening on a variable whose own toggle is disabled
      // is worse than opening on the reference view.
      if (!this.variableReady(this.variable)) await this.setVariable('sst')
      // Last, so it requests the variable that survived the check above rather
      // than the one the app hoped to open on. Non-fatal for the same reason
      // the opening point is: it costs the landing panel, not the page.
      if (this.scope === 'region') await this.loadRegionSeries(api).catch(() => {})
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
      const meta = this.domain?.variables?.[variable]
      const enc = meta?.encoding
      if (!enc || !Number.isFinite(vmin) || !Number.isFinite(vmax)) return
      // A categorical scale is not the user's to move: its stops are its
      // classes. The control is hidden for it, and this is the backstop — a
      // stale localStorage entry from before a variable turned categorical
      // reaches `loadScales` and must not be honoured.
      if (meta?.categorical) return

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

    /**
     * Fetch the active region's series for the current variable and period.
     *
     * `/region/{key}` is served from the `region_daily` rollup, so this is a
     * ~150 ms read of 121,696 rows rather than the 3-12 s aggregation over
     * billions that the same request cost before the rollup existed.
     *
     * The `api` parameter is not a convenience. `useApi()` reaches for the Nuxt
     * instance, which SSR only keeps for the *synchronous* part of a call chain
     * — so calling it here, from the tail of `loadMetadata()` after several
     * awaits, throws and the fetch never lands. `loadMetadata` passes the client
     * it captured before its first await; a browser-side caller can let this
     * default.
     */
    async loadRegionSeries(api = useApi()) {
      const key = this.activeRegion
      if (!key) return
      this.loadingRegion = true
      try {
        this.regionSeries = await api.get<Series>(`/region/${key}`, {
          period: this.period,
          variable: this.variable,
        })
      }
      finally {
        this.loadingRegion = false
      }
    },

    /**
     * Show a named region, switching scope to match.
     *
     * Selecting a region *is* switching to region scope — there is no way to
     * pick one without wanting to look at it, and a picker that silently left
     * the chart on a point would be the second mode switch this design exists
     * to avoid.
     */
    selectRegion(key: string): Promise<void> | void {
      const changed = key !== this.activeRegion
      this.activeRegion = key
      this.scope = 'region'
      if (changed || !this.regionSeries) return this.loadRegionSeries()
    },

    /**
     * Switch between the clicked cell and the named region.
     *
     * Fetches only what the target scope is missing: the point series survives a
     * trip through region scope and back, so returning to it is instant and does
     * not re-request a cell that has not changed.
     */
    setScope(scope: Scope): Promise<void> | void {
      if (scope === this.scope) return
      this.scope = scope
      if (scope === 'region' && !this.regionSeries) return this.loadRegionSeries()
      if (scope === 'point' && !this.pointSeries && this.selectedPoint) {
        const { lat, lon } = this.selectedPoint
        return this.selectPoint(lat, lon)
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
      // Both series are bucketed by the API, so both are stale. The inactive
      // one is dropped rather than refetched — switching scope reloads it, and
      // fetching a series nobody is looking at is a request for nothing.
      if (this.scope === 'region') {
        this.pointSeries = null
        return this.loadRegionSeries()
      }
      this.regionSeries = null
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
    // Return type annotated rather than inferred: `loadMetadata` calls this, and
    // an inferred one makes the two mutually recursive — TS then gives up on
    // `this` inside `loadMetadata` and loses every state property.
    setVariable(variable: VariableName): Promise<void> | void {
      if (variable === this.variable) return
      this.variable = variable
      if (this.scope === 'region') {
        this.pointSeries = null
        return this.loadRegionSeries()
      }
      this.regionSeries = null
      if (this.selectedPoint) {
        const { lat, lon } = this.selectedPoint
        return this.selectPoint(lat, lon)
      }
    },

    /**
     * Load a cell's series and ranking.
     *
     * `enterScope` is what separates a *user gesture* from a *bootstrap*. A map
     * click means "look at this cell" and must move the app into point scope;
     * `loadMetadata()` calling this to populate the opening chart means nothing
     * of the sort, and letting it switch scope silently landed the app in point
     * scope on every load — defeating the region-first opening state, whose
     * whole purpose is to have something to show before the first click.
     */
    async selectPoint(lat: number, lon: number, { enterScope = true }: { enterScope?: boolean } = {}) {
      // The ranking is period-independent, so a period toggle — which re-enters
      // here with the same cell — must not refetch it and flash the grid. It is
      // NOT variable-independent, though: ranking years by SST rather than by
      // anomaly is a different question, so a variable change must refetch.
      const sameCell = this.selectedPoint?.lat === lat
        && this.selectedPoint?.lon === lon
        && this.monthlyRanking !== null
        && this.monthlyRanking.variable === this.variable

      this.selectedPoint = { lat, lon }
      // What makes the Point/Region toggle self-teaching: you land in Region,
      // click the map, and the control that moved you is visibly the one that
      // moves you back.
      if (enterScope) this.scope = 'point'
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
