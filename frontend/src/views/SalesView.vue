<script setup>
/**
 * Sales register — every invoice the shop has issued.
 *
 * Both roles see it: any staff member has to be able to find a customer's
 * bill. What a sales executive does not see is cost or margin on the detail,
 * and there is no delete anywhere — a mistake is corrected by a void or a
 * return, never by making the document disappear.
 */
import { computed, onMounted, reactive, ref, watch } from 'vue'

import { fetchSale, listSales } from '@/api/pos'
import { requestVoid } from '@/api/returns'
import { useDebounced } from '@/composables/useDebounced'
import { useAuthStore } from '@/stores/auth'

const auth = useAuthStore()

const rupees = new Intl.NumberFormat('en-IN', {
  style: 'currency',
  currency: 'INR',
  minimumFractionDigits: 2,
})
const money = (v) => (v === null || v === undefined ? '—' : rupees.format(Number(v)))
const trim = (v) => Number(v).toLocaleString('en-IN', { maximumFractionDigits: 3 })

const when = (iso) =>
  new Date(iso).toLocaleString('en-IN', {
    timeZone: 'Asia/Kolkata',
    dateStyle: 'medium',
    timeStyle: 'short',
  })

const rows = ref([])
const total = ref(0)
const page = ref(1)
const pageSize = 25
const loading = ref(true)
const error = ref(null)
const notice = ref(null)

const filters = reactive({ q: '', date_from: '', date_to: '', status: '' })
const debouncedQ = useDebounced(
  computed(() => filters.q),
  250,
)

async function load() {
  loading.value = true
  error.value = null
  try {
    const data = await listSales({
      q: filters.q.trim() || undefined,
      date_from: filters.date_from || undefined,
      date_to: filters.date_to || undefined,
      status: filters.status || undefined,
      page: page.value,
      page_size: pageSize,
    })
    rows.value = data.items
    total.value = data.total
  } catch (err) {
    error.value = err.message
  } finally {
    loading.value = false
  }
}

watch([debouncedQ, () => filters.date_from, () => filters.date_to, () => filters.status], () => {
  page.value = 1
  load()
})

function changePage(delta) {
  const next = page.value + delta
  if (next < 1 || (next - 1) * pageSize >= total.value) return
  page.value = next
  load()
}

// --- one invoice, opened where it sits ------------------------------------
//
// The detail used to live in a card underneath the whole table, which meant
// clicking row 20 sent you scrolling to read it and scrolling back to click
// the next one. It now unfolds directly beneath its own row and pushes the
// rest of the register down, so the invoice you clicked stays where your eye
// already is. Clicking the same row again folds it away.
const open = ref(null)
const openId = ref(null)
const loadingDetail = ref(false)

async function show(row) {
  if (openId.value === row.id) {
    openId.value = null
    open.value = null
    return
  }

  error.value = null
  openId.value = row.id
  open.value = null
  loadingDetail.value = true
  try {
    const detail = await fetchSale(row.id)
    // A second click while this was in flight wins — do not overwrite it.
    if (openId.value === row.id) open.value = detail
  } catch (err) {
    error.value = err.message
    openId.value = null
  } finally {
    loadingDetail.value = false
  }
}

/** Columns in the register, so the detail row can span the full width. */
const COLUMN_COUNT = 8

// --- the two ways back out of a sale -------------------------------------

/** Nothing comes back from a void invoice, and nothing that is fully returned. */
const canReturn = computed(
  () => open.value && !['VOIDED', 'RETURNED'].includes(open.value.status),
)

/** A sale with goods already returned cannot be voided: the stock would go
 *  back twice. The server refuses it too. */
const canRequestVoid = computed(
  () =>
    open.value &&
    !['VOIDED', 'VOID_REQUESTED', 'PARTIALLY_RETURNED', 'RETURNED'].includes(
      open.value.status,
    ),
)

async function askForVoid() {
  const why = prompt(
    `Request a void of invoice ${open.value.invoice_no}?\n\n` +
      'An administrator has to approve it. Approval returns every unit to its batch; ' +
      'the invoice number is kept and marked void.\n\nReason:',
  )
  if (!why || why.trim().length < 5) return

  error.value = null
  try {
    await requestVoid(open.value.id, why.trim())
    notice.value = `Void requested for ${open.value.invoice_no} — waiting for approval.`
    open.value = await fetchSale(open.value.id)
    await load()
  } catch (err) {
    error.value = err.message
  }
}

onMounted(load)
</script>

