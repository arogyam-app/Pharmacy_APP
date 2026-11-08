<script setup>
/**
 * Expiry register — closing out stock that went out of date.
 *
 * The POS already refuses to sell expired goods, so this screen is not
 * about prevention. It is about the money: write the loss off, or send the
 * goods back to the distributor against a debit note and chase the credit.
 */
import { computed, onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'

import { listSuppliers } from '@/api/purchases'
import {
  listExpiryRegister,
  recordExpiryCredit,
  returnExpiredToSupplier,
  scanExpiry,
  writeOff,
} from '@/api/stockwatch'
import { ItemType } from '@/constants'
import { useAuthStore } from '@/stores/auth'

const rupees = new Intl.NumberFormat('en-IN', {
  style: 'currency',
  currency: 'INR',
  minimumFractionDigits: 2,
})
const money = (v) => (v === null || v === undefined ? '—' : rupees.format(Number(v)))
const trim = (v) => Number(v).toLocaleString('en-IN', { maximumFractionDigits: 3 })

const STATUS_LABEL = {
  IDENTIFIED: 'To action',
  WRITTEN_OFF: 'Written off',
  RETURNED_TO_SUPPLIER: 'Sent back',
  CREDIT_RECEIVED: 'Credit received',
}

const rows = ref([])
const suppliers = ref([])
const status = ref('IDENTIFIED')
const loading = ref(true)
const busy = ref(false)
const error = ref(null)
const notice = ref(null)

async function load() {
  loading.value = true
  error.value = null
  try {
    rows.value = (
      await listExpiryRegister({
        status: status.value || undefined,
        item_type: kind.value || undefined,
        page_size: 200,
      })
    ).items
  } catch (err) {
    error.value = err.message
  } finally {
    loading.value = false
  }
}

const auth = useAuthStore()
const route = useRoute()

// `/medicine/expiry` and `/general/expiry` are this same screen, scoped by
// the route so each side of the shop closes out its own losses.
const kind = computed(() => route.meta.itemType || '')
const isGeneral = computed(() => kind.value === ItemType.GENERAL)
// The register can be handed to an executive; what the loss cost the
// shop cannot. The server omits the field — this hides the column so
// it does not render empty (CLAUDE.md rule 4).
const showCost = computed(() => auth.isAdmin)

const totals = computed(() => ({
  atCost: rows.value.reduce((sum, r) => sum + Number(r.cost_value || 0), 0),
  atMrp: rows.value.reduce((sum, r) => sum + Number(r.mrp_value || 0), 0),
}))

async function scan() {
  busy.value = true
  error.value = null
  try {
    notice.value = (await scanExpiry()).message
    await load()
  } catch (err) {
    error.value = err.message
  } finally {
    busy.value = false
  }
}

async function bin(row) {
  const ok = confirm(
    `Write off ${row.medicine_name}, batch ${row.batch_no}?\n\n` +
      `${trim(row.qty_units)} unit(s) — ${money(row.cost_value)} at cost.\n\n` +
      'The stock is zeroed through the ledger and the batch is blocked so it ' +
      'cannot be adjusted back onto the shelf.',
  )
  if (!ok) return

  error.value = null
  try {
    const notes = prompt('Note (optional):') || null
    await writeOff(row.id, notes)
    notice.value = `Batch ${row.batch_no} written off.`
    await load()
  } catch (err) {
    error.value = err.message
  }
}

// --- send back to the supplier -------------------------------------------
const sendingFor = ref(null)
const form = ref({ supplier_id: '', debit_note_ref: '', credit_expected: '', notes: '' })

function startReturn(row) {
  sendingFor.value = row
  form.value = {
    supplier_id: row.supplier_id || '',
    debit_note_ref: '',
    credit_expected: row.cost_value || '',
    notes: '',
  }
}

async function sendBack() {
  if (!form.value.supplier_id || !form.value.debit_note_ref.trim()) {
    error.value = 'A supplier and a debit-note reference are both needed.'
    return
  }
  error.value = null
  try {
    await returnExpiredToSupplier(sendingFor.value.id, {
      // A key is a UUID string; Number() would make it NaN.
      supplier_id: form.value.supplier_id,
      debit_note_ref: form.value.debit_note_ref.trim(),
      credit_expected: form.value.credit_expected || null,
      notes: form.value.notes.trim() || null,
    })
    notice.value = `Batch ${sendingFor.value.batch_no} sent back on ${form.value.debit_note_ref}.`
    sendingFor.value = null
    await load()
  } catch (err) {
    error.value = err.message
  }
}

async function markCredited(row) {
  const amount = prompt(
    `Credit received for batch ${row.batch_no}?\n\n` +
      `Expected ${money(row.credit_received_amount)}. Enter the actual amount:`,
    row.credit_received_amount || '',
  )
  if (amount === null || amount === '') return

  error.value = null
  try {
    await recordExpiryCredit(row.id, amount)
    notice.value = `Credit of ${money(amount)} recorded against ${row.batch_no}.`
    await load()
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
      <h1>{{ isGeneral ? 'Expiry item' : 'Expiry medicine' }}</h1>
      <p class="muted">
        Expired {{ isGeneral ? 'general goods' : 'medicines' }}. The counter already refuses
        to sell them, so what is left to decide is whether the stock is written off as a loss
        or goes back to the distributor for credit. Goods with no shelf life never appear
        here.
      </p>
    </div>

    <div class="card stack">
      <div class="toolbar">
        <select v-model="status" @change="load">
          <option value="IDENTIFIED">To action</option>
          <option value="WRITTEN_OFF">Written off</option>
          <option value="RETURNED_TO_SUPPLIER">Sent back</option>
          <option value="CREDIT_RECEIVED">Credit received</option>
          <option value="">All</option>
        </select>
        <button class="btn btn--primary" :disabled="busy" @click="scan">
          {{ busy ? 'Scanning…' : 'Scan for expired stock' }}
        </button>
        <span v-if="rows.length" class="muted small">
          {{ rows.length }} batch(es) ·
          <template v-if="showCost">{{ money(totals.atCost) }} at cost · </template>
          {{ money(totals.atMrp) }} at MRP
        </span>
      </div>

      <p v-if="error" class="alert alert--error">{{ error }}</p>
      <p v-if="notice" class="alert alert--ok">{{ notice }}</p>
      <p v-if="loading" class="muted">Loading…</p>
      <p v-else-if="!rows.length" class="muted">
        Nothing here. <strong>Scan</strong> pulls in any expired batch that still holds stock.
      </p>

      <div v-else class="table-wrap">
        <table class="data">
          <thead>
            <tr>
              <th>Medicine</th>
              <th>Batch</th>
              <th>Expired</th>
              <th class="numeric">Units</th>
              <th v-if="showCost" class="numeric">At cost</th>
              <th class="numeric">At MRP</th>
              <th>Status</th>
              <th>Reference</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="row in rows" :key="row.id">
              <td>{{ row.medicine_name }}</td>
              <td>{{ row.batch_no }}</td>
              <td>
                {{ row.expiry_date }}
                <span class="badge badge--danger">{{ row.days_expired }}d ago</span>
              </td>
              <td class="numeric">{{ trim(row.qty_units) }}</td>
              <td v-if="showCost" class="numeric">{{ money(row.cost_value) }}</td>
              <td class="numeric">{{ money(row.mrp_value) }}</td>
              <td>
                <span
                  :class="[
                    'badge',
                    row.status === 'IDENTIFIED'
                      ? 'badge--warn'
                      : row.status === 'CREDIT_RECEIVED'
                        ? 'badge--ok'
                        : 'badge--danger',
                  ]"
                >
                  {{ STATUS_LABEL[row.status] || row.status }}
                </span>
              </td>
              <td>
                <template v-if="row.debit_note_ref">
                  {{ row.debit_note_ref }}
                  <small class="muted"><br />{{ row.supplier_name }}</small>
                </template>
                <span v-else class="muted">—</span>
              </td>
              <td class="numeric actions">
                <template v-if="row.status === 'IDENTIFIED'">
                  <button class="btn btn--sm btn--danger" @click="bin(row)">Write off</button>
                  <button class="btn btn--sm" @click="startReturn(row)">Send back</button>
                </template>
                <button
                  v-else-if="row.status === 'RETURNED_TO_SUPPLIER'"
                  class="btn btn--sm btn--primary"
                  @click="markCredited(row)"
                >
                  Credit received
                </button>
                <span v-else class="muted small">{{ row.actioned_by_name }}</span>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>

    <!-- send back to the supplier -->
    <div v-if="sendingFor" class="card stack">
      <h2>Send back — {{ sendingFor.medicine_name }}, batch {{ sendingFor.batch_no }}</h2>
      <p class="muted small">
        {{ trim(sendingFor.qty_units) }} unit(s)<template v-if="showCost">,
        {{ money(sendingFor.cost_value) }} at cost</template>.
        The stock leaves the shop either way; the debit note is what lets you chase the credit.
      </p>

      <div class="form-grid">
        <label class="field">
          <span>Supplier</span>
          <select v-model="form.supplier_id">
            <option value="">Choose…</option>
            <option v-for="s in suppliers" :key="s.id" :value="s.id">{{ s.name }}</option>
          </select>
        </label>
        <label class="field">
          <span>Debit note reference</span>
          <input v-model="form.debit_note_ref" placeholder="DN-2026-14" />
        </label>
        <label class="field">
          <span>Credit expected (₹)</span>
          <input v-model="form.credit_expected" type="number" step="0.01" min="0" />
        </label>
        <label class="field">
          <span>Note</span>
          <input v-model="form.notes" />
        </label>
      </div>

      <div class="form-actions">
        <button class="btn btn--primary" @click="sendBack">Send back</button>
        <button class="btn" @click="sendingFor = null">Cancel</button>
      </div>
    </div>
  </section>
</template>

<style scoped>
.actions {
  white-space: nowrap;
}

.actions button + button {
  margin-left: 0.3rem;
}
</style>
