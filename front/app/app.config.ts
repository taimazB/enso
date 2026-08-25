/**
 * Nuxt UI defaults its internal icons to lucide, but the only collection
 * installed here is `@iconify-json/mdi` — every icon in the app is `i-mdi-*`.
 * Left alone, a component that reaches for one of its own icons (UModal's close
 * button is the first to do so) logs "Collection lucide is not found locally"
 * and renders nothing.
 */
export default defineAppConfig({
  ui: {
    icons: {
      close: 'i-mdi-close',
    },
  },
})
