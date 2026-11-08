<script setup>
/**
 * Reports.
 *
 * One screen for all thirteen. The API returns every report in the same
 * envelope — typed columns, rows keyed by those columns, an optional totals
 * row — so this view renders a table it has never seen before: the column
 * *type* decides the formatting and the alignment, and nothing here knows
 * what a "net liability" is.
 *
 * What is hard-coded is only the catalogue below: which reports exist, where
 * they live, and which extra control each one takes. Adding a report to the
 * server means adding a line to it.
 */
import { computed, ref, watch } from 'vue'

import { downloadReport, fetchReport } from '@/api/reports'

const REPORTS = [
  {
    group: 'GST',
    key: 'gst-outward',
    label: 'Outward (sales) summary',
    path: '/gst/outward-summary',
    hint: 'Rate-wise taxable value and tax collected, split B2C/B2B and net of the period’s credit notes.',
  },
  {
    group: 'GST',
    key: 'gst-inward',
    label: 'Inward (purchase) summary',
    path: '/gst/inward-summary',
    hint: 'The purchase register: input tax by supplier and rate, dated by when the goods were received.',
  },
  {
    group: 'GST',
    key: 'gst-liability',
    label: 'Net liability',
    path: '/gst/net-liability',
    hint: 'Outward tax less input credit, per rate, per month. A negative figure is credit carried forward.',
  },
  {
    group: 'Sales',
    key: 'daybook',
    label: 'Day-book',
    path: '/sales/daybook',
    hint: 'Invoice by invoice, with the payment-mode split that reconciles the drawer.',
  },
  {
    group: 'Sales',
    key: 'by-executive',
    label: 'By executive',
    path: '/sales/by-executive',
    hint: 'Volume, discounting, and how often a sale comes undone.',
  },
  {
    group: 'Stock',
    key: 'fast-moving',
    label: 'Fast moving',
    path: '/inventory/fast-moving',
    hint: 'What sells, and how many days of cover is left at this rate.',
    controls: ['top'],
  },
  {
    group: 'Stock',
    key: 'slow-moving',
    label: 'Slow moving',
    path: '/inventory/slow-moving',
    hint: 'Stock that did not move, and the money standing still inside it.',
    controls: ['threshold'],
  },
  {
    group: 'Stock',
    key: 'valuation',
    label: 'Valuation',
    path: '/inventory/valuation',
    hint: 'Batch-wise stock at cost and at MRP. Taken as at today, not as at the end of the range.',
    undated: true,
  },
  {
    group: 'Stock',
    key: 'expiry-ageing',
    label: 'Expiry ageing',
    path: '/inventory/expiry-ageing',
    hint: 'How much of the shelf is running out of time. As at today.',
    undated: true,
  },
  {
    group: 'Business',
    key: 'profitability',
    label: 'Profitability',
    path: '/profitability',
    hint: 'Revenue excluding GST, less the cost snapshotted when the goods were sold. Returns are netted off both sides.',
    controls: ['groupBy'],
  },
  {
    group: 'Business',
    key: 'top-customers',
    label: 'Top customers',
    path: '/customers/top',
    hint: 'Ranked by value billed. A sale with no name and no mobile cannot be attributed and is left out.',
    controls: ['top'],
  },
  {
    group: 'Business',
    key: 'top-doctors',
    label: 'Top doctors',
    path: '/doctors/top',
    hint: 'Which prescribers bring the counter its business.',
    controls: ['top'],
  },
  {
    group: 'Business',
    key: 'credit-notes',
    label: 'Credit-note ledger',
    path: '/credit-notes/ledger',
    hint: 'Notes raised in the window: redeemed, still outstanding, and about to lapse.',
  },
  {
    group: 'Business',
    key: 'audit',
    label: 'Void & audit',
    path: '/audit',
    hint: 'Voids, approvals, rejections and discount overrides.',
  },
]

