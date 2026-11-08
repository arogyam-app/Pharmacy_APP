<script setup>
/**
 * Order book — what to buy, worked out from what has been sold.
 *
 * Each medicine is measured against the stock it held right after its last
 * delivery, so the threshold scales with how much of it the shop actually
 * buys: 60 % of a hundred strips and 60 % of ten both trigger at the right
 * moment, without anyone maintaining a number per medicine.
 */
import { computed, onMounted, ref } from 'vue'

import client from '@/api/client'
import { useAuthStore } from '@/stores/auth'
import { listSuppliers } from '@/api/purchases'
import {
  listOrderBook,
  orderBookExportUrl,
  recomputeOrderBook,
  updateOrderBookEntry,
} from '@/api/stockwatch'

const rupees = new Intl.NumberFormat('en-IN', {
  style: 'currency',
  currency: 'INR',
  minimumFractionDigits: 2,
})
const money = (v) => (v === null || v === undefined ? '—' : rupees.format(Number(v)))
const trim = (v) => Number(v).toLocaleString('en-IN', { maximumFractionDigits: 3 })

const TRIGGER_LABEL = {
  STOCK_DECLINE_60: 'Stock declined',
  BELOW_REORDER_LEVEL: 'Below reorder level',
  OUT_OF_STOCK: 'Out of stock',
  MANUAL: 'Added by hand',
}

const auth = useAuthStore()
// The order book can be handed to an executive; the money on it cannot.
// The server already omits these fields — this stops the columns
// rendering as empty ones (CLAUDE.md rule 4).
const showCost = computed(() => auth.isAdmin)

const rows = ref([])
const suppliers = ref([])
const status = ref('OPEN')
const loading = ref(true)
const error = ref(null)
const notice = ref(null)
const busy = ref(false)

async function load() {
  loading.value = true
  error.value = null
  try {
    rows.value = (await listOrderBook({ status: status.value, page_size: 200 })).items
  } catch (err) {
    error.value = err.message
  } finally {
    loading.value = false
  }
}

/** Grouped by supplier, because that is how an order is actually placed. */
const grouped = computed(() => {
  const groups = new Map()
  for (const row of rows.value) {
    const key = row.preferred_supplier_name || '— no supplier on file —'
    if (!groups.has(key)) groups.set(key, [])
    groups.get(key).push(row)
  }
  return [...groups.entries()].map(([supplier, items]) => ({
    supplier,
    items,
    value: items.reduce((sum, i) => sum + Number(i.estimated_value || 0), 0),
  }))
})

const totalValue = computed(() =>
  rows.value.reduce((sum, r) => sum + Number(r.estimated_value || 0), 0),
)

async function recompute() {
  busy.value = true
  error.value = null
  try {
    const result = await recomputeOrderBook()
    notice.value = result.message
    await load()
  } catch (err) {
    error.value = err.message
  } finally {
    busy.value = false
  }
}

async function setQty(row, value) {
  const packs = Number(value)
  if (!Number.isFinite(packs) || packs <= 0) return
  await patch(row, { suggested_qty_packs: String(packs) })
}

async function setSupplier(row, supplierId) {
  // A key is a UUID string. The empty option still has to become null.
  await patch(row, { preferred_supplier_id: supplierId || null })
}

async function setStatus(row, next) {
  await patch(row, { status: next })
}

async function patch(row, payload) {
  error.value = null
  try {
    const saved = await updateOrderBookEntry(row.id, payload)
    const index = rows.value.findIndex((r) => r.id === row.id)
    // A status change moves it out of the current filter.
    if (saved.status !== status.value) rows.value.splice(index, 1)
    else rows.value.splice(index, 1, saved)
  } catch (err) {
    error.value = err.message
    await load()
  }
}

/** The export needs the auth header, so it is fetched rather than linked. */
async function download() {
  error.value = null
  try {
    const response = await client.get(orderBookExportUrl(status.value), {
      responseType: 'blob',
    })
    const url = URL.createObjectURL(response.data)
    const link = document.createElement('a')
    link.href = url
    link.download = `order-book-${new Date().toISOString().slice(0, 10)}.csv`
    link.click()
    URL.revokeObjectURL(url)
  } catch (err) {
    error.value = err.message
  }
}

onMounted(async () => {
  await load()
  try {
    suppliers.value = (await listSuppliers({ is_active: true, page_size: 200 })).items
  } catch {
    suppliers.value = []
  }
})
</script>

