<script setup>
/**
 * Void approvals and the credit-note register — the owner's desk.
 *
 * Voiding is deliberately two-handed: the counter asks, an admin decides.
 * That separation is what stops a till shortfall being covered by erasing
 * the sale that caused it, so this screen is where the second pair of eyes
 * actually happens.
 */
import { computed, onMounted, ref } from 'vue'

import {
  approveVoid,
  cancelCreditNote,
  listCreditNotes,
  listVoidRequests,
  rejectVoid,
} from '@/api/returns'

const rupees = new Intl.NumberFormat('en-IN', {
  style: 'currency',
  currency: 'INR',
  minimumFractionDigits: 2,
})
const money = (v) => (v === null || v === undefined ? '—' : rupees.format(Number(v)))
const when = (iso) =>
  iso
    ? new Date(iso).toLocaleString('en-IN', {
        timeZone: 'Asia/Kolkata',
        dateStyle: 'medium',
        timeStyle: 'short',
      })
    : '—'

const error = ref(null)
const notice = ref(null)

// --- void requests --------------------------------------------------------
const voidStatus = ref('PENDING')
const voidRows = ref([])
const loadingVoids = ref(true)

async function loadVoids() {
  loadingVoids.value = true
  try {
    voidRows.value = (
      await listVoidRequests({ status: voidStatus.value, page_size: 50 })
    ).items
  } catch (err) {
    error.value = err.message
  } finally {
    loadingVoids.value = false
  }
}

async function approve(row) {
  const ok = confirm(
    `Approve the void of invoice ${row.invoice_no}?\n\n` +
      `${money(row.net_amount)} · requested by ${row.requested_by_name}\n` +
      `Reason: ${row.request_reason}\n\n` +
      'Every unit on the invoice goes back to the batch it came from. The invoice ' +
      'number is kept and marked void — it is never reused.',
  )
  if (!ok) return

  error.value = null
  try {
    const note = prompt('Note (optional):') || null
    await approveVoid(row.id, note)
    notice.value = `Invoice ${row.invoice_no} voided and the stock returned.`
    await Promise.all([loadVoids(), loadNotes()])
  } catch (err) {
    error.value = err.message
  }
}

async function reject(row) {
  const note = prompt(
    `Reject the void of invoice ${row.invoice_no}?\n\nWhy? (recorded against the request)`,
  )
  if (!note || note.trim().length < 3) return

  error.value = null
  try {
    await rejectVoid(row.id, note.trim())
    notice.value = `Void of ${row.invoice_no} rejected — the invoice stands.`
    await loadVoids()
  } catch (err) {
    error.value = err.message
  }
}

// --- credit notes ---------------------------------------------------------
const noteRows = ref([])
const openOnly = ref(true)
const loadingNotes = ref(true)

async function loadNotes() {
  loadingNotes.value = true
  try {
    noteRows.value = (
      await listCreditNotes({ open_only: openOnly.value || undefined, page_size: 50 })
    ).items
  } catch (err) {
    error.value = err.message
  } finally {
    loadingNotes.value = false
  }
}

const outstanding = computed(() =>
  noteRows.value.reduce((sum, n) => sum + Number(n.balance_amount), 0),
)

async function cancel(note) {
  const reason = prompt(
    `Cancel credit note ${note.credit_note_no} for ${money(note.balance_amount)}?\n\n` +
      'Only possible while none of it has been spent.\n\nReason:',
  )
  if (!reason || reason.trim().length < 3) return

  error.value = null
  try {
    await cancelCreditNote(note.id, reason.trim())
    notice.value = `Credit note ${note.credit_note_no} cancelled.`
    await loadNotes()
  } catch (err) {
    error.value = err.message
  }
}

onMounted(() => Promise.all([loadVoids(), loadNotes()]))
</script>

