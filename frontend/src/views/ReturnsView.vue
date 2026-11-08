<script setup>
/**
 * Returns — goods coming back against an invoice.
 *
 * Always against a specific invoice and its lines, never free-standing:
 * that is what caps the quantity at what was actually sold and fixes the
 * refund at the price actually charged.
 *
 * Arriving from the register with ?sale= jumps straight to that invoice.
 */
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { useRoute } from 'vue-router'

import { fetchSaleByInvoice, listSales } from '@/api/pos'
import { createReturn, fetchReturnable, listReturns } from '@/api/returns'
import { useDebounced } from '@/composables/useDebounced'

const route = useRoute()

const rupees = new Intl.NumberFormat('en-IN', {
  style: 'currency',
  currency: 'INR',
  minimumFractionDigits: 2,
})
const money = (v) => (v === null || v === undefined ? '—' : rupees.format(Number(v)))
const trim = (v) => Number(v).toLocaleString('en-IN', { maximumFractionDigits: 3 })

// --- find the invoice -----------------------------------------------------
const invoiceQuery = ref('')
const debouncedInvoice = useDebounced(invoiceQuery, 250)
const matches = ref([])
const error = ref(null)
const notice = ref(null)

watch(debouncedInvoice, async (value) => {
  const term = value.trim()
  if (term.length < 2) {
    matches.value = []
    return
  }
  try {
    matches.value = (await listSales({ q: term, page_size: 8 })).items
  } catch (err) {
    error.value = err.message
  }
})

// --- the invoice being returned against ----------------------------------
const sheet = ref(null)          // the returnable payload
const lines = reactive({})       // sale_item_id -> {qty, condition}
const refundMode = ref('CREDIT_NOTE')
const reason = ref('')
const saving = ref(false)
const result = ref(null)

async function openSale(saleId) {
  error.value = null
  notice.value = null
  result.value = null
  try {
    sheet.value = await fetchReturnable(saleId)
    matches.value = []
    invoiceQuery.value = sheet.value.invoice_no
    for (const key of Object.keys(lines)) delete lines[key]
    for (const line of sheet.value.items) {
      lines[line.sale_item_id] = { qty: '', condition: 'SALEABLE' }
    }
  } catch (err) {
    error.value = err.message
    sheet.value = null
  }
}

async function openByNumber() {
  const term = invoiceQuery.value.trim()
  if (!term) return
  try {
    const sale = await fetchSaleByInvoice(term)
    await openSale(sale.id)
  } catch (err) {
    error.value = err.message
  }
}

function setAll(line) {
  lines[line.sale_item_id].qty = String(Number(line.qty_returnable_units))
}

/** Only lines with a quantity are sent. */
const chosen = computed(() =>
  (sheet.value?.items || [])
    .map((line) => ({ line, entry: lines[line.sale_item_id] }))
    .filter(({ entry }) => Number(entry?.qty) > 0),
)

/**
 * Preview of the refund, worked out the same way the server does: the
 * line's own post-discount value, pro-rata by quantity. The server
 * recomputes it and its figure is the one paid.
 */
const refundPreview = computed(() =>
  chosen.value.reduce((sum, { line, entry }) => {
    const gross = Number(line.unit_mrp) * Number(entry.qty)
    const net = gross - (gross * Number(line.discount_pct)) / 100
    return sum + Math.round((net + Number.EPSILON) * 100) / 100
  }, 0),
)

const hasUnsaleable = computed(() =>
  chosen.value.some(({ entry }) => entry.condition !== 'SALEABLE'),
)

async function submit() {
  if (!chosen.value.length) {
    error.value = 'Enter a quantity against at least one line.'
    return
  }
  saving.value = true
  error.value = null
  try {
    result.value = await createReturn({
      sale_id: sheet.value.sale_id,
      refund_mode: refundMode.value,
      reason: reason.value.trim() || null,
      items: chosen.value.map(({ line, entry }) => ({
        sale_item_id: line.sale_item_id,
        qty_units: String(entry.qty),
        condition: entry.condition,
      })),
    })
    notice.value = `Return ${result.value.return_no} recorded — ${money(
      result.value.total_refund_amount,
    )} refunded.`
    await openSale(sheet.value.sale_id)   // refresh what is left
    await loadRecent()
  } catch (err) {
    error.value = err.message
  } finally {
    saving.value = false
  }
}