<template>
  <section class="stack">
    <div class="card stack">
      <h1>Order book</h1>
      <p class="muted">
        Raised automatically when a medicine's stock falls far enough below what it held
        after its last delivery, or past its reorder level. Suggested quantities are whole
        packs, because that is how distributors sell.
      </p>
    </div>

    <div class="card stack">
      <div class="toolbar">
        <select v-model="status" @change="load">
          <option value="OPEN">Open</option>
          <option value="ORDERED">Ordered</option>
          <option value="RECEIVED">Received</option>
          <option value="CANCELLED">Cancelled</option>
        </select>
        <button class="btn" :disabled="busy" @click="recompute">
          {{ busy ? 'Recomputing…' : 'Recompute' }}
        </button>
        <button class="btn" :disabled="!rows.length" @click="download">Export CSV</button>
        <span v-if="rows.length" class="muted small">
          {{ rows.length }} medicine(s)<template v-if="showCost"> · about
          {{ money(totalValue) }} at last cost</template>
        </span>
      </div>

      <p v-if="error" class="alert alert--error">{{ error }}</p>
      <p v-if="notice" class="alert alert--ok">{{ notice }}</p>
      <p v-if="loading" class="muted">Loading…</p>
      <p v-else-if="!rows.length" class="muted">
        Nothing {{ status.toLowerCase() }}. Recompute re-checks the whole catalogue.
      </p>
    </div>

    <!-- one card per supplier: this is how the order is actually placed -->
    <div v-for="group in grouped" :key="group.supplier" class="card stack">
      <div class="card__head">
        <h2>{{ group.supplier }}</h2>
        <span class="muted small">
          {{ group.items.length }} line(s) · about {{ money(group.value) }}
        </span>
      </div>

      <div class="table-wrap">
        <table class="data">
          <thead>
            <tr>
              <th>Medicine</th>
              <th>Why</th>
              <th class="numeric">In stock</th>
              <th class="numeric">Baseline</th>
              <th class="numeric">Monthly use</th>
              <th class="numeric">Cover</th>
              <th class="numeric">Order (packs)</th>
              <th v-if="showCost" class="numeric">Est. value</th>
              <th>Supplier</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="row in group.items" :key="row.id">
              <td>
                <strong>{{ row.medicine_name }}</strong>
                <p class="sub">{{ row.pack_label }}</p>
              </td>
              <td>
                <span
                  :class="[
                    'badge',
                    row.trigger_reason === 'OUT_OF_STOCK' ? 'badge--danger' : 'badge--warn',
                  ]"
                >
                  {{ TRIGGER_LABEL[row.trigger_reason] || row.trigger_reason }}
                </span>
                <p v-if="Number(row.decline_pct) > 0" class="sub">
                  down {{ row.decline_pct }}%
                </p>
              </td>
              <td class="numeric">{{ trim(row.available_units_now ?? row.available_units_at_trigger) }}</td>
              <td class="numeric">{{ trim(row.baseline_units) }}</td>
              <td class="numeric">{{ trim(row.avg_monthly_consumption_units) }}</td>
              <td class="numeric">
                {{ row.days_of_cover === null ? '—' : `${row.days_of_cover}d` }}
              </td>
              <td class="numeric">
                <input
                  class="qty"
                  type="number"
                  min="1"
                  step="1"
                  :value="Number(row.suggested_qty_packs)"
                  :disabled="status !== 'OPEN' && status !== 'ORDERED'"
                  @change="setQty(row, $event.target.value)"
                />
                <p class="sub">{{ trim(row.suggested_qty_units) }} units</p>
              </td>
              <td v-if="showCost" class="numeric">{{ money(row.estimated_value) }}</td>
              <td>
                <select
                  :value="row.preferred_supplier_id || ''"
                  :disabled="status === 'RECEIVED' || status === 'CANCELLED'"
                  @change="setSupplier(row, $event.target.value)"
                >
                  <option value="">— none —</option>
                  <option v-for="s in suppliers" :key="s.id" :value="s.id">{{ s.name }}</option>
                </select>
              </td>
              <td class="numeric actions">
                <button
                  v-if="row.status === 'OPEN'"
                  class="btn btn--sm btn--primary"
                  @click="setStatus(row, 'ORDERED')"
                >
                  Mark ordered
                </button>
                <button
                  v-if="row.status === 'OPEN' || row.status === 'ORDERED'"
                  class="btn btn--sm btn--danger"
                  @click="setStatus(row, 'CANCELLED')"
                >
                  Drop
                </button>
                <span v-if="row.status === 'RECEIVED'" class="muted small">
                  Received on invoice {{ row.purchase_invoice_id }}
                </span>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>
  </section>
</template>

<style scoped>
.card__head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--sp-2);
  flex-wrap: wrap;
}

.card__head h2 {
  margin: 0;
}

.sub {
  margin: 0.1rem 0 0;
  font-size: 0.78rem;
  color: var(--text-muted);
  white-space: normal;
}

.qty {
  width: 100%;
  max-width: 4.5rem;
  text-align: right;
}

.actions {
  white-space: nowrap;
}

.actions button + button {
  margin-left: 0.3rem;
}
</style>
