import { defineStore } from 'pinia'
import { useApi } from '~/composables/useApi'
import type { Period } from '~/utils/periods'
import type { MonthlyRanking } from '~/utils/ranking'
import { bucketStart } from '~/utils/periods'

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
  }>
  colorStops: Array<{ value: number, color: string }>
  regions: Array<{ key: string, label: string, lat: [number, number], lon: [number, number], partial: boolean }>
}

export interface Coverage {
  rows: number
  days: number
  start: string | null
  end: string | null
}

export interface Series {
  dates: string[]
  values: Array<number | null>
  period?: Period
  label?: string | null
  cell?: { lat: number, lon: number }
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
    /** Averaging window for both the map imagery and the chart. */
    period: 'daily' as Period,
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
      const [domain, coverage] = await Promise.all([
        api.get<DomainMeta>('/domain'),
        api.get<Coverage>('/coverage'),
      ])
      this.domain = domain
      this.coverage = coverage
      if (!this.selectedDate && coverage.end) this.setDate(coverage.end)
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

    async selectPoint(lat: number, lon: number) {
      // The ranking is period-independent, so a period toggle — which re-enters
      // here with the same cell — must not refetch it and flash the grid.
      const sameCell = this.selectedPoint?.lat === lat
        && this.selectedPoint?.lon === lon
        && this.monthlyRanking !== null

      this.selectedPoint = { lat, lon }
      this.outsideDomain = null
      this.loadingPoint = true
      try {
        const api = useApi()
        const [series, ranking] = await Promise.all([
          api.post<Series>('/timeseries', { lat, lon, period: this.period }),
          sameCell
            ? Promise.resolve(this.monthlyRanking!)
            : api.post<MonthlyRanking>('/monthlyRanking', { lat, lon }),
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
