<script setup>
/**
 * Debit notes — goods sent back to a distributor.
 *
 * The register exists mostly for the second half of the transaction. Raising
 * a return happens where the stock is, on the inventory screen; what happens
 * afterwards is that the distributor's next invoice arrives and the credit
 * has to come off it. That is done here, against a specific invoice, so there
 * is a record of which credit settled which bill.
 */
import { computed, onMounted, ref } from 'vue'

import { listPurchases } from '@/api/purchases'
import {
  applyCreditToInvoice,
  cancelPurchaseReturn,
  creditOnInvoice,
  listPurchaseReturns,
} from '@/api/purchaseReturns'
import ModalDialog from '@/components/ModalDialog.vue'
import { useAuthStore } from '@/stores/auth'

const auth = useAuthStore()

const rupees = new Intl.NumberFormat('en-IN', {
  style: 'currency',
  currency: 'INR',
  minimumFractionDigits: 2,
})
const money = (v) => (v === null || v === undefined ? '—' : rupees.format(Number(v)))
const trim = (v) => Number(v).toLocaleString('en-IN', { maximumFractionDigits: 3 })

const rows = ref([])
const total = ref(0)
const page = ref(1)
const pageSize = 25
const loading = ref(true)
const error = ref(null)
const notice = ref(null)
const expanded = ref(null)

const pages = computed(() => Math.max(1, Math.ceil(total.value / pageSize)))

async function load() {
  loading.value = true
  error.value = null
  try {
    const body = await listPurchaseReturns({ page: page.value, page_size: pageSize })
    rows.value = body.items || []
    total.value = body.total || 0
  } catch (err) {
    error.value = err.message
  } finally {
    loading.value = false
  }
}

function toggle(row) {
  expanded.value = expanded.value === row.id ? null : row.id
}

async function cancel(row) {
  const reason = prompt(
    `Cancel debit note ${row.debit_note_no}?\n\n` +
      'The goods go back on the shelf and the credit is withdrawn. Not ' +
      'possible once any of the credit has been set against an invoice.\n\n' +
      'Reason:',
  )
  if (!reason || reason.trim().length < 3) return
  try {
    await cancelPurchaseReturn(row.id, reason.trim())
    notice.value = `Debit note ${row.debit_note_no} cancelled; the stock is back.`
    error.value = null
    await load()
  } catch (err) {
    error.value = err.message
  }
}

// --- setting the credit against an invoice --------------------------------

const applyFor = ref(null)
const invoices = ref([])
const applyForm = ref({ purchase_invoice_id: '', amount: '' })
const applySaving = ref(false)
const applyError = ref(null)
const applied = ref(null)

async function startApply(row) {
  applyFor.value = row
  applyError.value = null
  applied.value = null
  applyForm.value = { purchase_invoice_id: '', amount: String(row.credit_balance ?? '') }
  try {
    // Only this distributor's invoices: credit from one supplier cannot
    // settle another's bill, and the server refuses it anyway.
    const body = await listPurchases({ supplier_id: row.supplier_id, page_size: 100 })
    invoices.value = body.items || []
  } catch (err) {
    applyError.value = err.message
  }
}

function closeApply() {
  applyFor.value = null
  applyError.value = null
  applied.value = null
}

async function onInvoiceChosen() {
  applied.value = null
  if (!applyForm.value.purchase_invoice_id) return
  try {
    applied.value = await creditOnInvoice(applyForm.value.purchase_invoice_id)
  } catch (err) {
    applyError.value = err.message
  }
}

async function submitApply() {
  const amount = Number(applyForm.value.amount)
  if (!applyForm.value.purchase_invoice_id) {
    applyError.value = 'Choose the invoice this credit comes off.'
    return
  }
  if (!Number.isFinite(amount) || amount <= 0) {
    applyError.value = 'Enter an amount.'
    return
  }
  if (amount > Number(applyFor.value.credit_balance)) {
    applyError.value = `Only ${money(applyFor.value.credit_balance)} is left on this note.`
    return
  }
  applySaving.value = true
  applyError.value = null
  try {
    const result = await applyCreditToInvoice(
      applyForm.value.purchase_invoice_id,
      applyFor.value.id,
      amount,
    )
    notice.value =
      `${money(result.amount_used)} of ${applyFor.value.debit_note_no} set against the ` +
      `invoice — ${money(result.balance_payable)} left to pay.`
    error.value = null
    closeApply()
    await load()
  } catch (err) {
    applyError.value = err.message
  } finally {
    applySaving.value = false
  }
}

onMounted(load)
</script>

