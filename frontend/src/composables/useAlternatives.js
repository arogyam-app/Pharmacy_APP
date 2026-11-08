import { ref } from 'vue'

import { fetchAlternatives } from '@/api/catalog'

/** How long a pointer must rest on a row before the panel appears. */
const HOVER_DELAY = 1000

/**
 * Waiting this long after the pointer leaves keeps the panel up while it
 * travels from the row into the panel itself. Without it the panel closes
 * under the cursor on its way there and cannot be clicked.
 */
const CLOSE_GRACE = 220

/**
 * Same-salt alternatives, shown on a deliberate hover.
 *
 * Substitution is a glance, not a task: the operator wants to know whether a
 * cheaper equivalent exists *while* looking at a row, and a panel that
 * appeared on every passing mouse movement would be noise. A full second of
 * resting on one row is the difference between reading it and sweeping past.
 *
 * Results are cached per medicine, because moving back and forth between two
 * rows is exactly what comparing them looks like, and re-fetching each time
 * would make the panel flicker.
 */
export function useAlternatives() {
  const open = ref(false)
  const medicine = ref(null)
  const rows = ref([])
  const loading = ref(false)
  const anchor = ref({ x: 0, y: 0 })

  const cache = new Map()
  let openTimer = null
  let closeTimer = null

  function cancelTimers() {
    clearTimeout(openTimer)
    clearTimeout(closeTimer)
    openTimer = closeTimer = null
  }

  async function load(item) {
    if (cache.has(item.id)) {
      rows.value = cache.get(item.id)
      return
    }
    loading.value = true
    try {
      const found = await fetchAlternatives(item.id)
      cache.set(item.id, found)
      // The pointer may have moved on while this was in flight; only the
      // row still being hovered gets to write into the panel.
      if (medicine.value?.id === item.id) rows.value = found
    } catch {
      if (medicine.value?.id === item.id) rows.value = []
    } finally {
      loading.value = false
    }
  }

  /**
   * Call from `@mouseenter` / `@mousemove` on a row.
   *
   * `event` positions the panel; without it the caller is responsible for
   * setting `anchor` itself.
   */
  function hover(item, event) {
    if (!item) return
    cancelTimers()
    if (event) anchor.value = { x: event.clientX, y: event.clientY }

    // Already showing this row: keep it up rather than restarting the wait.
    if (open.value && medicine.value?.id === item.id) return

    openTimer = setTimeout(() => {
      medicine.value = item
      rows.value = cache.get(item.id) || []
      open.value = true
      load(item)
    }, HOVER_DELAY)
  }

  /** Call from `@mouseleave` on a row, and on the panel itself. */
  function leave() {
    clearTimeout(openTimer)
    openTimer = null
    closeTimer = setTimeout(close, CLOSE_GRACE)
  }

  /** Pointer moved into the panel — it stays until it leaves again. */
  function hold() {
    clearTimeout(closeTimer)
    closeTimer = null
  }

  function close() {
    cancelTimers()
    open.value = false
    medicine.value = null
    rows.value = []
  }

  /**
   * Show it now, without the wait.
   *
   * Hover is unreachable on a touchscreen and by keyboard, so every caller
   * also wires a click or a focus to this.
   */
  function show(item, event) {
    cancelTimers()
    if (event) anchor.value = { x: event.clientX, y: event.clientY }
    medicine.value = item
    rows.value = cache.get(item.id) || []
    open.value = true
    load(item)
  }

  return { open, medicine, rows, loading, anchor, hover, leave, hold, close, show }
}
