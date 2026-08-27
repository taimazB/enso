import { defineStore } from 'pinia'
import { useApi } from '~/composables/useApi'
import type { Period } from '~/utils/periods'
import type { MonthlyRanking } from '~/utils/ranking'
import { bucketStart } from '~/utils/periods'

/** The two things the map and chart can show. `anom` is derived, not stored. */
export type VariableName = 'sst' | 'anom'

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
    }
  }>
  /** Keyed by variable: sst's scale is sequential, anom's diverging. */
  colorStops: Record<VariableName, Array<{ value: number, color: string }>>
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
  }),

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
