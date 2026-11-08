<script setup>
/**
 * Inventory — where the money lives.
 *
 * MRP, GST rate and the discount limits are entered against a batch rather
 * than the medicine, because each consignment arrives with its own printed
 * price and whatever tax slab applies that day.
 */
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { useRoute } from 'vue-router'

import {
  adjustBatch,
  deleteBatch,
  listBatches,
  listStock,
  updateBatch,
} from '@/api/inventory'
import { addPurchaseItems, listSuppliers, listPurchases } from '@/api/purchases'
import { createPurchaseReturn } from '@/api/purchaseReturns'
import ModalDialog from '@/components/ModalDialog.vue'
import { useDebounced } from '@/composables/useDebounced'
import {
  EXPIRY_BASIS_LABEL,
  ExpiryBasis,
  ItemType,
  SELLABLE_UNIT_LABEL,
} from '@/constants'
import { useAuthStore } from '@/stores/auth'

const auth = useAuthStore()
const route = useRoute()

const rupees = new Intl.NumberFormat('en-IN', {
  style: 'currency',
  currency: 'INR',
  minimumFractionDigits: 2,
})
const money = (v) => (v === null || v === undefined ? '—' : rupees.format(Number(v)))
const trim = (v) => Number(v).toLocaleString('en-IN', { maximumFractionDigits: 3 })

// --- stock on hand --------------------------------------------------------
const stockRows = ref([])
const stockTotal = ref(0)
const loadingStock = ref(true)
const stockError = ref(null)
const stockQuery = ref('')
const debouncedStockQuery = useDebounced(stockQuery, 250)
const lowStockOnly = ref(false)

// Which half of the shop this screen is. `/medicine/inventory` and
// `/general/inventory` are the same component scoped by the route, so there
// is one implementation rather than two that drift apart. Without the meta
// — an old bookmark, say — it shows everything.
const kind = computed(() => route.meta.itemType || '')
const isGeneral = computed(() => kind.value === ItemType.GENERAL)
const noun = computed(() => (isGeneral.value ? 'product' : 'medicine'))

async function loadStock() {
  loadingStock.value = true
  stockError.value = null
  try {
    const data = await listStock({
      q: stockQuery.value.trim() || undefined,
      low_stock_only: lowStockOnly.value || undefined,
      item_type: kind.value || undefined,
      // Stock on hand means stock: a product catalogued but never bought
      // has nothing to show and only pads the list. Once purchased it stays,
      // at zero, because a line that has run out is exactly what a stock
      // screen is for.
      stocked_only: true,
      page_size: 200,
    })
    stockRows.value = data.items
    stockTotal.value = data.total
  } catch (err) {
    stockError.value = err.message
  } finally {
    loadingStock.value = false
  }
}

watch([debouncedStockQuery, lowStockOnly, kind], loadStock)

// --- batches for one medicine --------------------------------------------
const openMedicine = ref(null)
const batches = ref([])
const loadingBatches = ref(false)

async function showBatches(row) {
  openMedicine.value = row
  loadingBatches.value = true
  try {
    const data = await listBatches({
      medicine_id: row.medicine_id,
      include_expired: true,
      include_empty: true,
      page_size: 100,
    })
    batches.value = data.items
  } catch (err) {
    stockError.value = err.message
  } finally {
    loadingBatches.value = false
  }
}

async function toggleBlock(batch) {
  try {
    await updateBatch(batch.id, {
      is_blocked: !batch.is_blocked,
      block_reason: batch.is_blocked ? null : 'Quarantined',
    })
    await refreshBatches()
  } catch (err) {
    batchError.value = err.message
  }
}

async function refreshBatches() {
  if (openMedicine.value) await showBatches(openMedicine.value)
  await loadStock()
}

// --- edit a batch ---------------------------------------------------------
const editingBatch = ref(null)
const savingBatch = ref(false)
const batchError = ref(null)
const batchNotice = ref(null)

function startEditBatch(batch) {
  batchError.value = null
  batchNotice.value = null
  editingBatch.value = {
    id: batch.id,
    batch_no: batch.batch_no,
    // Goods with no shelf life have a sentinel date, not one to correct.
    has_expiry: batch.has_expiry !== false,
    expiry_date: batch.expiry_date,
    mrp_per_pack: batch.mrp_per_pack,
    cost_per_pack: batch.cost_per_pack ?? '',
    gst_rate: String(Number(batch.gst_rate)),
    default_discount_pct: batch.default_discount_pct ?? '',
    max_discount_pct: batch.max_discount_pct ?? '',
  }
}

async function saveBatch() {
  savingBatch.value = true
  batchError.value = null
  try {
    await updateBatch(editingBatch.value.id, {
      ...(editingBatch.value.has_expiry
        ? { expiry_date: editingBatch.value.expiry_date }
        : {}),
      mrp_per_pack: String(editingBatch.value.mrp_per_pack),
      cost_per_pack: nullable(editingBatch.value.cost_per_pack),
      gst_rate: editingBatch.value.gst_rate,
      default_discount_pct: nullable(editingBatch.value.default_discount_pct),
      max_discount_pct: nullable(editingBatch.value.max_discount_pct),
    })
    batchNotice.value = `Batch ${editingBatch.value.batch_no} updated.`
    editingBatch.value = null
    await refreshBatches()
  } catch (err) {
    batchError.value = err.message
  } finally {
    savingBatch.value = false
  }
}

/** Quantities move through the ledger, so a correction needs a reason. */
async function adjustQuantity(batch) {
  const raw = prompt(
    `Adjust ${batch.batch_no} (currently ${trim(batch.qty_available_units)} units).\n` +
      'Enter a signed change, e.g. -5 for breakage or 12 for a recount.',
  )
  if (raw === null || raw.trim() === '') return
  const delta = Number(raw)
  if (!Number.isFinite(delta) || delta === 0) {
    batchError.value = 'Enter a non-zero number.'
    return
  }
  const reason = prompt('Reason for the adjustment (recorded in the stock ledger):')
  if (!reason || reason.trim().length < 3) {
    batchError.value = 'An adjustment needs a reason of at least 3 characters.'
    return
  }
  try {
    await adjustBatch(batch.id, delta, reason.trim())
    batchError.value = null
    batchNotice.value = `Adjusted ${batch.batch_no} by ${delta > 0 ? '+' : ''}${delta}.`
    await refreshBatches()
  } catch (err) {
    batchError.value = err.message
  }
}