<template>
  <section class="stack">
    <div class="card stack">
      <h1>Sales register</h1>
      <p class="muted">
        Search by invoice number or mobile number. Invoices are never deleted — a mistake is
        corrected with a void or a return, both of which leave the original standing.
      </p>
    </div>

    <div class="card stack">
      <div class="form-actions">
        <input
          v-model="filters.q"
          class="grow"
          type="search"
          placeholder="Invoice number or mobile…"
        />
        <input v-model="filters.date_from" type="date" title="From" />
        <input v-model="filters.date_to" type="date" title="To" />
        <select v-model="filters.status">
          <option value="">All statuses</option>
          <option value="COMPLETED">Completed</option>
          <option value="VOID_REQUESTED">Void requested</option>
          <option value="VOIDED">Voided</option>
          <option value="PARTIALLY_RETURNED">Partly returned</option>
          <option value="RETURNED">Returned</option>
        </select>
      </div>

      <p v-if="error" class="alert alert--error">{{ error }}</p>
      <p v-if="notice" class="alert alert--ok">{{ notice }}</p>
      <p v-if="loading" class="muted">Loading…</p>
      <p v-else-if="!rows.length" class="muted">No invoices match.</p>

      <div v-else class="table-wrap">
        <table class="data">
          <thead>
            <tr>
              <th>Invoice</th>
              <th>When</th>
              <th>Customer</th>
              <th>Billed by</th>
              <th class="numeric">Items</th>
              <th class="numeric">Discount</th>
              <th class="numeric">Amount</th>
              <th>Status</th>
            </tr>
          </thead>
          <tbody>
            <template v-for="row in rows" :key="row.id">
              <tr
                :class="{ 'row--selected': openId === row.id }"
                :aria-expanded="openId === row.id"
                @click="show(row)"
              >
                <td>
                  <span class="caret" aria-hidden="true">{{ openId === row.id ? '▾' : '▸' }}</span>
                  <strong>{{ row.invoice_no }}</strong>
                  <span v-if="row.reprint_count" class="badge badge--warn">
                    {{ row.reprint_count }} print(s)
                  </span>
                </td>
                <td>{{ when(row.invoice_datetime) }}</td>
                <td>
                  {{ row.customer_name || 'Walk-in' }}
                  <small v-if="row.customer_mobile" class="muted"><br />{{ row.customer_mobile }}</small>
                </td>
                <td>{{ row.sales_user_name }}</td>
                <td class="numeric">{{ row.item_count }}</td>
                <td class="numeric">{{ money(row.discount_amount) }}</td>
                <td class="numeric">{{ money(row.net_amount) }}</td>
                <td>
                  <span
                    :class="['badge', row.status === 'COMPLETED' ? 'badge--ok' : 'badge--warn']"
                  >
                    {{ row.status }}
                  </span>
                </td>
              </tr>

              <!-- The invoice, unfolded under its own row. -->
              <tr v-if="openId === row.id" class="row--detail">
                <td :colspan="COLUMN_COUNT">
                  <p v-if="loadingDetail" class="muted">Loading…</p>

                  <div v-else-if="open" class="detail stack">
                    <div class="bar">
                      <div class="form-actions">
                        <RouterLink
                          class="btn btn--sm btn--primary"
                          :to="{ name: 'invoice', params: { id: open.id } }"
                        >
                          View &amp; print
                        </RouterLink>
                        <!-- The two ways back out of a sale; neither deletes anything. -->
                        <RouterLink
                          v-if="canReturn"
                          class="btn btn--sm"
                          :to="{ name: 'returns', query: { sale: open.id } }"
                        >
                          Return goods
                        </RouterLink>
                        <button
                          v-if="canRequestVoid"
                          class="btn btn--sm btn--danger"
                          @click="askForVoid"
                        >
                          Request void
                        </button>
                        <button class="btn btn--sm" @click="show(row)">Close</button>
                      </div>
                      <p class="muted small">
                        {{ when(open.invoice_datetime) }} · {{ open.customer_name || 'Walk-in' }}
                        <template v-if="open.customer_mobile"> · {{ open.customer_mobile }}</template>
                        <template v-if="open.doctor_name"> · Dr. {{ open.doctor_name }}</template>
                      </p>
                    </div>

                    <p v-if="open.status === 'VOID_REQUESTED'" class="alert alert--warn">
                      A void has been requested and is waiting for an administrator.
                    </p>
                    <p v-if="open.status === 'VOIDED'" class="alert alert--error">
                      This invoice is void. The stock went back to its batches; the number is
                      kept and never reused.
                    </p>
                    <p v-if="open.discount_override_reason" class="alert alert--warn">
                      Discount override: {{ open.discount_override_reason }}
                    </p>

                    <div class="table-wrap">
                      <table class="data">
                        <thead>
                          <tr>
                            <th>Item</th>
                            <th>Batch</th>
                            <th>Expiry</th>
                            <th class="numeric">Qty</th>
                            <th class="numeric">MRP</th>
                            <th class="numeric">Disc</th>
                            <th class="numeric">Amount</th>
                            <th v-if="auth.isAdmin" class="numeric">Cost</th>
                            <th v-if="auth.isAdmin" class="numeric">Margin</th>
                          </tr>
                        </thead>
                        <tbody>
                          <tr v-for="item in open.items" :key="item.id" class="row--plain">
                            <td>
                              {{ item.medicine_name }}
                              <p class="composition">{{ item.pack_label }}</p>
                            </td>
                            <td>{{ item.batch_no }}</td>
                            <td>{{ item.expiry_date }}</td>
                            <td class="numeric">{{ trim(item.qty_units) }}</td>
                            <td class="numeric">{{ money(item.unit_mrp) }}</td>
                            <td class="numeric">{{ item.discount_pct }}%</td>
                            <td class="numeric">{{ money(item.net_amount) }}</td>
                            <td v-if="auth.isAdmin" class="numeric">{{ money(item.line_cost) }}</td>
                            <td v-if="auth.isAdmin" class="numeric">
                              {{ item.margin_pct !== null && item.margin_pct !== undefined
                                 ? `${item.margin_pct}%` : '—' }}
                            </td>
                          </tr>
                        </tbody>
                      </table>
                    </div>

                    <div class="totals">
                      <div><span>Gross</span><strong>{{ money(open.gross_amount) }}</strong></div>
                      <div>
                        <span>Discount</span><strong>−{{ money(open.discount_amount) }}</strong>
                      </div>
                      <div><span>Taxable</span><strong>{{ money(open.taxable_amount) }}</strong></div>
                      <div v-if="open.is_intra_state">
                        <span>CGST</span><strong>{{ money(open.cgst_amount) }}</strong>
                      </div>
                      <div v-if="open.is_intra_state">
                        <span>SGST</span><strong>{{ money(open.sgst_amount) }}</strong>
                      </div>
                      <div v-else><span>IGST</span><strong>{{ money(open.igst_amount) }}</strong></div>
                      <div><span>Round off</span><strong>{{ money(open.round_off) }}</strong></div>
                      <div class="totals__grand">
                        <span>Paid</span><strong>{{ money(open.net_amount) }}</strong>
                      </div>
                    </div>
                    <p class="muted small">
                      {{ open.amount_in_words }} ·
                      {{ open.payments.map((p) => `${p.mode} ${money(p.amount)}`).join(' + ') }}
                    </p>
                  </div>
                </td>
              </tr>
            </template>
          </tbody>
        </table>
      </div>

      <div v-if="total > pageSize" class="form-actions">
        <button class="btn btn--sm" :disabled="page === 1" @click="changePage(-1)">Previous</button>
        <span class="muted">
          Page {{ page }} of {{ Math.ceil(total / pageSize) }} · {{ total }} invoice(s)
        </span>
        <button class="btn btn--sm" :disabled="page * pageSize >= total" @click="changePage(1)">
          Next
        </button>
      </div>
    </div>
  </section>