const GROUPS = ['GST', 'Sales', 'Stock', 'Business']

const META_LABELS = {
  store_name: 'Store',
  gstin: 'GSTIN',
  state_code: 'State code',
  round_off_total: 'Round-off',
  dated_by: 'Dated by',
  as_at: 'As at',
  stock_as_at: 'Stock as at',
  batch_count: 'Batches',
  batches_without_cost: 'Batches with no cost',
  medicine_count: 'Medicines',
  threshold_units: 'Threshold (units)',
  days_in_window: 'Days in window',
  invoice_count: 'Invoices',
  voided_count: 'Voided',
  cash_refunds: 'Cash refunded',
  note_count: 'Credit notes',
  outstanding: 'Outstanding',
  expiring_within_30_days: 'Expiring in 30 days',
  entry_count: 'Entries',
  lines_without_cost: 'Lines with no cost',
  group_by: 'Grouped by',
  top: 'Showing top',
}

// Values that are plumbing rather than information on screen.
const META_HIDDEN = new Set(['by_action', 'payment_modes'])

const rupees = new Intl.NumberFormat('en-IN', {
  style: 'currency',
  currency: 'INR',
  minimumFractionDigits: 2,
})

const today = new Date()

// Built from the local parts, never `toISOString()`: that converts to UTC,
// so before 05:30 at the counter it would default the range to yesterday.
const iso = (d) =>
  `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`
const firstOfMonth = new Date(today.getFullYear(), today.getMonth(), 1)

const selected = ref(REPORTS[0])
const dateFrom = ref(iso(firstOfMonth))
const dateTo = ref(iso(today))
const top = ref(50)
const threshold = ref('0')
const groupBy = ref('medicine')

const report = ref(null)
const loading = ref(false)
const busy = ref(false)
const error = ref(null)

const grouped = computed(() =>
  GROUPS.map((group) => ({
    label: group,
    items: REPORTS.filter((r) => r.group === group),
  })),
)

const params = computed(() => {
  const query = { date_from: dateFrom.value, date_to: dateTo.value }
  const controls = selected.value.controls || []
  if (controls.includes('top')) query.top = top.value
  if (controls.includes('threshold')) query.threshold_units = threshold.value
  if (controls.includes('groupBy')) query.group_by = groupBy.value
  return query
})

/** Meta entries worth showing as chips, in the order the server sent them. */
const metaChips = computed(() => {
  const meta = report.value?.meta || {}
  return Object.entries(meta)
    .filter(([key, value]) => !META_HIDDEN.has(key) && value !== null && value !== '')
    .map(([key, value]) => ({ key, label: META_LABELS[key] || key.replace(/_/g, ' '), value }))
})

/** The day-book's drawer reconciliation — the one meta block with a shape. */
const paymentModes = computed(() => report.value?.meta?.payment_modes || [])

const isNumeric = (type) => ['int', 'money', 'qty', 'rate'].includes(type)

function format(value, type) {
  if (value === null || value === undefined || value === '') return '—'
  switch (type) {
    case 'money':
      return rupees.format(Number(value))
    case 'qty':
      return Number(value).toLocaleString('en-IN', { maximumFractionDigits: 3 })
    case 'rate':
      return `${Number(value).toFixed(2)} %`
    case 'int':
      return Number(value).toLocaleString('en-IN')
    case 'date':
      return new Date(`${value}T00:00:00`).toLocaleDateString('en-IN', {
        day: '2-digit',
        month: 'short',
        year: 'numeric',
      })
    case 'datetime':
      // The server sends an offset-carrying instant; the browser shows it in
      // the reader's own zone, which for this shop is the counter's.
      return new Date(value).toLocaleString('en-IN', {
        day: '2-digit',
        month: 'short',
        hour: '2-digit',
        minute: '2-digit',
      })
    default:
      return value
  }
}