async function removeBatch(batch) {
  const ok = confirm(
    `Delete batch ${batch.batch_no}?\n\n` +
      'Only possible while nothing has been sold from it. Its stock ledger ' +
      'rows go with it, and the deletion is recorded in the audit log.',
  )
  if (!ok) return
  try {
    await deleteBatch(batch.id)
    batchError.value = null
    batchNotice.value = `Batch ${batch.batch_no} deleted.`
    await refreshBatches()
  } catch (err) {
    batchError.value = err.message
  }
}

// --- returning goods to the distributor -----------------------------------
//
// Not an adjustment. An adjustment writes stock off and the value is simply
// lost; a return sends it back, reverses the input tax claimed on it and
// leaves credit with the supplier to set against a later invoice. Choosing
// the wrong one of the two costs the shop the whole value of the goods, so
// they are separate buttons with separate words on them.

const returnFor = ref(null)
const returnSuppliers = ref([])
const returnSaving = ref(false)
const returnError = ref(null)
const returnForm = reactive({
  supplier_id: '',
  qty_units: '',
  reason: '',
  supplier_credit_note_ref: '',
})

async function startReturn(batch) {
  returnFor.value = batch
  returnError.value = null
  returnForm.supplier_id = batch.supplier_id || ''
  returnForm.qty_units = ''
  returnForm.reason = ''
  returnForm.supplier_credit_note_ref = ''
  try {
    const page = await listSuppliers({ page_size: 200 })
    returnSuppliers.value = page.items || []
  } catch (err) {
    returnError.value = err.message
  }
}

function closeReturn() {
  returnFor.value = null
  returnError.value = null
}

async function submitReturn() {
  const qty = Number(returnForm.qty_units)
  if (!Number.isFinite(qty) || qty <= 0) {
    returnError.value = 'Enter how many units are going back.'
    return
  }
  if (qty > Number(returnFor.value.qty_available_units)) {
    returnError.value = `Only ${trim(returnFor.value.qty_available_units)} units are on the shelf.`
    return
  }
  if (returnForm.reason.trim().length < 3) {
    returnError.value = 'Say why the goods are going back — it is on the document.'
    return
  }
  returnSaving.value = true
  returnError.value = null
  try {
    const note = await createPurchaseReturn({
      supplier_id: returnForm.supplier_id,
      reason: returnForm.reason.trim(),
      supplier_credit_note_ref: returnForm.supplier_credit_note_ref.trim() || null,
      items: [{ batch_id: returnFor.value.id, qty_units: String(qty) }],
    })
    batchNotice.value =
      `Debit note ${note.debit_note_no} raised` +
      (note.total_amount ? ` — ${money(note.total_amount)} credit with the supplier.` : '.')
    batchError.value = null
    closeReturn()
    await refreshBatches()
  } catch (err) {
    returnError.value = err.message
  } finally {
    returnSaving.value = false
  }
}

// --- update stock ---------------------------------------------------------
//
// Receiving a consignment and correcting one are the same conversation about
// the same batch, so they are one panel. Pick the medicine, pick the batch it
// came in on, change what needs changing. Only medicines that have been
// bought before are listed: a brand's *first* consignment arrives through
// Purchases → Receive goods, against a supplier invoice.
const canUpdateStock = computed(() => auth.canAny('inventory:adjust', 'purchases:receive'))

const medicineQuery = ref('')
const debouncedMedicineQuery = useDebounced(medicineQuery, 250)
const medicineMatches = ref([])
const chosenMedicine = ref(null)

watch(debouncedMedicineQuery, async (value) => {
  const term = value.trim()
  if (term.length < 2) {
    medicineMatches.value = []
    return
  }
  try {
    const data = await listStock({
      q: term,
      stocked_only: true,
      item_type: kind.value || undefined,
      page_size: 15,
    })
    medicineMatches.value = data.items
  } catch {
    medicineMatches.value = []
  }
})

// --- the batch being updated ---
const medicineBatches = ref([])
const loadingMedicineBatches = ref(false)
const selectedBatchId = ref('')

const selectedBatch = computed(
  () => medicineBatches.value.find((b) => String(b.id) === selectedBatchId.value) || null,
)

/** Units in one pack — the batch's own snapshot wins over the master's,
 *  because editing the master must not restate what physically arrived. */
const packUnits = computed(
  () => Number(selectedBatch.value?.units_per_pack || chosenMedicine.value?.units_per_pack || 1),
)

// --- which supplier invoice this consignment came in on -------------------
//
// Required before a batch that is not already on file can be recorded. It is
// searched as you type and can only be *selected*: a typed invoice number
// that matches nothing would be a document nobody can produce.
const invoice = ref(null)
const invoiceQuery = ref('')
const invoiceMatches = ref([])
const searchingInvoices = ref(false)
let invoiceTimer = null

function onInvoiceSearch() {
  invoice.value = null
  clearTimeout(invoiceTimer)
  const term = invoiceQuery.value.trim()
  if (term.length < 1) {
    invoiceMatches.value = []
    return
  }
  invoiceTimer = setTimeout(async () => {
    searchingInvoices.value = true
    try {
      const data = await listPurchases({
        q: term,
        include_cancelled: false,
        page_size: 10,
      })
      invoiceMatches.value = data.items
    } catch {
      invoiceMatches.value = []
    } finally {
      searchingInvoices.value = false
    }
  }, 250)
}

