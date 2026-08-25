// https://nuxt.com/docs/api/configuration/nuxt-config
export default defineNuxtConfig({
  compatibilityDate: '2025-01-01',
  devtools: { enabled: true },

  modules: ['@nuxt/ui', '@pinia/nuxt', '@nuxt/eslint'],

  css: ['~/assets/css/main.css'],

  // Dark-only, matching the ocean-acidification dashboard. Light mode is a real
  // option but nothing here has been checked in it.
  colorMode: {
    preference: 'dark',
    fallback: 'dark',
  },

  runtimeConfig: {
    // Server-only. During SSR the Nitro server is inside the compose network,
    // where the browser-facing http://localhost:9021 is its own loopback — it
    // has to reach the API by service name instead.
    apiInternalBaseUrl: process.env.API_INTERNAL_BASE_URL || 'http://api:4000',
    public: {
      apiBaseUrl: process.env.NUXT_PUBLIC_API_BASE_URL || 'http://localhost:9021',
      mapboxToken: process.env.NUXT_PUBLIC_MAPBOX_TOKEN || '',
      version: process.env.NUXT_PUBLIC_VERSION || 'dev',
    },
  },

  vite: {
    server: {
      // The dev server runs in a container behind a published port.
      hmr: { clientPort: Number(process.env.FRONT_PORT) || 9020 },
      watch: { usePolling: true },
    },
  },
})
