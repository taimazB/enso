import posthog from 'posthog-js'

/**
 * Fire a custom PostHog event.
 *
 * A no-op during SSR (posthog-js is initialised only in the client-only plugin)
 * and a no-op in any environment without a key, so call sites never guard.
 *
 * The events are *decisions*, not requests: what someone chose to look at.
 * Anything that fires on its own — playback's per-frame `setDate`, the colour
 * slider's per-frame drag, a map pan — is either excluded or reduced to one
 * event at the call site, for the same reason `/image` is not instrumented
 * server-side.
 */
export function trackEvent(event: string, properties?: Record<string, unknown>) {
  if (import.meta.client) posthog.capture(event, properties)
}