<template>
  <section class="stack">
    <div class="card stack">
      <h1>Purchase returns</h1>
      <p class="muted">
        Goods sent back to a distributor. Each one is a debit note: the stock leaves, the
        input tax claimed on it is reversed so the GST return is right, and the value
        becomes credit against that distributor. When their next invoice arrives, set the
        credit against it here — the invoice still claims its own input tax in full,
        because a credit settles what is owed rather than reducing what was bought.
      </p>
      <p class="muted small">
        Raise one from <strong>Med inventory</strong> or <strong>Product inventory</strong>,
        where the batch is.
      </p>
    </div>

    <p v-if="error" class="alert alert--error">{{ error }}</p>
    <p v-if="notice" class="alert alert--ok">{{ notice }}</p>
    <p v-if="loading" class="muted">Loading…</p>

    <div v-else class="card">
      <table class="table">
        <thead>
          <tr>
            <th>Debit note</th>
            <th>Date</th>
            <th>Supplier</th>
            <th class="numeric">Items</th>
            <th v-if="auth.isAdmin" class="numeric">Value</th>
            <th v-if="auth.isAdmin" class="numeric">Credit left</th>
            <th>Status</th>
            <th v-if="auth.isAdmin" class="numeric">Actions</th>
          </tr>
        </thead>
        <tbody>
          <template v-for="row in rows" :key="row.id">
            <tr class="clickable" @click="toggle(row)">
              <td><strong>{{ row.debit_note_no }}</strong></td>
              <td>{{ row.return_date }}</td>
              <td>{{ row.supplier_name }}</td>
              <td class="numeric">{{ row.item_count }}</td>
              <td v-if="auth.isAdmin" class="numeric">{{ money(row.total_amount) }}</td>
              <td v-if="auth.isAdmin" class="numeric">{{ money(row.credit_balance) }}</td>
              <td>
                <span
                  :class="[
                    'badge',
                    row.status === 'CANCELLED' ? 'badge--warn' : 'badge--ok',
                  ]"
                >
                  {{ row.status === 'CANCELLED' ? 'Cancelled' : row.credit_status }}
                </span>
              </td>
              <td v-if="auth.isAdmin" class="numeric actions" @click.stop>
                <button
                  v-if="row.status !== 'CANCELLED' && Number(row.credit_balance) > 0"
                  class="btn btn--sm btn--primary"
                  @click="startApply(row)"
                >
                  Apply to invoice
                </button>
                <button
                  v-if="row.status !== 'CANCELLED' && Number(row.credit_used) === 0"
                  class="btn btn--sm btn--danger"
                  @click="cancel(row)"
                >
                  Cancel
                </button>
              </td>
            </tr>
            <tr v-if="expanded === row.id" class="expanded">
              <td :colspan="auth.isAdmin ? 8 : 5">
                <p class="muted small"><strong>Reason:</strong> {{ row.reason }}</p>
                <p v-if="row.supplier_credit_note_ref" class="muted small">
                  <strong>Their credit note:</strong> {{ row.supplier_credit_note_ref }}
                </p>
                <p v-if="row.cancel_reason" class="muted small">
                  <strong>Cancelled:</strong> {{ row.cancel_reason }}
                </p>
                <p v-if="auth.isAdmin" class="muted small">
                  Taxable {{ money(row.taxable_amount) }} ·
                  <template v-if="row.is_intra_state">
                    CGST {{ money(row.cgst_amount) }} · SGST {{ money(row.sgst_amount) }}
                  </template>
                  <template v-else>IGST {{ money(row.igst_amount) }}</template>
                  · Credit used {{ money(row.credit_used) }}
                </p>
              </td>
            </tr>
          </template>
          <tr v-if="!rows.length">
            <td :colspan="auth.isAdmin ? 8 : 5" class="muted">
              Nothing returned yet.
            </td>
          </tr>
        </tbody>
      </table>

      <div v-if="pages > 1" class="form-actions">
        <button class="btn" :disabled="page === 1" @click="page--; load()">Previous</button>
        <span class="muted small">Page {{ page }} of {{ pages }}</span>
        <button class="btn" :disabled="page >= pages" @click="page++; load()">Next</button>
      </div>
    </div>

    <ModalDialog
      v-if="applyFor"
      :title="`Set ${applyFor.debit_note_no} against an invoice`"
      @close="closeApply"
    >
      <form class="stack" @submit.prevent="submitApply">
        <p class="muted small">
          {{ money(applyFor.credit_balance) }} available from
          {{ applyFor.supplier_name }}.
        </p>

        <label class="field">
          <span>Invoice</span>
          <select
            v-model="applyForm.purchase_invoice_id"
            required
            @change="onInvoiceChosen"
          >
            <option value="" disabled>Choose an invoice from this supplier</option>
            <option v-for="i in invoices" :key="i.id" :value="i.id">
              {{ i.supplier_invoice_no }} — {{ i.received_date }}
            </option>
          </select>
        </label>

        <p v-if="applied" class="muted small">
          Invoice {{ money(applied.invoice_total) }} · already settled
          {{ money(applied.total_credit_applied) }} · left to pay
          <strong>{{ money(applied.balance_payable) }}</strong>
        </p>

        <label class="field">
          <span>Amount to set against it</span>
          <input v-model="applyForm.amount" inputmode="decimal" required />
          <p class="field__hint">
            The invoice's taxable value and its input tax are unchanged — this only
            changes what is left to pay.
          </p>
        </label>

        <p v-if="applyError" class="alert alert--error">{{ applyError }}</p>

        <div class="form-actions">
          <button class="btn btn--primary" type="submit" :disabled="applySaving">
            {{ applySaving ? 'Applying…' : 'Apply credit' }}
          </button>
          <button class="btn" type="button" @click="closeApply">Cancel</button>
        </div>
      </form>
    </ModalDialog>
  </section>
</template>

<style scoped>
.clickable {
  cursor: pointer;
}

.expanded td {
  background: var(--surface-2, #fafafa);
}

.actions {
  white-space: nowrap;
}

.small {
  font-size: 0.85rem;
}
</style>
