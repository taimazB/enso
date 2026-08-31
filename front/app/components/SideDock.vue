<template>
  <aside
    class="relative flex min-h-0 shrink-0 flex-col bg-default"
    :class="side === 'left' ? 'border-r border-default' : 'border-l border-default'"
    :style="{ width: `${width}px` }"
  >
    <!-- The dock and the map compete for the same width, and which one wins
         depends on what the user is doing — so it is a drag, not a constant.
         Arrow keys move it too: the handle is the only control here that a
         pointer-only implementation would put out of reach. The key that widens
         is the one pointing away from the map, whichever edge the dock is on. -->
    <div
      role="separator"
      aria-orientation="vertical"
      :aria-label="`Resize ${label}`"
      tabindex="0"
      class="absolute inset-y-0 z-20 w-2 cursor-col-resize touch-none transition-colors hover:bg-primary/40 focus-visible:bg-primary/40 focus-visible:outline-none"
      :class="[side === 'left' ? '-right-1' : '-left-1', dragging ? 'bg-primary/40' : '']"
      @pointerdown="startDrag"
      @keydown.left.prevent="nudge(side === 'left' ? -STEP : STEP)"
      @keydown.right.prevent="nudge(side === 'left' ? STEP : -STEP)"
    />

    <header
      v-if="title || $slots.header"
      class="flex shrink-0 items-start gap-2 border-b border-default py-2 pl-3 pr-2"
    >
      <div class="min-w-0 grow">
        <slot name="header">
          <h2 class="truncate text-sm font-semibold text-highlighted">{{ title }}</h2>
          <p v-if="subtitle" class="truncate text-xs text-muted">{{ subtitle }}</p>
        </slot>
      </div>
      <UButton
        v-if="closable"
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
  title?: string
  subtitle?: string
  /**
   * Which edge it is docked to. Only the handle's side and the drag arithmetic
   * differ — a left dock measures the pointer from 0, a right one from the
   * window's width — but getting either wrong makes the handle drag the wrong
   * way, which is why they are derived from one prop rather than duplicated.
   */
  side?: 'left' | 'right'
  /** localStorage key the width is remembered under; omit to not remember it. */
  storageKey?: string
  defaultWidth?: number
  /** Show the close button. A dock that is part of the page layout has none. */
  closable?: boolean
}>(), { defaultWidth: 520, side: 'right', closable: true })

const emit = defineEmits<{ close: [] }>()

/**
 * Floor, not a taste call: the ranks view's rail and its 66px of fixed rank
 * labels leave the detail plot unreadable below this. The numbers view is
 * comfortable well under it, but the floor cannot depend on which tab is open —
 * switching tabs would then resize the map.
 */
const MIN = 420
const MAX = 1000
/** Left for the map at any width — the dock may not squeeze it out entirely. */
const KEEP = 380
const STEP = 24

const width = ref(props.defaultWidth)
const dragging = ref(false)
const label = computed(() => props.title || 'panel')

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
  // The dock is flush to one edge, so the pointer's distance from that edge
  // *is* the width.
  const onMove = (e: PointerEvent) => {
    width.value = clamp(props.side === 'left' ? e.clientX : window.innerWidth - e.clientX)
  }
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
