<script setup>
/**
 * Customers — the register of who buys here.
 *
 * The POS already creates a customer from a mobile number mid-sale, so most
 * rows arrive with nothing but a number. This screen is where they are
 * filled in, and where the two questions the counter actually asks get
 * answered: what has this person bought, and do they have credit to spend?
 *
 * The mobile number is the identity — the POS matches on it and credit
 * notes hang off it — so it is set once and never edited. A number typed
 * wrongly is deleted and re-entered, which keeps a stranger's history from
 * silently becoming someone else's.
 */
import { computed, onMounted, reactive, ref, watch } from 'vue'

import {
  createCustomer,
  deleteCustomer,
  fetchCustomer,
  listCustomers,
  updateCustomer,
} from '@/api/pos'
import { useDebounced } from '@/composables/useDebounced'
import { useAuthStore } from '@/stores/auth'

const auth = useAuthStore()

const rupees = new Intl.NumberFormat('en-IN', {
  style: 'currency',
  currency: 'INR',
  minimumFractionDigits: 2,
})
const money = (v) => (v === null || v === undefined ? '—' : rupees.format(Number(v)))
const day = (v) =>
  v ? new Date(`${v}T00:00:00`).toLocaleDateString('en-IN', { dateStyle: 'medium' }) : '—'

const SALE_BADGE = {
  COMPLETED: 'badge--ok',
  VOIDED: 'badge--danger',
  VOID_REQUESTED: 'badge--warn',
  PARTIALLY_RETURNED: 'badge--warn',
  RETURNED: 'badge--warn',
}

/** Green only where there is money left to spend — a lapsed note is not credit. */
function noteBadge(note) {
  const live = note.status === 'OPEN' || note.status === 'PARTIALLY_USED'
  if (live && note.days_to_expiry >= 0 && Number(note.balance_amount) > 0) return 'badge--ok'
  return note.status === 'FULLY_USED' ? 'badge--warn' : 'badge--danger'
}

const words = (v) => v.replaceAll('_', ' ').toLowerCase()

const items = ref([])
const total = ref(0)
const loading = ref(true)
const error = ref(null)
const notice = ref(null)

const query = ref('')
const debouncedQuery = useDebounced(query, 250)
const sort = ref('name')
const creditOnly = ref(false)
const showInactive = ref(false)

async function load() {
  loading.value = true
  error.value = null
  try {
    const page = await listCustomers({
      q: query.value.trim() || undefined,
      sort: sort.value,
      has_credit: creditOnly.value || undefined,
      is_active: showInactive.value ? undefined : true,
      page_size: 100,
    })
    items.value = page.items
    total.value = page.total
  } catch (err) {
    error.value = err.message
  } finally {
    loading.value = false
  }
}

watch([debouncedQuery, sort, creditOnly, showInactive], load)

// --- the card ------------------------------------------------------------
const card = ref(null)
const cardLoading = ref(false)

async function open(customer) {
  if (card.value?.id === customer.id) {
    card.value = null // clicking the open row closes it
    return
  }
  cardLoading.value = true
  error.value = null
  try {
    card.value = await fetchCustomer(customer.id)
  } catch (err) {
    error.value = err.message
  } finally {
    cardLoading.value = false
  }
}

/** The cached balance and the notes themselves should agree; say so if not. */
const creditDrift = computed(
  () => card.value && Number(card.value.credit_balance) !== Number(card.value.open_credit),
)

// --- add / edit ----------------------------------------------------------
const blank = () => ({
  mobile: '',
  name: '',
  address: '',
  email: '',
  date_of_birth: '',
  gstin: '',
  state_code: '',
  notes: '',
})
const form = reactive(blank())
const editing = ref(null)
const adding = ref(false)
const saving = ref(false)
const formError = ref(null)

const formOpen = computed(() => adding.value || editing.value !== null)

function startAdd() {
  Object.assign(form, blank())
  editing.value = null
  adding.value = true
  formError.value = null
}

function startEdit(customer) {
  Object.assign(form, blank(), {
    mobile: customer.mobile,
    name: customer.name || '',
    address: customer.address || '',
    email: customer.email || '',
    date_of_birth: customer.date_of_birth || '',
    gstin: customer.gstin || '',
    state_code: customer.state_code || '',
    notes: customer.notes || '',
  })
  editing.value = customer
  adding.value = false
  formError.value = null
}

