<script setup>
/**
 * Alerts — what needs attention, in five buckets.
 *
 * The counter sees all of it: knowing a strip expires next month is what
 * lets the operator sell it before it becomes a loss. Cost figures are
 * admin-only, as everywhere else.
 */
import { computed, onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'

import {
  fetchAlertSummary,
  fetchExpired,
  fetchExpiring,
  fetchLowStock,
  fetchOutOfStock,
} from '@/api/stockwatch'
import { ItemType } from '@/constants'
import { useAuthStore } from '@/stores/auth'

const auth = useAuthStore()
const route = useRoute()

// Which half of the shop. `/medicine/alerts` and `/general/alerts` are this
// same screen, scoped by the route; the API takes the same filter.
const kind = computed(() => route.meta.itemType || '')
const isGeneral = computed(() => kind.value === ItemType.GENERAL)

const rupees = new Intl.NumberFormat('en-IN', {
  style: 'currency',
  currency: 'INR',
  minimumFractionDigits: 2,
})
const money = (v) => (v === null || v === undefined ? '—' : rupees.format(Number(v)))
const trim = (v) => Number(v).toLocaleString('en-IN', { maximumFractionDigits: 3 })

const summary = ref(null)
const rows = ref([])
const tab = ref('expiring')
const loading = ref(true)
const error = ref(null)

const tabs = computed(() => [
  { key: 'expiring', label: 'Near expiry', count: summary.value?.near_expiry },
  { key: 'expired', label: 'Expired', count: summary.value?.expired },
  { key: 'low', label: 'Low stock', count: summary.value?.low_stock },
  { key: 'out', label: 'Out of stock', count: summary.value?.out_of_stock },
])

const isBatchList = computed(() => tab.value === 'expiring' || tab.value === 'expired')

async function load() {
  loading.value = true
  error.value = null
  try {
    summary.value = await fetchAlertSummary(kind.value)
    const fetchers = {
      expiring: () => fetchExpiring(undefined, kind.value),
      expired: () => fetchExpired(kind.value),
      low: () => fetchLowStock(kind.value),
      out: () => fetchOutOfStock(kind.value),
    }
    rows.value = await fetchers[tab.value]()
  } catch (err) {
    error.value = err.message
  } finally {
    loading.value = false
  }
}

function select(key) {
  tab.value = key
  load()
}

onMounted(load)
</script>

<template>
  <section class="stack">
    <div class="card stack">
      <h1>{{ isGeneral ? 'Product alerts' : 'Medicine alerts' }}</h1>
      <p class="muted">
        Stock that needs a decision, on the {{ isGeneral ? 'general goods' : 'medicine' }}
        side of the shop. Near-expiry goods can still be sold — that is what the window is
        for; expired ones cannot, and show up as out of stock until they are written off.
      </p>
    </div>

    <p v-if="error" class="alert alert--error">{{ error }}</p>

    <div v-if="summary" class="grid-3">
      <div class="card tile">
        <span class="tile__label">Expired</span>
        <strong :class="['tile__value', summary.expired ? 'is-bad' : '']">
          {{ summary.expired }}
        </strong>
        <span class="muted small">{{ money(summary.expired_value) }} at retail</span>
      </div>
      <div class="card tile">
        <span class="tile__label">Near expiry</span>
        <strong :class="['tile__value', summary.near_expiry ? 'is-warn' : '']">
          {{ summary.near_expiry }}
        </strong>
        <span class="muted small">within {{ summary.near_expiry_days }} days</span>
      </div>
      <div class="card tile">
        <span class="tile__label">To reorder</span>
        <strong :class="['tile__value', summary.order_book_open ? 'is-warn' : '']">
          {{ summary.order_book_open }}
        </strong>
        <span class="muted small">
          <RouterLink v-if="auth.isAdmin" to="/order-book">open in the order book</RouterLink>
          <template v-else>open suggestion(s)</template>
        </span>
      </div>
    </div>

    <div class="card stack">
      <div class="toolbar">
        <button
          v-for="t in tabs"
          :key="t.key"
          :class="['btn', 'btn--sm', tab === t.key ? 'btn--primary' : '']"
          @click="select(t.key)"
        >
          {{ t.label }}<template v-if="t.count !== undefined"> · {{ t.count }}</template>
        </button>
      </div>

      <p v-if="loading" class="muted">Loading…</p>
      <p v-else-if="!rows.length" class="muted">Nothing in this bucket — good.</p>

      <!-- batch-level: expired and near expiry -->
      <div v-else-if="isBatchList" class="table-wrap">
        <table class="data">
          <thead>
            <tr>
              <th>Medicine</th>
              <th>Batch</th>
              <th>Expiry</th>
              <th class="numeric">Days</th>
              <th class="numeric">Units</th>
              <th class="numeric">Packs</th>
              <th class="numeric">At MRP</th>
              <th v-if="auth.isAdmin" class="numeric">At cost</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="row in rows" :key="row.batch_id">
              <td>{{ row.medicine_name }}</td>
              <td>{{ row.batch_no }}</td>
              <td>{{ row.expiry_date }}</td>
              <td class="numeric">
                <span
                  :class="[
                    'badge',
                    row.days_to_expiry < 0
                      ? 'badge--danger'
                      : row.days_to_expiry <= 30
                        ? 'badge--warn'
                        : 'badge--ok',
                  ]"
                >
                  {{ row.days_to_expiry < 0 ? `${-row.days_to_expiry}d ago` : `${row.days_to_expiry}d` }}
                </span>
              </td>
              <td class="numeric">{{ trim(row.qty_units) }}</td>
              <td class="numeric">{{ trim(row.qty_packs) }}</td>
              <td class="numeric">{{ money(row.mrp_value) }}</td>
              <td v-if="auth.isAdmin" class="numeric">{{ money(row.cost_value) }}</td>
            </tr>
          </tbody>
        </table>
      </div>

      <!-- medicine-level: low and out of stock -->
      <div v-else class="table-wrap">
        <table class="data">
          <thead>
            <tr>
              <th>Medicine</th>
              <th>Pack</th>
              <th>Rack</th>
              <th class="numeric">Available</th>
              <th class="numeric">Packs</th>
              <th class="numeric">Reorder at</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="row in rows" :key="row.medicine_id">
              <td>{{ row.brand_name }}</td>
              <td>{{ row.pack_label }}</td>
              <td>{{ row.rack_location || '—' }}</td>
              <td class="numeric">
                {{ trim(row.available_units) }}
                <span v-if="!Number(row.available_units)" class="badge badge--danger">Out</span>
              </td>
              <td class="numeric">{{ trim(row.available_packs) }}</td>
              <td class="numeric">{{ trim(row.reorder_level_units) }}</td>
            </tr>
          </tbody>
        </table>
      </div>

      <p v-if="tab === 'expired' && rows.length && auth.isAdmin" class="muted small">
        Expired stock is written off or sent back to the supplier from the
        <RouterLink :to="isGeneral ? '/general/expiry' : '/medicine/expiry'">expiry register</RouterLink>.
      </p>
    </div>
  </section>
</template>

<style scoped>
.tile {
  display: flex;
  flex-direction: column;
  gap: 0.1rem;
}

.tile__label {
  font-size: 0.75rem;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.05em;
  color: var(--text-muted);
}

.tile__value {
  font-size: 1.8rem;
  line-height: 1.1;
  font-variant-numeric: tabular-nums;
}

.is-bad {
  color: var(--danger);
}

.is-warn {
  color: var(--warn);
}
</style>
