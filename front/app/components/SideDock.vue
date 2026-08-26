<template>
  <aside
    class="relative flex min-h-0 shrink-0 flex-col border-l border-default bg-default"
    :style="{ width: `${width}px` }"
  >
    <!-- The dock and the map compete for the same width, and which one wins
         depends on what the user is doing — so it is a drag, not a constant.
         Arrow keys move it too: the handle is the only control here that a
         pointer-only implementation would put out of reach. -->
    <div
      role="separator"
      aria-orientation="vertical"
      :aria-label="`Resize ${title}`"
      tabindex="0"
      class="absolute inset-y-0 -left-1 z-20 w-2 cursor-col-resize touch-none transition-colors hover:bg-primary/40 focus-visible:bg-primary/40 focus-visible:outline-none"
      :class="dragging ? 'bg-primary/40' : ''"
      @pointerdown="startDrag"
      @keydown.left.prevent="nudge(STEP)"
      @keydown.right.prevent="nudge(-STEP)"
    />

    <header class="flex shrink-0 items-start gap-2 border-b border-default py-2 pl-3 pr-2">
      <div class="min-w-0 grow">
        <h2 class="truncate text-sm font-semibold text-highlighted">{{ title }}</h2>
        <p v-if="subtitle" class="truncate text-xs text-muted">{{ subtitle }}</p>
      </div>
      <UButton
        icon="i-mdi-close"
        size="xs"
        color="neutral"
        variant="ghost"
        aria-label="Close panel"
        @click="emit('close')"
      />
    </header>

    <div class="flex min-h-0 grow flex-col p-2">
      <slot />
    </div>
  </aside>
</template>

<script setup lang="ts">
const props = withDefaults(defineProps<{
  title: string
  subtitle?: string
  /** localStorage key the width is remembered under; omit to not remember it. */
  storageKey?: string
  defaultWidth?: number
}>(), { defaultWidth: 520 })

const emit = defineEmits<{ close: [] }>()

/**
 * Floor, not a taste call: the rail and the panel's 66px of rank labels are
 * fixed, so below this the detail's plot area stops being wide enough to read.
 */
const MIN = 420
const MAX = 1000
/** Left for the map at any width — the dock may not squeeze it out entirely. */
const KEEP = 380
const STEP = 24

const width = ref(props.defaultWidth)
const dragging = ref(false)

function clamp(w: number): number {
  const max = Math.min(MAX, Math.max(MIN, window.innerWidth - KEEP))
  return Math.min(Math.max(w, MIN), max)
}

function remember() {
  if (props.storageKey) localStorage.setItem(props.storageKey, String(Math.round(width.value)))
}

function nudge(delta: number) {
  width.value = clamp(width.value + delta)
  remember()
}

let stopDrag: (() => void) | null = null

function startDrag(event: PointerEvent) {
  event.preventDefault()
  dragging.value = true
  // The dock is flush to the right edge, so the pointer's distance from that
  // edge *is* the width.
  const onMove = (e: PointerEvent) => { width.value = clamp(window.innerWidth - e.clientX) }
  const onUp = () => { stopDrag?.() }
  stopDrag = () => {
    dragging.value = false
    window.removeEventListener('pointermove', onMove)
    window.removeEventListener('pointerup', onUp)
    stopDrag = null
    remember()
  }
  window.addEventListener('pointermove', onMove)
  window.addEventListener('pointerup', onUp)
}

function onWindowResize() { width.value = clamp(width.value) }

onMounted(() => {
  const saved = props.storageKey ? Number(localStorage.getItem(props.storageKey)) : NaN
  width.value = clamp(Number.isFinite(saved) && saved > 0 ? saved : props.defaultWidth)
  window.addEventListener('resize', onWindowResize)
})

onBeforeUnmount(() => {
  stopDrag?.()
  window.removeEventListener('resize', onWindowResize)
})
</script>