function chooseInvoice(row) {
  invoice.value = row
  invoiceQuery.value = row.supplier_invoice_no
  invoiceMatches.value = []
}

function clearInvoice() {
  invoice.value = null
  invoiceQuery.value = ''
  invoiceMatches.value = []
}

const blank = () => ({
  batch_no: '',
  expiry_basis: ExpiryBasis.EXPIRY_DATE,
  expiry_date: '',
  mfg_date: '',
  best_before_months: '',
  qty_packs: '',
  mrp_per_pack: '',
  cost_per_pack: '',
  gst_rate: '12',
  default_discount_pct: '',
  max_discount_pct: '',
  received_date: '',
  reason: '',
})

const form = reactive(blank())
const saving = ref(false)
const formError = ref(null)
const formNotice = ref(null)

async function chooseMedicine(row) {
  chosenMedicine.value = row
  medicineQuery.value = ''
  medicineMatches.value = []
  formError.value = null
  formNotice.value = null
  await loadMedicineBatches()
}

function clearMedicine() {
  chosenMedicine.value = null
  medicineBatches.value = []
  selectedBatchId.value = ''
  Object.assign(form, blank())
  formError.value = null
}

async function loadMedicineBatches(keepId = null) {
  if (!chosenMedicine.value) return
  loadingMedicineBatches.value = true
  try {
    const data = await listBatches({
      medicine_id: chosenMedicine.value.medicine_id,
      include_expired: true,
      include_empty: true,
      page_size: 100,
    })
    medicineBatches.value = data.items
    // Default to the batch the counter is selling from — nearest expiry
    // first, which is the one a top-up or a price correction usually means.
    selectBatch(keepId ?? data.items[0]?.id ?? '')
  } catch (err) {
    medicineBatches.value = []
    selectBatch('')
    formError.value = err.message
  } finally {
    loadingMedicineBatches.value = false
  }
}

/** Picking a batch fills the form from it; every field stays editable. */
function selectBatch(id) {
  selectedBatchId.value = id === '' || id === null ? '' : String(id)
  const batch = selectedBatch.value
  if (!batch) {
    const medicine = chosenMedicine.value
    Object.assign(form, blank(), {
      mrp_per_pack: medicine?.mrp_per_pack ?? '',
      gst_rate: medicine?.gst_rate ? String(Number(medicine.gst_rate)) : '12',
    })
    return
  }
  Object.assign(form, {
    batch_no: batch.batch_no,
    expiry_basis: batch.expiry_basis || ExpiryBasis.EXPIRY_DATE,
    // The sentinel behind "does not expire" is never shown as a date.
    expiry_date: batch.has_expiry === false ? '' : batch.expiry_date,
    mfg_date: batch.mfg_date ?? '',
    best_before_months: batch.best_before_months ?? '',
    qty_packs: String(batch.qty_available_packs),
    mrp_per_pack: String(batch.mrp_per_pack),
    // Absent, not blank, from the response a non-admin gets (rule 4).
    cost_per_pack: batch.cost_per_pack ?? '',
    gst_rate: String(Number(batch.gst_rate)),
    default_discount_pct: batch.default_discount_pct ?? '',
    max_discount_pct: batch.max_discount_pct ?? '',
    received_date: batch.received_date ?? '',
    reason: '',
  })
}

const packNoun = computed(() =>
  chosenMedicine.value
    ? SELLABLE_UNIT_LABEL[chosenMedicine.value.pack_unit] || 'unit'
    : 'unit',
)

// Stock is stored to three decimals; round the same way here so a keyed
// figure identical to the stored one never produces a phantom movement.
const round3 = (n) => Math.round(n * 1000) / 1000

const unitsPreview = computed(() => {
  if (!chosenMedicine.value || form.qty_packs === '') return null
  return `${trim(round3(Number(form.qty_packs) * packUnits.value))} sellable ${packNoun.value}(s)`
})

const unitPricePreview = computed(() => {
  if (!chosenMedicine.value || !form.mrp_per_pack) return null
  const per = Number(form.mrp_per_pack) / packUnits.value
  return `${rupees.format(per)} per ${packNoun.value}`
})

/** Signed units between what is keyed and what the ledger says is there. */
const qtyDelta = computed(() => {
  const batch = selectedBatch.value
  if (!batch || form.qty_packs === '' || !Number.isFinite(Number(form.qty_packs))) return 0
  return round3(round3(Number(form.qty_packs) * packUnits.value) - Number(batch.qty_available_units))
})

/** "exp 2028-03-15", or "no expiry" for goods with no shelf life. */
const expiryLabel = (batch) =>
  batch.has_expiry === false ? 'no expiry' : `exp ${batch.expiry_date}`

/**
 * The shelf date a mfg date + best-before period works out to.
 *
 * Shown while typing so the operator sees what the arithmetic gives before
 * committing — the server does the same sum, and this is only a preview.
 */
const computedExpiry = computed(() => {
  if (form.expiry_basis !== ExpiryBasis.BEST_BEFORE) return null
  const months = Number(form.best_before_months)
  if (!form.mfg_date || !Number.isFinite(months) || months < 1) return null
  const from = new Date(`${form.mfg_date}T00:00:00`)
  if (Number.isNaN(from.getTime())) return null
  const target = new Date(from)
  target.setMonth(target.getMonth() + months)
  // Rolling past the end of a short month lands in the next one — 31 Jan
  // plus one month must be 28 Feb, not 3 March.
  if (target.getDate() !== from.getDate()) target.setDate(0)
  return target.toISOString().slice(0, 10)
})