<template>
  <section class="stack">
    <div class="card stack">
      <h1>Approvals</h1>
      <p class="muted">
        Void requests waiting on a decision, and the credit notes the shop owes. Whoever
        billed a sale cannot void it themselves — that is the point of this queue.
      </p>
    </div>

    <p v-if="error" class="alert alert--error">{{ error }}</p>
    <p v-if="notice" class="alert alert--ok">{{ notice }}</p>

    <div class="card stack">
      <div class="card__head">
        <h2>Void requests</h2>
        <select v-model="voidStatus" @change="loadVoids">
          <option value="PENDING">Pending</option>
          <option value="APPROVED">Approved</option>
          <option value="REJECTED">Rejected</option>
        </select>
      </div>

      <p v-if="loadingVoids" class="muted">Loading…</p>
      <p v-else-if="!voidRows.length" class="muted">
        Nothing {{ voidStatus.toLowerCase() }}.
      </p>

      <div v-else class="table-wrap">
        <table class="data">
          <thead>
            <tr>
              <th>Invoice</th>
              <th>Requested</th>
              <th>By</th>
              <th>Reason</th>
              <th class="numeric">Amount</th>
              <th v-if="voidStatus !== 'PENDING'">Reviewed</th>
              <th v-else></th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="row in voidRows" :key="row.id">
              <td>
                <strong>{{ row.invoice_no }}</strong>
                <small class="muted"><br />{{ row.invoice_date }}</small>
              </td>
              <td>{{ when(row.requested_at) }}</td>
              <td>{{ row.requested_by_name }}</td>
              <td class="wrap">{{ row.request_reason }}</td>
              <td class="numeric">{{ money(row.net_amount) }}</td>
              <td v-if="voidStatus !== 'PENDING'" class="wrap">
                {{ row.reviewed_by_name || '—' }}
                <small v-if="row.review_note" class="muted"><br />{{ row.review_note }}</small>
              </td>
              <td v-else class="numeric actions">
                <button class="btn btn--sm btn--primary" @click="approve(row)">Approve</button>
                <button class="btn btn--sm btn--danger" @click="reject(row)">Reject</button>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>

    <div class="card stack">
      <div class="card__head">
        <h2>Credit notes</h2>
        <label class="inline muted small">
          <input v-model="openOnly" type="checkbox" @change="loadNotes" />
          Unspent only
        </label>
      </div>
      <p class="muted small">
        Outstanding balance: <strong>{{ money(outstanding) }}</strong> across
        {{ noteRows.length }} note(s).
      </p>

      <p v-if="loadingNotes" class="muted">Loading…</p>
      <p v-else-if="!noteRows.length" class="muted">No credit notes on file.</p>

      <div v-else class="table-wrap">
        <table class="data">
          <thead>
            <tr>
              <th>Note</th>
              <th>Issued</th>
              <th>Customer</th>
              <th class="numeric">Issued</th>
              <th class="numeric">Used</th>
              <th class="numeric">Balance</th>
              <th>Expires</th>
              <th>Status</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="note in noteRows" :key="note.id">
              <td><strong>{{ note.credit_note_no }}</strong></td>
              <td>{{ note.issue_date }}</td>
              <td>
                {{ note.customer_name || '—' }}
                <small class="muted"><br />{{ note.customer_mobile }}</small>
              </td>
              <td class="numeric">{{ money(note.amount_issued) }}</td>
              <td class="numeric">{{ money(note.amount_used) }}</td>
              <td class="numeric">{{ money(note.balance_amount) }}</td>
              <td>
                {{ note.expires_on }}
                <span v-if="note.days_to_expiry <= 30 && note.days_to_expiry >= 0"
                      class="badge badge--warn">
                  {{ note.days_to_expiry }}d
                </span>
              </td>
              <td>
                <span
                  :class="[
                    'badge',
                    note.status === 'OPEN' || note.status === 'PARTIALLY_USED'
                      ? 'badge--ok'
                      : note.status === 'CANCELLED' || note.status === 'EXPIRED'
                        ? 'badge--danger'
                        : 'badge--warn',
                  ]"
                >
                  {{ note.status.replace('_', ' ').toLowerCase() }}
                </span>
              </td>
              <td class="numeric">
                <button
                  class="btn btn--sm btn--danger"
                  :disabled="Number(note.amount_used) > 0 || note.status === 'CANCELLED'"
                  :title="
                    Number(note.amount_used) > 0
                      ? 'Partly spent — cannot be cancelled'
                      : 'Cancel this note'
                  "
                  @click="cancel(note)"
                >
                  Cancel
                </button>
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

.inline {
  display: inline-flex;
  align-items: center;
  gap: 0.35rem;
}

.wrap {
  white-space: normal;
  max-width: 22rem;
}

.actions {
  white-space: nowrap;
}

.actions button + button {
  margin-left: 0.35rem;
}
</style>
