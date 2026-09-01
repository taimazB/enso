import { trackEvent } from '~/composables/useAnalytics'
import { useMainStore } from '~/stores/main'
import { bucketStart, shiftBuckets } from '~/utils/periods'

export const MIN_FPS = 1
export const MAX_FPS = 10
const DEFAULT_FPS = 4

/** Frames warmed ahead of the playhead. */
const AHEAD = 8
/** Decoded frames held at once — see the note on memory below. */
const CACHE_MAX = 24
/** A single slow frame must not freeze playback. */
const FRAME_TIMEOUT_MS = 3000

/**
 * Play the map forward one bucket at a time until the user stops it.
 *
 * The playhead writes through `store.setDate()`, so the map, the chart's MAP
 * markLine and the date input all follow with no extra wiring — and a manual
 * step or chart click mid-playback simply relocates the playhead, because each
 * tick advances from whatever `store.selectedDate` currently is.
 *
 * Frames are not all preloaded: the archive is ~16.4k daily WebPs (~1.2 GB, and
 * ~4 MB each once decoded). Instead a small window ahead of the playhead is
 * fetched with `new Image()` and decoded off the critical path; `/image` sends
 * `Cache-Control: public, max-age=86400`, so Mapbox's own fetch for the same URL
 * then resolves out of the browser cache with no network and no decode stall.
 *
 * The loop is paced by frame readiness rather than a bare `setInterval`, because
 * a Mapbox `ImageSource` never retries a failed image and silently keeps showing
 * the previous frame — a fixed interval would turn that into an unexplained
 * stutter.
 *
 * Reaching the end of coverage stops playback rather than wrapping: the archive
 * is a record with an end, and looping back to 1985 mid-watch reads as a glitch.
 * Pressing play again while parked on the last bucket restarts from the first,
 * since the button would otherwise do nothing.
 */
export function usePlayback() {
  const store = useMainStore()
  const api = useApi()

  const playing = ref(false)
  const fps = ref(DEFAULT_FPS)

  /** Invalidates an in-flight loop, so stop/start cannot leave two running. */
  let run = 0
  const cache = new Map<string, HTMLImageElement>()

  const canPlay = computed(() => Boolean(store.selectedDate && store.coverage?.end))

  function warm(url: string): HTMLImageElement {
    let img = cache.get(url)
    if (!img) {
      img = new Image()
      // Mandatory, not a nicety: Mapbox fetches an image source in CORS mode,
      // while a bare `new Image()` sends no Origin — and the API only answers
      // with `Access-Control-Allow-Origin` when it sees one. The prefetch would
      // then park a header-less response in the HTTP cache that Mapbox's own
      // fetch reuses and the browser blocks, leaving the map stuck on one frame.
      img.crossOrigin = 'anonymous'
      img.decoding = 'async'
      img.src = url
      cache.set(url, img)
      while (cache.size > CACHE_MAX) cache.delete(cache.keys().next().value!)
    }
    return img
  }

  /** Resolves once the frame is decoded, or gives up so the loop keeps moving. */
  function ready(url: string): Promise<unknown> {
    const decoded = warm(url).decode().catch(() => {})
    return Promise.race([decoded, sleep(FRAME_TIMEOUT_MS)])
  }

  /** The next bucket, or null once the end of coverage is past. */
  function next(from: string): string | null {
    const stepped = shiftBuckets(from, store.period, 1)
    const end = store.coverage?.end
    if (end && stepped > bucketStart(end, store.period)) return null
    return stepped
  }

  function sleep(ms: number): Promise<void> {
    return new Promise(resolve => setTimeout(resolve, ms))
  }

  async function loop() {
    const mine = ++run
    while (playing.value && mine === run && store.selectedDate) {
      const began = performance.now()
      const target = next(store.selectedDate)
      if (!target) break

      await ready(api.imageUrl(target, store.period, store.variable))
      if (!playing.value || mine !== run) return

      store.setDate(target)

      // Queue the window in front of the frame just shown. Cheap: these are
      // ~78 KB each and already rendered to disk on the API side.
      let ahead: string | null = target
      for (let i = 0; i < AHEAD; i++) {
        ahead = next(ahead)
        if (!ahead) break
        warm(api.imageUrl(ahead, store.period, store.variable))
      }

      // fps is read per frame, so the slider takes effect on the next one.
      const wait = 1000 / fps.value - (performance.now() - began)
      if (wait > 0) await sleep(wait)
    }
    // Ran off the end of coverage. Guarded on `mine`, so a loop that exited
    // because a newer one took over does not stop the one now running.
    if (mine === run) playing.value = false
  }

  function play() {
    if (!canPlay.value || playing.value) return
    // Parked on the last bucket, there is nothing forward to play — rewind
    // rather than start a loop that exits on its first tick.
    if (store.selectedDate && !next(store.selectedDate)) {
      const start = store.coverage?.start
      if (!start) return
      store.setDate(bucketStart(start, store.period))
    }
    playing.value = true
    // One event per press, not per frame. `store.setDate()` runs up to ten
    // times a second here, so instrumenting the playhead would bury every real
    // decision under it — the same reason `/image` is not instrumented
    // server-side, and the reason `setDate` carries no event at all.
    trackEvent('playback_started', {
      variable: store.variable,
      period: store.period,
      fps: fps.value,
      from: store.selectedDate,
    })
    void loop()
  }

  function stop() {
    playing.value = false
    run++
  }

  function toggle() {
    if (playing.value) stop()
    else play()
  }

  onBeforeUnmount(stop)

  return { playing, fps, canPlay, play, stop, toggle }
}