/** Only the commercial terms that actually differ, so a PATCH says what changed. */
const termChanges = computed(() => {
  const batch = selectedBatch.value
  if (!batch) return {}

  const changes = {}
  const same = (typed, stored) => {
    const a = nullable(typed)
    const b = stored === null || stored === undefined ? null : String(stored)
    if (a === null || b === null) return a === b
    // Dates compare as text; everything else as a number, so "12.00" and
    // "12" are the same figure.
    return a.includes('-') || b.includes('-') ? a === b : Number(a) === Number(b)
  }

  // Shelf life travels as a set: the server re-resolves the basis with its
  // two dates, so switching from a printed expiry to a best-before period
  // drops the old date instead of leaving both on the batch.
  const basisChanged = form.expiry_basis !== (batch.expiry_basis || ExpiryBasis.EXPIRY_DATE)
  const expiryEdited =
    (form.expiry_basis === ExpiryBasis.EXPIRY_DATE &&
      form.expiry_date &&
      form.expiry_date !== batch.expiry_date) ||
    (form.expiry_basis === ExpiryBasis.BEST_BEFORE &&
      (!same(form.mfg_date, batch.mfg_date) ||
        !same(form.best_before_months, batch.best_before_months)))

  if (basisChanged || expiryEdited) {
    changes.expiry_basis = form.expiry_basis
    if (form.expiry_basis === ExpiryBasis.EXPIRY_DATE) {
      changes.expiry_date = form.expiry_date
    } else if (form.expiry_basis === ExpiryBasis.BEST_BEFORE) {
      changes.mfg_date = form.mfg_date
      changes.best_before_months = Number(form.best_before_months)
    }
  }
  if (!same(form.mrp_per_pack, batch.mrp_per_pack)) {
    changes.mrp_per_pack = String(form.mrp_per_pack)
  }
  // Cost is missing from a non-admin's copy of the batch, so there is nothing
  // to compare and nothing to send — omitting it leaves the stored figure be.
  if (auth.isAdmin && !same(form.cost_per_pack, batch.cost_per_pack)) {
    changes.cost_per_pack = nullable(form.cost_per_pack)
  }
  if (!same(form.gst_rate, batch.gst_rate)) changes.gst_rate = form.gst_rate
  if (!same(form.default_discount_pct, batch.default_discount_pct)) {
    changes.default_discount_pct = nullable(form.default_discount_pct)
  }
  if (!same(form.max_discount_pct, batch.max_discount_pct)) {
    changes.max_discount_pct = nullable(form.max_discount_pct)
  }
  return changes
})

const pendingTerms = computed(() => Object.keys(termChanges.value).length)

/**
 * What the pack earns, and how far its price can be cut.
 *
 * MRP is GST-inclusive; the distributor's rate is not. Subtracting one from
 * the other overstates the margin by the whole of the tax — on an 18 % line
 * that is most of the apparent profit. The tax carved out of MRP goes to the
 * government, so the real margin is (MRP net of GST − cost), and the
 * headroom is that margin as a percentage **of MRP**, because that is what a
 * counter discount is taken off.
 */
const marginPreview = computed(() => {
  const mrp = Number(form.mrp_per_pack)
  const cost = Number(form.cost_per_pack)
  if (!mrp || !cost) return null

  const mrpExGst = mrp / (1 + Number(form.gst_rate || 0) / 100)
  const margin = mrpExGst - cost
  if (margin <= 0) {
    return `Selling at a loss: cost ${money(cost)} against ${money(mrpExGst)} net of GST.`
  }
  const headroom = (margin / mrp) * 100
  return (
    `Margin ${money(margin)} per pack — ${((margin / cost) * 100).toFixed(1)}% on cost. ` +
    `A discount above ${headroom.toFixed(1)}% sells at a loss.`
  )
})

function nullable(value) {
  const t = String(value ?? '').trim()
  return t === '' ? null : t
}

function updateStock() {
  if (!chosenMedicine.value) {
    formError.value = 'Choose a medicine first.'
    return
  }
  formError.value = null
  formNotice.value = null
  return selectedBatch.value ? amendBatch(selectedBatch.value) : receiveNewBatch()
}

/**
 * An existing batch: terms are corrected in place, quantity moves through the
 * ledger. They are two calls because they are two different kinds of record —
 * a price correction rewrites a fact, a quantity change adds a movement that
 * has to explain itself (CLAUDE.md rule 3).
 */
async function amendBatch(batch) {
  const delta = qtyDelta.value
  const changes = termChanges.value
  if (!delta && !pendingTerms.value) {
    formError.value = 'Nothing has changed on this batch yet.'
    return
  }
  const reason = form.reason.trim()
  if (delta && reason.length < 3) {
    formError.value =
      'A quantity change needs a reason of at least 3 characters — it is written to the stock ledger.'
    return
  }

  saving.value = true
  try {
    const done = []
    if (pendingTerms.value) {
      await updateBatch(batch.id, changes)
      done.push(`${pendingTerms.value} term(s) updated`)
    }
    if (delta) {
      await adjustBatch(batch.id, delta, reason)
      done.push(`${delta > 0 ? '+' : '−'}${trim(Math.abs(delta))} ${packNoun.value}(s)`)
    }
    formNotice.value = `Batch ${batch.batch_no}: ${done.join(', ')}.`
    await refreshAfterWrite(batch.id)
  } catch (err) {
    formError.value = err.message
  } finally {
    saving.value = false
  }
}

/**
 * A batch that is not on file yet becomes a **line on a supplier invoice**,
 * not a free-standing batch.
 *
 * Stock that appears with no document behind it cannot be reconciled with
 * what the shop was billed, and it is the gap a stock loss hides in. So this
 * screen no longer creates batches on its own: it adds the item to an
 * invoice already received, which is also how a medicine and a general item
 * entered from two different screens end up on the same document.
 */