</template>

<style scoped>
.grow {
  flex: 1;
  min-width: 10rem;
}

.bar {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 1rem;
  flex-wrap: wrap;
}

.composition {
  margin: 0.1rem 0 0;
  font-size: 0.8rem;
  color: var(--text-muted);
  white-space: normal;
}

.totals {
  margin-left: auto;
  min-width: 18rem;
  font-size: 0.92rem;
}

.totals > div {
  display: flex;
  justify-content: space-between;
  padding: 0.15rem 0;
}

.totals__grand {
  border-top: 1px solid var(--border);
  margin-top: 0.3rem;
  padding-top: 0.4rem;
  font-size: 1.05rem;
}

.small {
  font-size: 0.82rem;
}

tbody tr {
  cursor: pointer;
}

.row--selected {
  background: var(--accent-soft) !important;
}

/* The unfolded invoice: inset from the register, and not itself clickable. */
.row--detail,
.row--detail td,
.row--plain {
  cursor: default;
}

.row--detail > td {
  background: var(--surface-sunken);
  border-left: 3px solid var(--accent);
  padding: var(--sp-3);
}

.detail {
  max-width: 100%;
}

/* The nested lines table keeps the register's alternating fill from bleeding
   through — it is a different table with its own rows. */
.detail .data {
  background: var(--surface);
}

.caret {
  display: inline-block;
  width: 1em;
  color: var(--text-muted);
  font-size: 0.8em;
}
</style>
