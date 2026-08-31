/**
 * Nuxt UI defaults its internal icons to lucide, but the only collection
 * installed here is `@iconify-json/mdi` — every icon in the app is `i-mdi-*`.
 * Left alone, a component that reaches for one of its own icons logs
 * "Collection lucide is not found locally" and renders nothing — which for
 * USelect means a dropdown with no chevron and no tick beside the chosen item.
 *
 * Add an entry here rather than installing lucide: two icon collections in the
 * bundle so that six glyphs can come from the wrong one is not a trade.
 */
export default defineAppConfig({
  ui: {
    colors: { neutral: 'neutral' }, 
    icons: {
      arrowLeft: 'i-mdi-arrow-left',
      arrowRight: 'i-mdi-arrow-right',
      check: 'i-mdi-check',
      chevronDoubleLeft: 'i-mdi-chevron-double-left',
      chevronDoubleRight: 'i-mdi-chevron-double-right',
      chevronDown: 'i-mdi-chevron-down',
      chevronLeft: 'i-mdi-chevron-left',
      chevronRight: 'i-mdi-chevron-right',
      chevronUp: 'i-mdi-chevron-up',
      close: 'i-mdi-close',
      ellipsis: 'i-mdi-dots-horizontal',
      external: 'i-mdi-open-in-new',
      loading: 'i-mdi-loading',
      minus: 'i-mdi-minus',
      plus: 'i-mdi-plus',
      search: 'i-mdi-magnify',
    },
  },
})