async function receiveNewBatch() {
  if (!invoice.value) {
    formError.value =
      'Choose the supplier invoice this batch arrived on, or record the purchase first.'
    return
  }
  saving.value = true
  try {
    const line = {
      medicine_id: chosenMedicine.value.medicine_id,
      batch_no: form.batch_no.trim(),
      // Whatever the carton stated; the server turns the three into one
      // shelf date.
      expiry_basis: form.expiry_basis,
      expiry_date: form.expiry_basis === ExpiryBasis.EXPIRY_DATE ? form.expiry_date : null,
      mfg_date: form.expiry_basis === ExpiryBasis.BEST_BEFORE ? form.mfg_date : null,
      best_before_months:
        form.expiry_basis === ExpiryBasis.BEST_BEFORE
          ? Number(form.best_before_months)
          : null,
      qty_packs: String(form.qty_packs),
      mrp_per_pack: String(form.mrp_per_pack),
      gst_rate: form.gst_rate,
      default_discount_pct: nullable(form.default_discount_pct),
      max_discount_pct: nullable(form.max_discount_pct),
      // The invoice is the supplier's bill, so a cost belongs on the line.
      // Blank means the goods were free of charge, not unknown.
      cost_per_pack: String(form.cost_per_pack),
    }

    const updated = await addPurchaseItems(invoice.value.id, [line])
    formNotice.value =
      `${form.batch_no.trim()} added to invoice ${updated.supplier_invoice_no} ` +
      `(${updated.doc_number}) — ${updated.items.length} item(s) on it now.`
    if (updated.warnings?.length) formNotice.value += ` ${updated.warnings.join(' ')}`
    await refreshAfterWrite()
  } catch (err) {
    formError.value = err.message
  } finally {
    saving.value = false
  }
}

/** Re-read everything this write touched, and stay on the batch just changed. */
async function refreshAfterWrite(batchId = null) {
  await loadStock()
  // A batch created through the invoice comes back on the reloaded list; the
  // newest is the one just added, so select it rather than guessing an id.
  await loadMedicineBatches(batchId)
  form.reason = ''
  if (openMedicine.value?.medicine_id === chosenMedicine.value?.medicine_id) {
    await showBatches(openMedicine.value)
  }
}

onMounted(loadStock)
</script>

