import axios from 'axios'
import type { Period } from '~/utils/periods'

/** Mirrors `shared/render.py`'s DEFAULT_WIDTH — see `imageUrl` below. */
export const IMAGE_WIDTH = 2048

/**
 * Thin axios wrapper bound to the API.
 *
 * Requests use two different base URLs on purpose: during SSR the Nitro server
 * talks to the API over the compose network (`http://api:4000`), while anything
 * the browser fetches — including image URLs handed to Mapbox — must use the
 * published host URL.
 */
export function useApi() {
  const config = useRuntimeConfig()
  // Trailing slashes are stripped because every URL below is built by plain
  // concatenation with a leading-slash path. A base ending in `/` yields
  // `//image/...`, and the API 404s a doubled slash on every route — which
  // surfaces as a blank map and a chart that never updates, while SSR (on the
  // internal base, which has no slash to trim) keeps working and hides it.
  const publicBase = String(config.public.apiBaseUrl ?? '').replace(/\/+$/, '')
  const internalBase = String(config.apiInternalBaseUrl ?? '').replace(/\/+$/, '')
  const requestBase = import.meta.server ? internalBase : publicBase

  return {
    baseURL: publicBase,
    async get<T>(path: string, params?: Record<string, unknown>): Promise<T> {
      const { data } = await axios.get<T>(`${requestBase}${path}`, { params })
      return data
    },
    async post<T>(path: string, body?: unknown): Promise<T> {
      const { data } = await axios.post<T>(`${requestBase}${path}`, body)
      return data
    },
    /**
     * Always browser-facing: this URL is handed to Mapbox, not fetched here.
     *
     * `width` must match what `CRW.cli render` was run with — the cache is keyed
     * by (variable, period, bucket start, width), so a different width is a
     * different file, and for a historical bucket there is no NetCDF left to
     * render one from. A mismatch shows up as a 404 and a blank map, not as a
     * slower render.
     */
    imageUrl(date: string, period: Period = 'daily', variable = 'sst', width = IMAGE_WIDTH): string {
      return `${publicBase}/image/${date}.webp?width=${width}&period=${period}&variable=${variable}`
    },
  }
}