function preset(which) {
  const now = new Date()
  if (which === 'today') {
    dateFrom.value = iso(now)
    dateTo.value = iso(now)
  } else if (which === 'month') {
    dateFrom.value = iso(new Date(now.getFullYear(), now.getMonth(), 1))
    dateTo.value = iso(now)
  } else if (which === 'last-month') {
    dateFrom.value = iso(new Date(now.getFullYear(), now.getMonth() - 1, 1))
    dateTo.value = iso(new Date(now.getFullYear(), now.getMonth(), 0))
  } else if (which === 'year') {
    // The Indian financial year runs April to March.
    const start = now.getMonth() >= 3 ? now.getFullYear() : now.getFullYear() - 1
    dateFrom.value = iso(new Date(start, 3, 1))
    dateTo.value = iso(now)
  }
  load()
}

async function load() {
  loading.value = true
  error.value = null
  try {
    report.value = await fetchReport(selected.value.path, params.value)
  } catch (err) {
    report.value = null
    error.value = err.message
  } finally {
    loading.value = false
  }
}

async function save(format_) {
  busy.value = true
  error.value = null
  try {
    await downloadReport(
      selected.value.path,
      params.value,
      format_,
      `${selected.value.key}-${dateFrom.value}-to-${dateTo.value}`,
    )
  } catch (err) {
    error.value = err.message
  } finally {
    busy.value = false
  }
}

function choose(item) {
  selected.value = item
  load()
}

watch([top, threshold, groupBy], load)

load()
</script>

<template>
  <section class="reports">
    <aside class="card stack reports__nav">
      <h1>Reports</h1>
      <div v-for="group in grouped" :key="group.label" class="reports__group">
        <p class="reports__heading">{{ group.label }}</p>
        <button
          v-for="item in group.items"
          :key="item.key"
          class="reports__link"
          :class="{ 'reports__link--on': selected.key === item.key }"
          @click="choose(item)"
        >
          {{ item.label }}
        </button>
      </div>
    </aside>

    <div class="stack reports__body">
      <div class="card stack">
        <div class="card__head">
          <h2>{{ report?.title || selected.label }}</h2>
          <div class="toolbar">
            <button class="btn btn--sm" :disabled="busy || !report" @click="save('csv')">
              Export CSV
            </button>
            <button class="btn btn--sm" :disabled="busy || !report" @click="save('xlsx')">
              Export Excel
            </button>
          </div>
        </div>
        <p class="muted small">{{ selected.hint }}</p>

        <div class="toolbar">
          <label class="field field--inline">
            <span>From</span>
            <input v-model="dateFrom" type="date" :disabled="selected.undated" @change="load" />
          </label>
          <label class="field field--inline">
            <span>To</span>
            <input v-model="dateTo" type="date" :disabled="selected.undated" @change="load" />
          </label>

          <template v-if="!selected.undated">
            <button class="btn btn--sm" @click="preset('today')">Today</button>
            <button class="btn btn--sm" @click="preset('month')">This month</button>
            <button class="btn btn--sm" @click="preset('last-month')">Last month</button>
            <button class="btn btn--sm" @click="preset('year')">Financial year</button>
          </template>

          <label v-if="selected.controls?.includes('top')" class="field field--inline">
            <span>Top</span>
            <input v-model.number="top" type="number" min="1" max="500" />
          </label>
          <label v-if="selected.controls?.includes('threshold')" class="field field--inline">
            <span>Sold at most</span>
            <input v-model="threshold" type="number" min="0" step="1" />
          </label>
          <label v-if="selected.controls?.includes('groupBy')" class="field field--inline">
            <span>Group by</span>
            <select v-model="groupBy">
              <option value="medicine">Medicine</option>
              <option value="manufacturer">Manufacturer</option>
              <option value="month">Month</option>
            </select>
          </label>
        </div>

        <p v-if="selected.undated" class="muted small">
          This report is a snapshot of the shelf right now, so the date range does not
          apply to it.
        </p>

        <div v-if="metaChips.length" class="reports__meta">
          <span v-for="chip in metaChips" :key="chip.key" class="badge">
            {{ chip.label }}: <strong>{{ chip.value }}</strong>
          </span>
        </div>

        <p v-if="error" class="alert alert--error">{{ error }}</p>
      </div>

      <div v-if="paymentModes.length" class="card stack">
        <h3>Collected by mode</h3>
        <div class="reports__modes">
          <div v-for="mode in paymentModes" :key="mode.mode" class="reports__mode">
            <span class="muted small">{{ mode.mode.replace('_', ' ') }}</span>
            <strong>{{ format(mode.amount, 'money') }}</strong>
            <span class="muted small">{{ mode.payment_count }} payment(s)</span>
          </div>
        </div>
      </div>

      <div class="card stack">
        <p v-if="loading" class="muted">Loading…</p>
        <p v-else-if="!report" class="muted">Nothing to show.</p>
        <p v-else-if="!report.rows.length" class="muted">
          No activity between {{ format(report.date_from, 'date') }} and
          {{ format(report.date_to, 'date') }}.
        </p>
        <div v-else class="table-wrap">
          <table class="data">
            <thead>
              <tr>
                <th
                  v-for="column in report.columns"
                  :key="column.key"
                  :class="{ numeric: isNumeric(column.type) }"
                >
                  {{ column.label }}
                </th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="(row, index) in report.rows" :key="index">
                <td
                  v-for="column in report.columns"
                  :key="column.key"
                  :class="{ numeric: isNumeric(column.type) }"
                >
                  {{ format(row[column.key], column.type) }}
                </td>
              </tr>
            </tbody>
            <tfoot v-if="report.totals">
              <tr>
                <td
                  v-for="(column, index) in report.columns"
                  :key="column.key"
                  :class="{ numeric: isNumeric(column.type) }"
                >
                  <template v-if="report.totals[column.key] !== undefined">
                    {{ format(report.totals[column.key], column.type) }}
                  </template>
                  <template v-else-if="index === 0">Total</template>
                </td>
              </tr>
            </tfoot>
          </table>
        </div>
      </div>
    </div>
  </section>