function closeForm() {
  adding.value = false
  editing.value = null
  formError.value = null
}

/** Empty strings are not blanks to the API — a GSTIN must be 15 characters
 *  or absent, and an empty email is not a valid address. */
function payload() {
  const clean = (v) => (String(v).trim() ? String(v).trim() : null)
  const body = {
    name: clean(form.name),
    address: clean(form.address),
    email: clean(form.email),
    date_of_birth: clean(form.date_of_birth),
    gstin: clean(form.gstin),
    state_code: clean(form.state_code),
    notes: clean(form.notes),
  }
  if (adding.value) body.mobile = form.mobile.replace(/\D/g, '')
  return body
}

async function submit() {
  saving.value = true
  formError.value = null
  notice.value = null
  try {
    if (editing.value) {
      const saved = await updateCustomer(editing.value.id, payload())
      notice.value = `${saved.name || saved.mobile} updated.`
      if (card.value?.id === saved.id) card.value = await fetchCustomer(saved.id)
    } else {
      const created = await createCustomer(payload())
      notice.value = `${created.name || created.mobile} added.`
    }
    closeForm()
    await load()
  } catch (err) {
    formError.value = err.message
  } finally {
    saving.value = false
  }
}

async function toggleActive(customer) {
  error.value = null
  try {
    await updateCustomer(customer.id, { is_active: !customer.is_active })
    await load()
  } catch (err) {
    error.value = err.message
  }
}

