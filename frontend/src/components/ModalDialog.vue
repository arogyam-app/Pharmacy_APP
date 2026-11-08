<script setup>
/**
 * The shell every dialog in the app shares: a dimmed backdrop, a card, and
 * the two ways out that people expect — Escape, and clicking outside it.
 *
 * Factored out because three screens grew their own copy of the same
 * backdrop markup, and the copies had already started to differ on whether
 * Escape closed them.
 */
import { onMounted, onUnmounted, ref } from 'vue'

defineProps({
  title: { type: String, required: true },
  /** Wider for a form with a full field grid. */
  wide: { type: Boolean, default: false },
})

const emit = defineEmits(['close'])

const root = ref(null)

/**
 * Escape closes the **topmost** dialog only.
 *
 * Dialogs nest here: the new-composition dialog opens on top of the
 * add-medicine one. Without this check a single Escape would close both and
 * take a half-filled medicine form with it. Any element with `role="dialog"`
 * counts, so a dialog that does not use this shell is still respected.
 */
function onKey(event) {
  if (event.key !== 'Escape') return
  const open = document.querySelectorAll('[role="dialog"]')
  if (open.length && open[open.length - 1] !== root.value) return
  emit('close')
}

onMounted(() => document.addEventListener('keydown', onKey))
onUnmounted(() => document.removeEventListener('keydown', onKey))
</script>

<template>
  <div class="backdrop" @click.self="emit('close')">
    <div
      ref="root"
      :class="['dialog', 'card', 'stack', { 'dialog--wide': wide }]"
      role="dialog"
      aria-modal="true"
      :aria-label="title"
    >
      <div class="dialog__head">
        <h2 class="grow">{{ title }}</h2>
        <button class="dialog__x" type="button" aria-label="Close" @click="emit('close')">×</button>
      </div>
      <slot />
    </div>
  </div>
</template>

<style scoped>
.backdrop {
  position: fixed;
  inset: 0;
  background: rgba(16, 24, 40, 0.45);
  display: grid;
  place-items: center;
  padding: 1.5rem;
  z-index: 50;
}

.dialog {
  width: min(620px, 100%);
  max-height: 90vh;
  overflow-y: auto;
}

.dialog--wide {
  width: min(900px, 100%);
}

.dialog__head {
  display: flex;
  align-items: center;
  gap: 0.75rem;
}

.dialog__head h2 {
  margin: 0;
}

.grow {
  flex: 1;
}

.dialog__x {
  font: inherit;
  font-size: 1.4rem;
  line-height: 1;
  background: none;
  border: 0;
  color: var(--text-muted);
  cursor: pointer;
  padding: 0 0.25rem;
}

.dialog__x:hover {
  color: var(--text);
}
</style>