// --- recent returns -------------------------------------------------------
const recent = ref([])

async function loadRecent() {
  try {
    recent.value = (await listReturns({ page_size: 15 })).items
  } catch {
    recent.value = []
  }
}

onMounted(async () => {
  await loadRecent()
  if (route.query.sale) await openSale(Number(route.query.sale))
})
</script>

<template>
  <section class="stack">
    <div class="card stack">
      <h1>Returns</h1>
      <p class="muted">
        Always against an invoice: that is what limits the quantity to what was sold and
        fixes the refund at the price charged. Damaged or expired goods are refunded but
        never go back on sale.
      </p>
    </div>

    <div class="card stack">
      <div class="toolbar">
        <input
          v-model="invoiceQuery"
          class="grow"
          type="search"
          placeholder="Invoice number or mobile…"
          @keydown.enter.prevent="openByNumber"
        />
        <button class="btn" @click="openByNumber">Open</button>
      </div>

      <ul v-if="matches.length" class="matches">
        <li v-for="row in matches" :key="row.id">
          <button type="button" @click="openSale(row.id)">
            <strong>{{ row.invoice_no }}</strong>
            <span class="muted small">
              {{ row.invoice_date }} · {{ row.customer_name || 'Walk-in' }}
              <template v-if="row.customer_mobile"> · {{ row.customer_mobile }}</template>
              · {{ money(row.net_amount) }} · {{ row.status }}
            </span>
          </button>
        </li>
      </ul>

      <p v-if="error" class="alert alert--error">{{ error }}</p>
      <p v-if="notice" class="alert alert--ok">{{ notice }}</p>
    </div>

    <!-- the invoice, line by line -->
    <div v-if="sheet" class="card stack">
      <div class="card__head">
        <h2>Invoice {{ sheet.invoice_no }}</h2>
        <span :class="['badge', sheet.within_window ? 'badge--ok' : 'badge--warn']">
          {{ sheet.days_since_sale }} day(s) old
        </span>
      </div>
      <p class="muted small">
        Sold {{ sheet.invoice_date }}<template v-if="sheet.customer_mobile"> ·
        {{ sheet.customer_mobile }}</template>
      </p>

      <p v-if="!sheet.within_window" class="alert alert--warn">
        Past the {{ sheet.return_window_days }}-day return window — an administrator has to
        take this one.
      </p>
      <p v-if="!sheet.can_return" class="alert alert--warn">
        Nothing left to return on this invoice.
      </p>

      <div class="table-wrap">
        <table class="data">
          <thead>
            <tr>
              <th>Item</th>
              <th>Batch</th>
              <th class="numeric">Sold</th>
              <th class="numeric">Returned</th>
              <th class="numeric">Can return</th>
              <th class="numeric">Return qty</th>
              <th>Condition</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="line in sheet.items" :key="line.sale_item_id">
              <td>{{ line.medicine_name }}</td>
              <td>
                {{ line.batch_no }}
                <small class="muted"><br />exp {{ line.expiry_date }}</small>
              </td>
              <td class="numeric">{{ trim(line.qty_sold_units) }}</td>
              <td class="numeric">{{ trim(line.qty_returned_units) }}</td>
              <td class="numeric">
                {{ trim(line.qty_returnable_units) }}
                <button
                  v-if="Number(line.qty_returnable_units) > 0"
                  class="btn btn--sm"
                  @click="setAll(line)"
                >
                  All
                </button>
              </td>
              <td class="numeric">
                <input
                  v-model="lines[line.sale_item_id].qty"
                  class="qty"
                  type="number"
                  min="0"
                  step="1"
                  :max="Number(line.qty_returnable_units)"
                  :disabled="Number(line.qty_returnable_units) <= 0"
                />
              </td>
              <td>
                <select
                  v-model="lines[line.sale_item_id].condition"
                  :disabled="Number(line.qty_returnable_units) <= 0"
                >
                  <option value="SALEABLE">Saleable</option>
                  <option value="DAMAGED">Damaged</option>
                  <option value="EXPIRED">Expired</option>
                </select>
              </td>
            </tr>
          </tbody>
        </table>
      </div>

      <div class="form-grid">
        <label class="field">
          <span>Refund as</span>
          <select v-model="refundMode">
            <option value="CREDIT_NOTE">Credit note</option>
            <option value="CASH">Cash</option>
            <option value="CARD_REVERSAL">Card reversal</option>
          </select>
          <p v-if="refundMode === 'CREDIT_NOTE' && !sheet.customer_id" class="field__hint">
            This invoice has no customer on file, so a credit note cannot be issued —
            refund cash instead.
          </p>
        </label>
        <label class="field field--wide">
          <span>Reason</span>
          <input v-model="reason" placeholder="Why are the goods coming back?" />
        </label>
      </div>

      <p v-if="hasUnsaleable" class="alert alert--warn">
        Damaged and expired lines are refunded but do not go back on the shelf.
      </p>

      <div class="form-actions">
        <button
          class="btn btn--primary"
          :disabled="saving || !chosen.length"
          @click="submit"
        >
          {{ saving ? 'Saving…' : `Refund ${money(refundPreview)}` }}
        </button>
        <span v-if="chosen.length" class="muted small">
          {{ chosen.length }} line(s) · the server recomputes the exact figure
        </span>
      </div>
    </div>

    <!-- what just happened -->
    <div v-if="result" class="card stack">
      <h2>Return {{ result.return_no }}</h2>
      <p v-for="(w, i) in result.warnings" :key="i" class="alert alert--warn">{{ w }}</p>
      <dl class="kv">
        <dt>Refunded</dt><dd>{{ money(result.total_refund_amount) }}</dd>
        <dt>Taxable</dt><dd>{{ money(result.taxable_amount) }}</dd>
        <dt>Tax reversed</dt>
        <dd>
          {{ money(Number(result.cgst_amount) + Number(result.sgst_amount) + Number(result.igst_amount)) }}
        </dd>
        <dt>Settled as</dt><dd>{{ result.refund_mode.replace('_', ' ').toLowerCase() }}</dd>
        <dt v-if="result.credit_note_no">Credit note</dt>
        <dd v-if="result.credit_note_no">{{ result.credit_note_no }}</dd>
      </dl>
    </div>

    <!-- recent returns -->
    <div class="card stack">
      <h2>Recent returns</h2>
      <p v-if="!recent.length" class="muted">No returns recorded yet.</p>
      <div v-else class="table-wrap">
        <table class="data">
          <thead>
            <tr>
              <th>Return</th>
              <th>Date</th>
              <th>Invoice</th>
              <th>Customer</th>
              <th class="numeric">Refund</th>
              <th>Settled</th>
              <th>Credit note</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="row in recent" :key="row.id">
              <td><strong>{{ row.return_no }}</strong></td>
              <td>{{ row.return_date }}</td>
              <td>{{ row.invoice_no }}</td>
              <td>{{ row.customer_mobile || 'Walk-in' }}</td>
              <td class="numeric">{{ money(row.total_refund_amount) }}</td>
              <td>{{ row.refund_mode.replace('_', ' ').toLowerCase() }}</td>
              <td>{{ row.credit_note_no || '—' }}</td>
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

.field--wide {
  grid-column: 1 / -1;
}

.qty {
  width: 100%;
  max-width: 5rem;
  text-align: right;
}

.matches {
  list-style: none;
  margin: 0;
  padding: 0;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  max-height: 14rem;
  overflow-y: auto;
}

.matches li + li {
  border-top: 1px solid var(--border);
}

.matches button {
  display: flex;
  flex-direction: column;
  width: 100%;
  text-align: left;
  background: none;
  border: 0;
  font: inherit;
  padding: 0.4rem 0.7rem;
  cursor: pointer;
}

.matches button:hover {
  background: var(--accent-soft);
}
</style>