async function remove(customer) {
  const who = customer.name || customer.mobile
  if (!confirm(`Delete ${who}? A customer with invoices is deactivated instead.`)) return

  error.value = null
  try {
    const result = await deleteCustomer(customer.id)
    notice.value = result.message
    if (card.value?.id === customer.id) card.value = null
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
      <h1>Customers</h1>
      <p class="muted">
        The mobile number is the identity — it is what the counter types, what the register
        searches, and what a credit note is matched against. Select a row to see what
        someone has bought and what credit they still hold.
      </p>
    </div>

    <div class="card stack">
      <div class="toolbar">
        <input
          v-model="query"
          class="grow"
          type="search"
          placeholder="Mobile number or name…"
        />
        <select v-model="sort" aria-label="Sort by">
          <option value="name">Name (A–Z)</option>
          <option value="mobile">Mobile</option>
          <option value="-created_at">Newest first</option>
          <option value="-credit_balance">Most credit</option>
        </select>
        <label class="check">
          <input v-model="creditOnly" type="checkbox" />
          <span>Holding credit</span>
        </label>
        <label class="check">
          <input v-model="showInactive" type="checkbox" />
          <span>Show disabled</span>
        </label>
        <button class="btn btn--primary" @click="startAdd">Add customer</button>
      </div>

      <p v-if="error" class="alert alert--error">{{ error }}</p>
      <p v-if="notice" class="alert alert--ok">{{ notice }}</p>
      <p v-if="loading" class="muted">Loading…</p>
      <p v-else-if="!items.length" class="muted">
        No customers match. The POS adds one automatically when a mobile number is taken
        with a sale.
      </p>

      <div v-else class="table-wrap">
        <table class="data">
          <thead>
            <tr>
              <th>Customer</th>
              <th class="numeric">Invoices</th>
              <th>Last purchase</th>
              <th class="numeric">Lifetime spend</th>
              <th class="numeric">Credit</th>
              <th>Status</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            <tr
              v-for="item in items"
              :key="item.id"
              :class="{ inactive: !item.is_active, 'is-open': card?.id === item.id }"
            >
              <td>
                <button class="linky" @click="open(item)">
                  <strong>{{ item.name || 'Unnamed' }}</strong>
                  <span class="muted"> · {{ item.mobile }}</span>
                </button>
                <p v-if="item.gstin" class="sub">GSTIN {{ item.gstin }}</p>
              </td>
              <td class="numeric">{{ item.sale_count ?? 0 }}</td>
              <td>{{ day(item.last_purchase_on) }}</td>
              <td class="numeric">{{ money(item.total_spend) }}</td>
              <td class="numeric">
                <span v-if="Number(item.credit_balance) > 0" class="badge badge--ok">
                  {{ money(item.credit_balance) }}
                </span>
                <span v-else class="muted">—</span>
              </td>
              <td>
                <span :class="['badge', item.is_active ? 'badge--ok' : 'badge--danger']">
                  {{ item.is_active ? 'Active' : 'Disabled' }}
                </span>
              </td>
              <td class="numeric actions">
                <button class="btn btn--sm" @click="open(item)">
                  {{ card?.id === item.id ? 'Hide' : 'View' }}
                </button>
                <button class="btn btn--sm" @click="startEdit(item)">Edit</button>
                <button class="btn btn--sm" @click="toggleActive(item)">
                  {{ item.is_active ? 'Disable' : 'Enable' }}
                </button>
                <button v-if="auth.isAdmin" class="btn btn--sm btn--danger" @click="remove(item)">
                  Delete
                </button>
              </td>
            </tr>
          </tbody>
        </table>
      </div>

      <p v-if="!loading && items.length" class="muted small">
        {{ items.length }} of {{ total }} customer(s).
      </p>
    </div>

    <!-- add / edit ------------------------------------------------------- -->
    <div v-if="formOpen" class="card stack">
      <h2>{{ editing ? `Edit ${editing.name || editing.mobile}` : 'Add a customer' }}</h2>
      <form class="stack" @submit.prevent="submit">
        <div class="form-grid">
          <label class="field">
            <span>Mobile</span>
            <input
              v-model="form.mobile"
              :disabled="!!editing"
              required
              inputmode="numeric"
              maxlength="15"
              placeholder="9876543210"
            />
            <small v-if="editing" class="field__hint">
              The mobile number is the identity and cannot be changed. If it is wrong,
              delete this record and enter it again.
            </small>
          </label>
          <label class="field">
            <span>Name</span>
            <input v-model="form.name" maxlength="150" />
          </label>
          <label class="field">
            <span>Email</span>
            <input v-model="form.email" type="email" />
          </label>
          <label class="field">
            <span>Date of birth</span>
            <input v-model="form.date_of_birth" type="date" />
          </label>
          <label class="field">
            <span>GSTIN</span>
            <input v-model="form.gstin" maxlength="15" placeholder="15 characters, B2B only" />
          </label>
          <label class="field">
            <span>State code</span>
            <input v-model="form.state_code" maxlength="2" />
          </label>
          <label class="field field--wide">
            <span>Address</span>
            <input v-model="form.address" maxlength="300" />
          </label>
          <label class="field field--wide">
            <span>Notes</span>
            <input v-model="form.notes" maxlength="1000" placeholder="Allergies, standing instructions…" />
          </label>
        </div>

        <p v-if="formError" class="alert alert--error">{{ formError }}</p>
        <div class="form-actions">
          <button class="btn btn--primary" type="submit" :disabled="saving">
            {{ saving ? 'Saving…' : editing ? 'Save changes' : 'Add customer' }}
          </button>
          <button class="btn" type="button" @click="closeForm">Cancel</button>
        </div>
      </form>
    </div>

    <!-- one customer's card ---------------------------------------------- -->
    <p v-if="cardLoading" class="muted">Opening…</p>

    <template v-if="card">
      <div class="grid-3">
        <div class="card tile">
          <span class="tile__label">Invoices</span>
          <strong class="tile__value">{{ card.sale_count ?? 0 }}</strong>
          <span class="muted small">
            since {{ day(card.first_purchase_on) }}
          </span>
        </div>
        <div class="card tile">
          <span class="tile__label">Lifetime spend</span>
          <strong class="tile__value">{{ money(card.total_spend) }}</strong>
          <span class="muted small">voided invoices excluded</span>
        </div>
        <div class="card tile">
          <span class="tile__label">Credit to spend</span>
          <strong :class="['tile__value', Number(card.open_credit) > 0 ? 'is-ok' : '']">
            {{ money(card.open_credit) }}
          </strong>
          <span class="muted small">
            applied automatically at the counter
          </span>
        </div>
      </div>

      <p v-if="creditDrift" class="alert alert--warn">
        The cached balance ({{ money(card.credit_balance) }}) disagrees with the open notes
        ({{ money(card.open_credit) }}). The notes are the truth — the counter will only
        allow what they hold.
      </p>

      <div class="grid-2">
        <div class="card stack">
          <h2>{{ card.name || 'Unnamed' }} · {{ card.mobile }}</h2>
          <dl class="kv">
            <dt>Email</dt>
            <dd>{{ card.email || '—' }}</dd>
            <dt>Date of birth</dt>
            <dd>{{ day(card.date_of_birth) }}</dd>
            <dt>GSTIN</dt>
            <dd>{{ card.gstin || '—' }}</dd>
            <dt>State code</dt>
            <dd>{{ card.state_code || '—' }}</dd>
            <dt>Address</dt>
            <dd>{{ card.address || '—' }}</dd>
            <dt>Last purchase</dt>
            <dd>{{ day(card.last_purchase_on) }}</dd>
          </dl>
          <p v-if="card.notes" class="note">{{ card.notes }}</p>
          <div class="form-actions">
            <button class="btn btn--sm" @click="startEdit(card)">Edit details</button>
            <button class="btn btn--sm" @click="card = null">Close</button>
          </div>
        </div>

        <div class="card stack">
          <h2>Credit notes</h2>
          <p v-if="!card.credit_notes.length" class="muted">
            None on file. A return settled as credit puts one here.
          </p>
          <div v-else class="table-wrap">
            <table class="data">
              <thead>
                <tr>
                  <th>Note</th>
                  <th>Issued</th>
                  <th class="numeric">Issued for</th>
                  <th class="numeric">Balance</th>
                  <th>Expires</th>
                  <th>Status</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="note in card.credit_notes" :key="note.id">
                  <td>{{ note.credit_note_no }}</td>
                  <td>{{ day(note.issue_date) }}</td>
                  <td class="numeric">{{ money(note.amount_issued) }}</td>
                  <td class="numeric">{{ money(note.balance_amount) }}</td>
                  <td>
                    {{ day(note.expires_on) }}
                    <span
                      v-if="note.days_to_expiry < 0"
                      class="badge badge--danger"
                      >lapsed</span
                    >
                    <span
                      v-else-if="note.days_to_expiry <= 30"
                      class="badge badge--warn"
                      >{{ note.days_to_expiry }}d</span
                    >
                  </td>
                  <td>
                    <span :class="['badge', noteBadge(note)]">{{ words(note.status) }}</span>
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
        </div>
      </div>

      <div class="card stack">
        <h2>Purchase history</h2>
        <p v-if="!card.recent_sales.length" class="muted">Nothing bought yet.</p>
        <div v-else class="table-wrap">
          <table class="data">
            <thead>
              <tr>
                <th>Invoice</th>
                <th>Date</th>
                <th class="numeric">Items</th>
                <th class="numeric">Amount</th>
                <th>Status</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="sale in card.recent_sales" :key="sale.id">
                <td>{{ sale.invoice_no }}</td>
                <td>{{ day(sale.invoice_date) }}</td>
                <td class="numeric">{{ sale.item_count }}</td>
                <td class="numeric">{{ money(sale.net_amount) }}</td>
                <td>
                  <span :class="['badge', SALE_BADGE[sale.status] || 'badge--warn']">
                    {{ words(sale.status) }}
                  </span>
                </td>
                <td class="numeric">
                  <RouterLink class="btn btn--sm" :to="`/sales/${sale.id}/invoice`">
                    Invoice
                  </RouterLink>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>
    </template>
  </section>
</template>

<style scoped>
.check {
  display: flex;
  align-items: center;
  gap: var(--sp-1);
  font-size: 0.85rem;
  white-space: nowrap;
  color: var(--text-muted);
}

.check input {
  width: auto;
}

/* The name is the way into the card, so it has to look like a control
   without becoming a second button in the actions column. */
.linky {
  background: none;
  border: 0;
  padding: 0;
  font: inherit;
  color: var(--text);
  cursor: pointer;
  text-align: left;
}

.linky:hover strong {
  color: var(--accent);
  text-decoration: underline;
}

.sub {
  margin: 0.1rem 0 0;
  font-size: 0.75rem;
  color: var(--text-muted);
}

.is-open > td {
  background: var(--accent-soft);
}

.inactive td {
  opacity: 0.6;
}

.actions {
  white-space: nowrap;
}

.actions .btn + .btn {
  margin-left: 0.3rem;
}

.field--wide {
  grid-column: 1 / -1;
}

.note {
  margin: 0;
  padding: var(--sp-2);
  background: var(--surface-sunken);
  border-left: 2px solid var(--border-strong);
  border-radius: var(--radius-sm);
  font-size: 0.85rem;
}

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
  font-size: 1.6rem;
  line-height: 1.1;
  font-variant-numeric: tabular-nums;
}

.is-ok {
  color: var(--ok);
}
</style>