<template>
  <section class="stack">
    <div class="card stack">
      <h1>{{ isGeneral ? 'Product inventory' : 'Med inventory' }}</h1>
      <p class="muted">
        Price and tax belong to the consignment, not the medicine: each batch carries its own
        <strong>MRP</strong>, <strong>GST rate</strong> and <strong>discount limits</strong>, so a
        price rise or a slab change only affects stock received after it.
      </p>
    </div>

    <div v-if="canUpdateStock" class="card stack">
      <h2>Update stock</h2>

      <div v-if="!chosenMedicine">
        <p class="muted">
          {{ isGeneral ? 'Products' : 'Medicines' }} already purchased. A first consignment
          comes in through <RouterLink to="/purchases">Purchases</RouterLink>, against a
          supplier invoice.
        </p>
        <label class="field">
          <span>Medicine</span>
          <input
            v-model="medicineQuery"
            type="search"
            placeholder="Search purchased stock by brand or generic name…"
          />
        </label>
        <ul v-if="medicineMatches.length" class="matches">
          <li v-for="m in medicineMatches" :key="m.medicine_id">
            <button type="button" @click="chooseMedicine(m)">
              <strong>{{ m.brand_name }}</strong>
              <span class="muted small">
                <template v-if="m.composition_text">{{ m.composition_text }} · </template>
                {{ m.manufacturer_name }} · {{ m.pack_label }} ·
                {{ trim(m.available_units) }} unit(s) in {{ m.batch_count }} batch(es)
              </span>
            </button>
          </li>
        </ul>
        <p v-if="medicineQuery.trim().length >= 2 && !medicineMatches.length" class="muted small">
          Nothing purchased matches that.
        </p>
      </div>

      <template v-else>
        <div class="chosen">
          <div>
            <strong>{{ chosenMedicine.brand_name }}</strong>
            <p class="muted small">
              <template v-if="chosenMedicine.composition_text">
                {{ chosenMedicine.composition_text }} ·
              </template>
              {{ chosenMedicine.pack_label }} ·
              {{ packUnits }} sellable {{ packNoun }}(s) per pack
            </p>
          </div>
          <button class="btn btn--sm" type="button" @click="clearMedicine">Change</button>
        </div>

        <label class="field">
          <span>Batch</span>
          <select
            :value="selectedBatchId"
            :disabled="loadingMedicineBatches"
            @change="selectBatch($event.target.value)"
          >
            <option v-for="b in medicineBatches" :key="b.id" :value="String(b.id)">
              {{ b.batch_no }} · {{ expiryLabel(b) }} · {{ trim(b.qty_available_units) }} unit(s)
              <template v-if="b.is_expired"> · expired</template>
              <template v-else-if="b.is_blocked"> · blocked</template>
            </option>
            <option value="">New batch…</option>
          </select>
          <p class="field__hint">
            <template v-if="loadingMedicineBatches">Loading batches…</template>
            <template v-else-if="!medicineBatches.length">
              No batch on file — this will record the first one.
            </template>
            <template v-else-if="selectedBatch">
              Fields are filled from this batch. Change any of them.
            </template>
            <template v-else>A new consignment of a {{ noun }} already on the shelf.</template>
          </p>
        </label>

        <!-- A batch not already on file becomes a line on a supplier
             invoice, so that every unit on the shelf traces to a document
             the shop was billed for. Searched as you type; only an invoice
             that exists can be chosen. -->
        <div v-if="!selectedBatch" class="field">
          <span class="field__label">Supplier invoice</span>
          <input
            v-model="invoiceQuery"
            type="search"
            placeholder="Search by invoice number…"
            @input="onInvoiceSearch"
          />

          <p v-if="invoice" class="chosen-invoice">
            <strong>{{ invoice.supplier_invoice_no }}</strong>
            <span class="muted small">
              {{ invoice.supplier_name }} · {{ invoice.doc_number }} ·
              received {{ invoice.received_date }} · {{ invoice.item_count }} item(s)
            </span>
            <button class="btn btn--sm" type="button" @click="clearInvoice">Change</button>
          </p>

          <ul v-else-if="invoiceMatches.length" class="matches">
            <li v-for="row in invoiceMatches" :key="row.id">
              <button type="button" @click="chooseInvoice(row)">
                <strong>{{ row.supplier_invoice_no }}</strong>
                <span class="muted small">
                  {{ row.supplier_name }} · {{ row.doc_number }} ·
                  received {{ row.received_date }} · {{ row.item_count }} item(s)
                </span>
              </button>
            </li>
          </ul>

          <p v-else-if="searchingInvoices" class="field__hint">Searching…</p>

          <p v-else class="field__hint">
            <template v-if="invoiceQuery.trim()">
              No invoice matches that.
              <RouterLink to="/purchases">Record the purchase</RouterLink> first — stock with
              no document behind it cannot be reconciled with what the shop was billed.
            </template>
            <template v-else>
              Required. The item joins that invoice, so a medicine and a general item keyed
              from different screens land on the same document.
            </template>
          </p>
        </div>

        <form class="stack" @submit.prevent="updateStock">
          <div class="form-grid">
            <label class="field">
              <span>Batch number <template v-if="isGeneral">(optional)</template></span>
              <input
                v-model="form.batch_no"
                :disabled="Boolean(selectedBatch)"
                :required="!isGeneral"
                maxlength="60"
              />
              <p v-if="selectedBatch" class="field__hint">
                Fixed once received — a different number is a different batch.
              </p>
            </label>
            <!-- Three things can be printed on a carton, and the counter
                 should key in whichever it is looking at: a medicine's
                 expiry, a bottle's "MFG 03/2026, best before 24 months", or
                 nothing at all on a comb. -->
            <label class="field">
              <span>Shelf life</span>
              <select v-model="form.expiry_basis">
                <option v-for="(label, value) in EXPIRY_BASIS_LABEL" :key="value" :value="value">
                  {{ label }}
                </option>
              </select>
            </label>
            <label v-if="form.expiry_basis === ExpiryBasis.EXPIRY_DATE" class="field">
              <span>Expiry date</span>
              <input v-model="form.expiry_date" type="date" required />
            </label>
            <template v-else-if="form.expiry_basis === ExpiryBasis.BEST_BEFORE">
              <label class="field">
                <span>Manufactured on</span>
                <input v-model="form.mfg_date" type="date" required />
              </label>
              <label class="field">
                <span>Best before (months)</span>
                <input
                  v-model="form.best_before_months"
                  type="number"
                  step="1"
                  min="1"
                  max="600"
                  required
                />
                <p v-if="computedExpiry" class="field__hint">Expires {{ computedExpiry }}.</p>
              </label>
            </template>
            <p v-else class="field field__hint">
              No shelf life — skipped by expiry alerts and the expiry register.
            </p>
            <label class="field">
              <span>{{ selectedBatch ? 'Quantity on hand (packs)' : 'Quantity (packs)' }}</span>
              <input
                v-model="form.qty_packs"
                type="number"
                step="0.001"
                :min="selectedBatch ? 0 : 0.001"
                required
              />
              <p v-if="selectedBatch && qtyDelta" class="field__hint field__hint--warn">
                {{ qtyDelta > 0 ? 'Adds' : 'Removes' }} {{ trim(Math.abs(qtyDelta)) }}
                {{ packNoun }}(s) — posted to the stock ledger.
              </p>
              <p v-else-if="unitsPreview" class="field__hint">{{ unitsPreview }}</p>
            </label>
            <label v-if="selectedBatch" class="field field--wide">
              <span>Reason {{ qtyDelta ? '(required)' : '' }}</span>
              <input
                v-model="form.reason"
                maxlength="200"
                :required="Boolean(qtyDelta)"
                placeholder="Recount, breakage, keying error, goods received…"
              />
              <p class="field__hint">
                Recorded against the movement. Only needed when the quantity changes.
              </p>
            </label>
            <label class="field">
              <span>MRP per pack (₹)</span>
              <input v-model="form.mrp_per_pack" type="number" step="0.01" min="0.01" required />
              <p v-if="unitPricePreview" class="field__hint">{{ unitPricePreview }}</p>
            </label>
            <!-- On a new batch this is a line on the supplier's invoice, so
                 whoever may receive goods keys what was billed — writing a
                 cost is not reading one, and it is never read back to them
                 (CLAUDE.md rule 4). On an existing batch it stays admin-only,
                 because there it *is* a stored figure being shown. -->
            <label v-if="auth.isAdmin || !selectedBatch" class="field">
              <span>Cost per pack (₹)</span>
              <input
                v-model="form.cost_per_pack"
                type="number"
                step="0.01"
                min="0"
                :required="!selectedBatch"
              />
              <p class="field__hint">
                <template v-if="!selectedBatch">
                  GST-exclusive, as the distributor billed it.
                </template>
                <template v-else>Never shown to sales staff.</template>
              </p>
            </label>
            <label class="field">
              <span>GST %</span>
              <select v-model="form.gst_rate">
                <option value="0">0</option>
                <option value="5">5</option>
                <option value="12">12</option>
                <option value="18">18</option>
                <option value="28">28</option>
              </select>
            </label>
            <label class="field">
              <span>Default discount %</span>
              <input v-model="form.default_discount_pct" type="number" step="0.01" min="0" max="100" />
              <p class="field__hint">Blank uses the store policy.</p>
            </label>
            <label class="field">
              <span>Max discount %</span>
              <input v-model="form.max_discount_pct" type="number" step="0.01" min="0" max="100" />
              <!-- Fine print, but the figure the ceiling should be set from. -->
              <p v-if="marginPreview" class="field__hint field__hint--margin">
                {{ marginPreview }}
              </p>
            </label>

          </div>

          <p v-if="formError" class="alert alert--error">{{ formError }}</p>
          <p v-if="formNotice" class="alert alert--ok">{{ formNotice }}</p>

          <div class="form-actions">
            <button
              class="btn btn--primary"
              type="submit"
              :disabled="saving || (!selectedBatch && !invoice)"
            >
              {{ saving ? 'Saving…' : selectedBatch ? 'Update stock' : 'Add to invoice' }}
            </button>
            <span v-if="selectedBatch" class="muted small">
              <template v-if="qtyDelta || pendingTerms">
                {{ [pendingTerms ? `${pendingTerms} term(s)` : null, qtyDelta ? 'quantity' : null]
                  .filter(Boolean)
                  .join(' and ') }} pending
              </template>
              <template v-else>No changes yet</template>
            </span>
          </div>
        </form>
      </template>

      <p v-if="formNotice && !chosenMedicine" class="alert alert--ok">{{ formNotice }}</p>
    </div>

    <div class="card stack">
      <h2>Stock on hand</h2>
      <p class="muted small">
        Everything purchased at least once, including what has since run out. A
        {{ noun }} never bought is not stock — add it from
        <RouterLink :to="isGeneral ? '/general/products' : '/medicine/catalogue'">
          the catalogue</RouterLink>, then receive it against an invoice.
      </p>
      <div class="form-actions">
        <input
          v-model="stockQuery"
          class="grow"
          type="search"
          :placeholder="`Filter ${noun}s…`"
        />
        <label class="inline">
          <input v-model="lowStockOnly" type="checkbox" />
          Low stock only
        </label>
      </div>

      <p v-if="stockError" class="alert alert--error">{{ stockError }}</p>
      <p v-else-if="loadingStock" class="muted">Loading…</p>
      <p v-else-if="!stockRows.length" class="muted">Nothing matches.</p>

      <div v-else class="table-wrap">
        <table class="data">
          <thead>
            <tr>
              <th>Brand</th>
              <th>Pack</th>
              <th class="numeric">Available</th>
              <th class="numeric">Packs</th>
              <th class="numeric">MRP</th>
              <th class="numeric">GST</th>
              <th>Nearest expiry</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            <tr
              v-for="row in stockRows"
              :key="row.medicine_id"
              :class="{ 'row--selected': openMedicine?.medicine_id === row.medicine_id }"
              @click="showBatches(row)"
            >
              <td>
                <strong>{{ row.brand_name }}</strong>
                <p class="composition">
                  {{ row.composition_text || (row.item_type === 'GENERAL' ? 'General item' : '') }}
                </p>
              </td>
              <td>{{ row.pack_label }}</td>
              <td class="numeric">
                {{ trim(row.available_units) }}
                <span v-if="row.is_low_stock" class="badge badge--warn">Low</span>
              </td>
              <td class="numeric">{{ trim(row.available_packs) }}</td>
              <td class="numeric">{{ money(row.mrp_per_pack) }}</td>
              <td class="numeric">{{ row.gst_rate ? `${row.gst_rate}%` : '—' }}</td>
              <td>
                <template v-if="row.nearest_expiry">
                  {{ row.nearest_expiry }}
                  <span v-if="row.has_near_expiry" class="badge badge--warn">Soon</span>
                </template>
                <span v-else class="muted">—</span>
              </td>
              <td class="numeric">{{ row.batch_count }} batch(es)</td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>

    <div v-if="openMedicine" class="card stack">
      <h2>Batches — {{ openMedicine.brand_name }}</h2>
      <p class="muted">
        Quantities are corrected with an adjustment, never edited directly, so the ledger keeps
        explaining how the current figure was reached.
      </p>

      <p v-if="batchError" class="alert alert--error">{{ batchError }}</p>
      <p v-if="batchNotice" class="alert alert--ok">{{ batchNotice }}</p>

      <p v-if="loadingBatches" class="muted">Loading…</p>
      <p v-else-if="!batches.length" class="muted">No batches recorded.</p>

      <!-- Edit form for one batch: prices, tax and limits, never quantity. -->
      <form v-if="editingBatch" class="card stack" @submit.prevent="saveBatch">
        <h3>Batch {{ editingBatch.batch_no }}</h3>
        <div class="form-grid">
          <label v-if="editingBatch.has_expiry" class="field">
            <span>Expiry date</span>
            <input v-model="editingBatch.expiry_date" type="date" required />
          </label>
          <p v-else class="field field__hint">This batch has no shelf life.</p>
          <label class="field">
            <span>MRP per pack (₹)</span>
            <input v-model="editingBatch.mrp_per_pack" type="number" step="0.01" min="0.01" required />
          </label>
          <label v-if="auth.isAdmin" class="field">
            <span>Cost per pack (₹)</span>
            <input v-model="editingBatch.cost_per_pack" type="number" step="0.01" min="0" />
          </label>
          <label class="field">
            <span>GST %</span>
            <select v-model="editingBatch.gst_rate">
              <option value="0">0</option>
              <option value="5">5</option>
              <option value="12">12</option>
              <option value="18">18</option>
              <option value="28">28</option>
            </select>
          </label>
          <label class="field">
            <span>Default discount %</span>
            <input v-model="editingBatch.default_discount_pct" type="number" step="0.01" min="0" max="100" />
          </label>
          <label class="field">
            <span>Max discount %</span>
            <input v-model="editingBatch.max_discount_pct" type="number" step="0.01" min="0" max="100" />
          </label>
        </div>
        <div class="form-actions">
          <button class="btn btn--primary" type="submit" :disabled="savingBatch">
            {{ savingBatch ? 'Saving…' : 'Save batch' }}
          </button>
          <button class="btn" type="button" @click="editingBatch = null">Cancel</button>
        </div>
      </form>

      <div v-if="batches.length" class="table-wrap">
        <table class="data">
          <thead>
            <tr>
              <th>Batch</th>
              <th>Expiry</th>
              <th class="numeric">Available</th>
              <th class="numeric">MRP</th>
              <th class="numeric">Per unit</th>
              <th v-if="auth.isAdmin" class="numeric">Cost</th>
              <th v-if="auth.isAdmin" class="numeric">Margin</th>
              <th class="numeric">GST</th>
              <th class="numeric">Max disc.</th>
              <th>Status</th>
              <th v-if="auth.isAdmin"></th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="batch in batches" :key="batch.id">
              <td>{{ batch.batch_no }}</td>
              <td>{{ batch.has_expiry === false ? 'Does not expire' : batch.expiry_date }}</td>
              <td class="numeric">{{ trim(batch.qty_available_units) }}</td>
              <td class="numeric">{{ money(batch.mrp_per_pack) }}</td>
              <td class="numeric">{{ money(batch.unit_mrp) }}</td>
              <td v-if="auth.isAdmin" class="numeric">{{ money(batch.cost_per_pack) }}</td>
              <td v-if="auth.isAdmin" class="numeric">
                {{ batch.margin_pct ? `${batch.margin_pct}%` : '—' }}
              </td>
              <td class="numeric">{{ batch.gst_rate }}%</td>
              <td class="numeric">{{ batch.effective_max_discount_pct }}%</td>
              <td>
                <span v-if="batch.is_expired" class="badge badge--danger">Expired</span>
                <span v-else-if="batch.is_blocked" class="badge badge--danger">Blocked</span>
                <span
                  v-else-if="batch.days_to_expiry !== null && batch.days_to_expiry <= 90"
                  class="badge badge--warn"
                >
                  {{ batch.days_to_expiry }} days
                </span>
                <span v-else class="badge badge--ok">OK</span>
              </td>
              <td v-if="auth.isAdmin" class="numeric actions">
                <button class="btn btn--sm" @click="startEditBatch(batch)">Edit</button>
                <button class="btn btn--sm" @click="adjustQuantity(batch)">Adjust qty</button>
                <button class="btn btn--sm" @click="startReturn(batch)">Return to supplier</button>
                <button class="btn btn--sm" @click="toggleBlock(batch)">
                  {{ batch.is_blocked ? 'Unblock' : 'Block' }}
                </button>
                <button class="btn btn--sm btn--danger" @click="removeBatch(batch)">Delete</button>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>
  </section>

    <!-- Goods back to the distributor. Deliberately not the same control as
         "Adjust qty": an adjustment writes the value off, this recovers it. -->
    <ModalDialog v-if="returnFor" title="Return to supplier" @close="closeReturn">
      <form class="stack" @submit.prevent="submitReturn">
        <p class="muted small">
          {{ returnFor.medicine_name }} — batch {{ returnFor.batch_no }},
          {{ trim(returnFor.qty_available_units) }} unit(s) on the shelf.
        </p>
        <p class="muted small">
          Raises a debit note: the stock leaves, the input tax claimed on it is
          reversed, and the value becomes credit you can set against a later
          invoice from this supplier.
        </p>

        <div class="form-grid">
          <label class="field">
            <span>Supplier</span>
            <select v-model="returnForm.supplier_id" required>
              <option value="" disabled>Choose a supplier</option>
              <option v-for="s in returnSuppliers" :key="s.id" :value="s.id">{{ s.name }}</option>
            </select>
            <p class="field__hint">Whoever is taking the goods back and issuing the credit.</p>
          </label>

          <label class="field">
            <span>Units going back</span>
            <input v-model="returnForm.qty_units" inputmode="decimal" required />
            <p class="field__hint">
              In {{ SELLABLE_UNIT_LABEL[returnFor.pack_unit] || 'units' }}, not packs.
            </p>
          </label>

          <label class="field">
            <span>Their credit note no.</span>
            <input v-model="returnForm.supplier_credit_note_ref" />
            <p class="field__hint">Optional — fill it in when the distributor sends theirs.</p>
          </label>
        </div>

        <label class="field">
          <span>Reason</span>
          <input v-model="returnForm.reason" required placeholder="Short-dated, distributor agreed to take it back" />
          <p class="field__hint">Printed on the debit note, so write it for the distributor.</p>
        </label>

        <p v-if="returnError" class="alert alert--error">{{ returnError }}</p>

        <div class="form-actions">
          <button class="btn btn--primary" type="submit" :disabled="returnSaving">
            {{ returnSaving ? 'Raising…' : 'Raise debit note' }}
          </button>
          <button class="btn" type="button" @click="closeReturn">Cancel</button>
        </div>
      </form>
    </ModalDialog>
