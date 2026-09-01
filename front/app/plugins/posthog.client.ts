import posthog from 'posthog-js'
import axios from 'axios'

/**
 * PostHog, browser side. **Client-only** — `posthog-js` touches `window` at
 * init, and this file's `.client` suffix is what keeps it out of SSR.
 *
 * Lives under `app/` because Nuxt 4's srcDir is `app/`: a top-level
 * `front/plugins/` is not picked up at all (the ocean-acidification dashboard
 * has one there; do not copy that layout here).
 */
export default defineNuxtPlugin(() => {
  const config = useRuntimeConfig()
  // No key, no analytics, no error — mirrors the API's own no-op so dev needs
  // no special case and a forgotten key can never break the page.
  if (!config.public.posthogKey) return

  posthog.init(config.public.posthogKey as string, {
    api_host: config.public.posthogHost as string,
    // Off deliberately, both of them. This is a full-bleed Mapbox UI: autocapture
    // would log a stream of canvas clicks whose coordinates mean nothing, and
    // session replay would record a map that redraws on every frame of playback.
    // The events worth having are the discrete decisions, captured explicitly
    // through `useAnalytics`.
    autocapture: false,
    disable_session_recording: true,
    capture_pageview: true,
  })

  // One assignment reaches every API call: `useApi()` uses the module-level
  // axios default instance rather than an `axios.create()` of its own, so this
  // header rides along on /timeseries, /region/* and the rest — letting the API
  // attribute its events to the same visitor instead of falling back to IP.
  // The image URLs are handed to Mapbox, not fetched through axios, and are
  // deliberately not instrumented anyway (see api/modules/posthog_helpers.py).
  axios.defaults.headers.common['X-PostHog-Distinct-Id'] = posthog.get_distinct_id()
})
