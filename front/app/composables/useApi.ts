import axios from 'axios'
import type { Period } from '~/utils/periods'

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
  const publicBase = config.public.apiBaseUrl
  const requestBase = import.meta.server ? config.apiInternalBaseUrl : publicBase

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
    /** Always browser-facing: this URL is handed to Mapbox, not fetched here. */
    imageUrl(date: string, period: Period = 'daily', width = 720): string {
      return `${publicBase}/image/${date}.png?width=${width}&period=${period}`
    },
  }
}
