<script setup>
/**
 * Same-salt alternatives, floating beside the row being looked at.
 *
 * Two medicines are substitutes exactly when they share a composition, so
 * this is the panel that answers "is there a cheaper one?" — cheapest per
 * unit first, because that is the whole point of asking. Out-of-stock and
 * unpriced brands sort last: a substitute you cannot hand over is not one.
 *
 * It is a popover rather than a column of the page because it is read for a
 * second and dismissed, and a permanent panel spent that space on nothing
 * for the rest of the day.
 */
import { computed } from 'vue'

const props = defineProps({
  /** The medicine being compared against. */
  medicine: { type: Object, default: null },
  rows: { type: Array, default: () => [] },
  loading: { type: Boolean, default: false },
  /** Viewport coordinates to sit beside, usually the pointer. */
  anchor: { type: Object, required: true },
  /** Shown when a row can be added straight to the cart. */
  actionLabel: { type: String, default: '' },
})

const emit = defineEmits(['hold', 'leave', 'pick'])

const WIDTH = 420
const MAX_HEIGHT = 340
const GAP = 16

/**
 * Kept inside the viewport, and flipped to the other side of the pointer
 * when there is no room — a panel half off-screen is worse than none.
 */
const style = computed(() => {
  const { x, y } = props.anchor
  const vw = window.innerWidth
  const vh = window.innerHeight

  const left = x + GAP + WIDTH > vw ? Math.max(GAP, x - GAP - WIDTH) : x + GAP
  const top = Math.min(Math.max(GAP, y - 40), Math.max(GAP, vh - MAX_HEIGHT - GAP))

  return { left: `${left}px`, top: `${top}px`, width: `${WIDTH}px` }
})

const rupees = new Intl.NumberFormat('en-IN', {
  style: 'currency',
  currency: 'INR',
  minimumFractionDigits: 2,
})
const money = (v) => (v === null || v === undefined ? '—' : rupees.format(Number(v)))
const trim = (v) => Number(v).toLocaleString('en-IN', { maximumFractionDigits: 3 })

/** How much cheaper per unit, against the medicine being compared. */
function saving(alt) {
  const base = Number(props.medicine?.unit_mrp)
  const other = Number(alt.unit_mrp)
  if (!base || !other || other >= base) return null
  return `${(((base - other) / base) * 100).toFixed(0)}% cheaper`
}
</script>

<template>
  <div
    class="popover card"
    :style="style"
    role="dialog"
    aria-label="Same-salt alternatives"
    @mouseenter="emit('hold')"
    @mouseleave="emit('leave')"
  >
    <div class="popover__head">
      <strong>Same composition</strong>
      <span v-if="medicine" class="muted small">{{ medicine.brand_name }}</span>
    </div>
    <p v-if="medicine?.composition_text" class="composition">
      {{ medicine.composition_text }}
    </p>

    <p v-if="loading && !rows.length" class="muted small">Looking…</p>
    <p v-else-if="!rows.length" class="muted small">
      No other brand on file shares this composition.
    </p>

    <div v-else class="popover__rows">
      <button
        v-for="alt in rows"
        :key="alt.id"
        type="button"
        class="alt"
        :class="{ 'alt--out': !alt.in_stock }"
        @click="emit('pick', alt)"
      >
        <span class="alt__name">
          <strong>{{ alt.brand_name }}</strong>
          <span v-if="saving(alt)" class="badge badge--ok">{{ saving(alt) }}</span>
          <span v-if="!alt.in_stock" class="badge badge--warn">no stock</span>
        </span>
        <span class="muted small">{{ alt.manufacturer_name }} · {{ alt.pack_label }}</span>
        <span class="alt__figures">
          <span>{{ money(alt.mrp_per_pack) }} / pack</span>
          <span>{{ money(alt.unit_mrp) }} / unit</span>
          <span>{{ trim(alt.available_units) }} on hand</span>
        </span>
      </button>
    </div>

    <p v-if="actionLabel && rows.length" class="muted small">{{ actionLabel }}</p>
  </div>
</template>

<style scoped>
.popover {
  position: fixed;
  z-index: 60;
  max-height: 340px;
  overflow-y: auto;
  box-shadow: var(--shadow-lift);
  padding: var(--sp-3);
  display: flex;
  flex-direction: column;
  gap: 0.4rem;
}

.popover__head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 0.5rem;
}

.composition {
  margin: 0;
  font-size: 0.78rem;
  color: var(--text-muted);
}

.popover__rows {
  display: flex;
  flex-direction: column;
  gap: 0.2rem;
}

.alt {
  display: flex;
  flex-direction: column;
  gap: 0.1rem;
  width: 100%;
  text-align: left;
  font: inherit;
  background: none;
  border: 1px solid transparent;
  border-radius: var(--radius-sm);
  padding: 0.35rem 0.5rem;
  cursor: pointer;
}

.alt:hover {
  background: var(--accent-soft);
  border-color: var(--accent);
}

.alt--out {
  opacity: 0.6;
}

.alt__name {
  display: flex;
  align-items: baseline;
  gap: 0.35rem;
  flex-wrap: wrap;
}

.alt__figures {
  display: flex;
  gap: 0.75rem;
  font-size: 0.78rem;
  color: var(--text-muted);
}

.small {
  font-size: 0.8rem;
}
</style>