</template>

<style scoped>
.reports {
  display: grid;
  grid-template-columns: 15rem minmax(0, 1fr);
  gap: 1rem;
  align-items: start;
}

@media (max-width: 60rem) {
  .reports {
    grid-template-columns: minmax(0, 1fr);
  }
}

.reports__nav {
  position: sticky;
  top: 1rem;
}

.reports__group + .reports__group {
  margin-top: 0.75rem;
}

.reports__heading {
  margin: 0 0 0.25rem;
  font-size: 0.72rem;
  font-weight: 700;
  letter-spacing: 0.06em;
  text-transform: uppercase;
  opacity: 0.6;
}

.reports__link {
  display: block;
  width: 100%;
  padding: 0.35rem 0.5rem;
  border: 0;
  border-radius: 0.375rem;
  background: none;
  font: inherit;
  text-align: left;
  color: inherit;
  cursor: pointer;
}

.reports__link:hover {
  background: rgba(15, 118, 110, 0.08);
}

.reports__link--on {
  background: rgba(15, 118, 110, 0.14);
  font-weight: 600;
}

.reports__body {
  min-width: 0;
}

.reports__meta {
  display: flex;
  flex-wrap: wrap;
  gap: 0.4rem;
}

.reports__modes {
  display: flex;
  flex-wrap: wrap;
  gap: 1.25rem;
}

.reports__mode {
  display: flex;
  flex-direction: column;
}

.field--inline {
  flex-direction: row;
  align-items: center;
  gap: 0.4rem;
}

.field--inline > span {
  margin: 0;
  white-space: nowrap;
}

tfoot td {
  font-weight: 600;
  border-top: 2px solid rgba(15, 23, 42, 0.15);
}
</style>