</template>

<style scoped>
.grow {
  flex: 1;
  min-width: 10rem;
}

.inline {
  display: inline-flex;
  align-items: center;
  gap: 0.4rem;
  font-size: 0.9rem;
  white-space: nowrap;
}

.matches {
  list-style: none;
  margin: 0.4rem 0 0;
  padding: 0;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  max-height: 240px;
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
  padding: 0.5rem 0.8rem;
  cursor: pointer;
}

.matches button:hover {
  background: var(--accent-soft);
}

.chosen {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 0.75rem;
  padding: 0.55rem 0.8rem;
  border: 1px solid var(--accent);
  background: var(--accent-soft);
  border-radius: var(--radius);
}

.chosen p {
  margin: 0.15rem 0 0;
}

/* The reason sits under the quantity it explains, across the whole grid. */
.field--wide {
  grid-column: 1 / -1;
}

.field__hint--warn {
  color: var(--warn);
  font-weight: 600;
}

.field__hint--margin {
  font-size: 0.74rem;
}

.field__label {
  display: block;
  font-size: 0.78rem;
  font-weight: 600;
  color: var(--text-muted);
  margin-bottom: 0.15rem;
}

.chosen-invoice {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: 0.5rem;
  margin: 0.4rem 0 0;
  padding: 0.45rem 0.7rem;
  border: 1px solid var(--accent);
  background: var(--accent-soft);
  border-radius: var(--radius);
}

.composition {
  margin: 0.1rem 0 0;
  font-size: 0.82rem;
  color: var(--text-muted);
  white-space: normal;
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

.actions {
  white-space: nowrap;
}

.actions button + button {
  margin-left: 0.35rem;
}
</style>
