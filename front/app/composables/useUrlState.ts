/**
 * Keep the URL saying what is on screen, and honour one that already does.
 *
 * A dashboard whose every view lives at the same address cannot be sent to
 * anyone. "Look at the Blob in September 2015" is four gestures to describe and
 * one link to send, and the link is also what lets the guide, a colleague's
 * email and a CIOOS page point at a specific finding rather than at the app.
 *
 * **Client-only, and deliberately after `loadMetadata()`.** Applying the query
 * during SSR would mean issuing the selection fetches inside the render, and
 * `useApi()` only survives the synchronous part of an SSR call chain — the trap
 * `loadRegionSeries`'s `api` parameter exists for. Running from `onMounted`
 * instead costs at most one extra request (the opening cell the bootstrap
 * already fetched) and cannot strand the page.
 *
 * **`replaceState`, never `push`.** Every one of these is a change of view
 * rather than a change of page; pushing would make the browser's Back button
 * step through a hundred playback frames instead of leaving the site.
 */
import { useMainStore, type VariableName } from '~/stores/main'
import { PERIODS, type Period } from '~/utils/periods'

/** Query keys, kept short because these links get pasted into chat and email. */
const KEYS = {
  variable: 'v',
  period: 'p',
  date: 'd',
  region: 'r',
  point: 'at',
} as const

const VARIABLES: VariableName[] = ['sst', 'anom', 'mhw']

function parsePoint(value: string): { lat: number, lon: number } | null {
  const [lat, lon] = value.split(',').map(Number)
  if (!Number.isFinite(lat) || !Number.isFinite(lon)) return null
  if (lat! < -90 || lat! > 90) return null
  return { lat: lat!, lon: lon! }
}

/** A cell as `48.03,-127.97` — trimmed, since the grid is 0.05 degrees. */
function formatPoint(p: { lat: number, lon: number }): string {
  // Longitudes come back on the API's 0-360 convention; written signed here
  // because that is what a person pastes and what `GlobalGrid.gx()` accepts
  // either way.
  const lon = ((p.lon + 180) % 360 + 360) % 360 - 180
  return `${p.lat.toFixed(2)},${lon.toFixed(2)}`
}

function first(value: unknown): string | null {
  const v = Array.isArray(value) ? value[0] : value
  return typeof v === 'string' && v ? v : null
}

export function useUrlState() {
  const store = useMainStore()
  const route = useRoute()

  /**
   * Read the query into the store, in one pass and with one fetch.
   *
   * `variable` and `period` are written straight into state rather than through
   * `setVariable`/`setPeriod`, which would each fire their own refetch of a
   * selection that is about to be replaced anyway — three requests for one link.
   * They are also the two actions that report an analytics event, and arriving
   * on a link is not the same gesture as pressing a toggle: a deep link should
   * look like a page view, not like the visitor having changed the variable.
   */
  async function applyQuery() {
    const q = route.query
    const variable = first(q[KEYS.variable]) as VariableName | null
    const period = first(q[KEYS.period]) as Period | null
    const date = first(q[KEYS.date])
    const region = first(q[KEYS.region])
    const point = first(q[KEYS.point])

    const patch: { variable?: VariableName, period?: Period } = {}
    // Gated exactly as the toggle is: a stale link to `mhw` from before its
    // archive was complete must not open on a variable the app would refuse to
    // draw, and silently falling back is better than an empty map.
    if (variable && VARIABLES.includes(variable) && store.variableReady(variable)) {
      patch.variable = variable
    }
    if (period && PERIODS.some(p => p.value === period)) patch.period = period
    if (Object.keys(patch).length) store.$patch(patch)

    // After the period is in force, so the date snaps to the right bucket — and
    // ALWAYS, not only when the link carries one. `$patch` above deliberately
    // skips `setPeriod`, which is also the thing that re-snaps the current date;
    // without this, `?p=monthly` with no `d` leaves the date on the weekly
    // bucket the bootstrap chose, and the panel reports "no value for this
    // bucket" against a series that has one for the month.
    const target = date ?? store.selectedDate
    if (target) store.setDate(target)

    // One selection, one fetch. `track: false` on the point for the same reason
    // the patch above skips the actions: this is not a click on the map.
    if (region && store.domain?.regions?.some(r => r.key === region)) {
      store.activeRegion = region
      store.scope = 'region'
      await store.loadRegionSeries()
    }
    else if (point) {
      const p = parsePoint(point)
      if (p) await store.selectPoint(p.lat, p.lon, { track: false })
    }
    else if (patch.variable || patch.period) {
      // No selection named, but the field or the window changed under the
      // opening cell the bootstrap already fetched — so that series is for the
      // wrong variable and has to be replaced.
      const p = store.selectedPoint
      if (p) await store.selectPoint(p.lat, p.lon, { track: false })
    }
  }

  /** The query the current state deserves, with defaults left out. */
  function currentQuery(): Record<string, string> {
    const q: Record<string, string> = {
      [KEYS.variable]: store.variable,
      [KEYS.period]: store.period,
    }
    if (store.selectedDate) q[KEYS.date] = store.selectedDate
    if (store.scope === 'region') {
      if (store.activeRegion) q[KEYS.region] = store.activeRegion
    }
    else if (store.pointSeries?.cell) {
      // The resolved CELL, not the raw click: a link should reopen on the same
      // grid cell rather than on whatever pixel happened to be under the cursor,
      // and the two round to the same place anyway.
      q[KEYS.point] = formatPoint(store.pointSeries.cell)
    }
    return q
  }

  function sync() {
    const q = currentQuery()
    const search = new URLSearchParams(q).toString()
    const url = `${window.location.pathname}${search ? `?${search}` : ''}${window.location.hash}`
    if (url !== `${window.location.pathname}${window.location.search}${window.location.hash}`) {
      window.history.replaceState(window.history.state, '', url)
    }
  }

  onMounted(async () => {
    await applyQuery()
    sync()
    // One watcher over everything the URL carries. Playback writes
    // `selectedDate` up to ten times a second and `replaceState` is cheap, but
    // `flush: 'post'` keeps it off the critical path of the frame.
    watch(
      () => [store.variable, store.period, store.selectedDate, store.scope,
             store.activeRegion, store.pointSeries?.cell?.lat, store.pointSeries?.cell?.lon],
      sync,
      { flush: 'post' },
    )
  })
}
