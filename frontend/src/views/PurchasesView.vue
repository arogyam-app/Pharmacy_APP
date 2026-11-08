<script setup>
/**
 * Purchases — goods inward.
 *
 * Cost here is GST-*exclusive*, the way a distributor bills, unlike retail
 * MRP which is GST-inclusive. The totals shown while typing are a preview
 * computed the same way the server does; the server recomputes everything
 * on save and its figures are the ones stored.
 */
import { computed, onMounted, reactive, ref, watch } from 'vue'

import { matchMedicines } from '@/api/catalog'
import {
  addPurchaseItems,
  cancelPurchase,
  createPurchase,
  downloadPurchaseTemplate,
  previewPurchaseImport,
  fetchPurchase,
  listPurchases,
  listSuppliers,
  updatePurchase,
} from '@/api/purchases'
import { useDebounced } from '@/composables/useDebounced'
import { EXPIRY_BASIS_LABEL, ExpiryBasis, ItemType } from '@/constants'
import DmyDateInput from '@/components/DmyDateInput.vue'
import QuickAddProductDialog from '@/components/QuickAddProductDialog.vue'
import { useAuthStore } from '@/stores/auth'

const auth = useAuthStore()

// Goods inward can be delegated: an executive keys in what the
// distributor delivered. What it cost stays with the owner, so the
// server omits those fields and these columns come out with them
// (CLAUDE.md rule 4).
const showCost = computed(() => auth.isAdmin)

const rupees = new Intl.NumberFormat('en-IN', {
  style: 'currency',
  currency: 'INR',
  minimumFractionDigits: 2,
})
const money = (v) => (v === null || v === undefined || v === '' ? '—' : rupees.format(Number(v)))
const trim = (v) => Number(v).toLocaleString('en-IN', { maximumFractionDigits: 3 })
const today = () => new Date().toISOString().slice(0, 10)

/**
 * "2026-08-14" -> "14/08/2026". Plain string surgery, not `Date` +
 * `toLocaleDateString`: these are business dates (no time component), and
 * routing one through `Date` risks the browser's local timezone reading
 * UTC midnight as the *previous* day — an off-by-one this app cannot
 * afford on an invoice date. See InvoiceView.vue's billDate for the same
 * dd/mm/yyyy formatting where a real timestamp (with a timezone to
 * convert from) makes `Date` the right tool instead.
 */
const dmy = (v) => {
  if (!v) return '—'
  const [y, m, d] = v.slice(0, 10).split('-')
  return `${d}/${m}/${y}`
}

// --- suppliers ------------------------------------------------------------
const suppliers = ref([])

async function loadSuppliers() {
  const data = await listSuppliers({ is_active: true, page_size: 200 })
  suppliers.value = data.items
}

const chosenSupplier = computed(() =>
  suppliers.value.find((s) => String(s.id) === String(header.supplier_id)),
)

/** Which tax the invoice will carry — the operator should see this up front. */
const taxMode = computed(() => {
  // Adding to a document already received: it settled its own split when it
  // arrived, and a later line inherits it. Re-deriving from the store's
  // state would preview CGST + SGST on an inter-state invoice.
  if (addingTo.value) {
    return addingTo.value.is_intra_state
      ? { intra: true, label: 'CGST + SGST (as on this invoice)' }
      : { intra: false, label: 'IGST (as on this invoice)' }
  }
  const supplierCode = chosenSupplier.value?.state_code
  const storeCode = auth.store?.state_code
  if (!supplierCode || !storeCode) return { intra: true, label: 'CGST + SGST (assumed same state)' }
  const intra = supplierCode === storeCode
  return {
    intra,
    label: intra ? `CGST + SGST (both in state ${storeCode})` : `IGST (supplier in ${supplierCode})`,
  }
})

// --- the invoice being entered -------------------------------------------
const blankHeader = () => ({
  supplier_id: '',
  supplier_invoice_no: '',
  supplier_invoice_date: today(),
  received_date: today(),
  other_charges: '',
  payment_status: 'UNPAID',
  notes: '',
})

const blankLine = () => ({
  medicine: null,
  // Its own search box: what has been typed, what came back, and whether a
  // search has actually run — the last one is what tells "nothing matches"
  // apart from "nothing typed yet".
  query: '',
  matches: [],
  searching: false,
  searched: false,
  // What the matcher noticed about the text: the size it read off it, and
  // whether any candidate actually is that pack. A brand that exists in
  // another size is not this product.
  packHint: null,
  hasPackMatch: false,
  batch_no: '',
  // Whatever the carton states — a printed expiry, a manufacturing date with
  // a best-before period, or nothing at all for goods that never expire.
  expiry_basis: ExpiryBasis.EXPIRY_DATE,
  expiry_date: '',
  mfg_date: '',
  best_before_months: '',
  qty_packs: '',
  free_qty_packs: '',
  // Problems the importer found with this row, if it came from a file.
  importErrors: [],
  cost_per_pack: '',
  mrp_per_pack: '',
  discount_pct: '',
  gst_rate: '12',
  default_discount_pct: '',
  max_discount_pct: '',
})

const header = reactive(blankHeader())
const lines = ref([blankLine()])
const saving = ref(false)
const formError = ref(null)
const formNotice = ref(null)
const formWarnings = ref([])

function addLine() {
  lines.value.push(blankLine())
}

function removeLine(index) {
  lines.value.splice(index, 1)
  if (!lines.value.length) lines.value.push(blankLine())
}

function resetForm() {
  Object.assign(header, blankHeader())
  lines.value = [blankLine()]
}

// --- adding to an invoice already received --------------------------------
//
// Non-null while the form is appending rather than creating. The header then
// describes a document that already exists, so it is shown read-only: its
// supplier, number and date are the supplier's and are not being restated.
const addingTo = ref(null)

function startAddingTo(invoice) {
  addingTo.value = invoice
  resetForm()
  formError.value = null
  formNotice.value = null
  formWarnings.value = []
  window.scrollTo({ top: 0, behavior: 'smooth' })
}

function stopAddingTo() {
  addingTo.value = null
}

// --- receiving the item list as a file ------------------------------------
//
// A distributor sends the lines as a spreadsheet. The file carries **items
// only**: the supplier, the invoice number and its date identify the
// document and are keyed on screen, so a file cannot name a supplier nobody
// chose.
//
// The upload never receives anything. Rows come back parsed, each with the
// products its text might mean, and they land in the ordinary item rows for
// a person to resolve — which is the same form, and the same confirm, as a
// list typed by hand.
const importing = ref(false)
const importError = ref(null)
const importSummary = ref(null)

async function onFileChosen(event) {
  const file = event.target.files?.[0]
  event.target.value = '' // so choosing the same file twice still fires
  if (!file) return

  importing.value = true
  importError.value = null
  importSummary.value = null
  formNotice.value = null
  try {
    const preview = await previewPurchaseImport(file)
    if (preview.missing_columns.length) {
      importError.value =
        `The file is missing ${preview.missing_columns.join(', ')}. ` +
        "Download the template and paste the supplier's lines into it."
      return
    }
    if (!preview.total_rows) {
      importError.value = 'No rows in that file.'
      return
    }

    lines.value = preview.lines.map(fromImported)
    importSummary.value = {
      filename: preview.filename,
      total: preview.total_rows,
      ready: preview.ready_rows,
      unresolved: preview.total_rows - preview.ready_rows,
    }
  } catch (err) {
    importError.value = err.message
  } finally {
    importing.value = false
  }
}

/** One imported row as an item row, with its candidates already attached. */
function fromImported(imported) {
  const line = blankLine()
  Object.assign(line, {
    query: imported.product_text,
    matches: imported.matches,
    // Shown as a resolved search: the candidates are there to be picked
    // from, and nothing is chosen for the operator.
    searched: true,
    packHint: imported.pack_hint,
    hasPackMatch: imported.has_pack_match,
    importErrors: imported.errors,
    batch_no: imported.batch_no,
    expiry_basis: imported.expiry_basis,
    expiry_date: imported.expiry_date || '',
    mfg_date: imported.mfg_date || '',
    best_before_months: imported.best_before_months ?? '',
    qty_packs: imported.qty_packs ?? '',
    free_qty_packs: imported.free_qty_packs ?? '',
    mrp_per_pack: imported.mrp_per_pack ?? '',
    cost_per_pack: imported.cost_per_pack ?? '',
    discount_pct: imported.discount_pct ?? '',
    gst_rate: String(Number(imported.gst_rate ?? 12)),
    default_discount_pct: imported.default_discount_pct ?? '',
    max_discount_pct: imported.max_discount_pct ?? '',
  })
  return line
}

/** Rows still needing a product chosen — the form refuses while any remain. */
const unresolvedLines = computed(() =>
  lines.value.filter((l) => !l.medicine && (l.query || l.batch_no)).length,
)

// --- finding the product, one search box per item ------------------------
//
// Every item row carries its own box and searches as you type — no button to
// press first, and no single shared picker that had to be opened against one
// row at a time. The search covers medicines *and* general goods, because one
// distributor invoice regularly carries both.
//
// Each line keeps its own `query`, `matches` and `searching`, so two rows can
// be half-typed at once without stealing each other's results.

/**
 * Debouncing per row.
 *
 * Keyed by the row itself rather than its position: removing a row shifts
 * every index below it, and an index-keyed timer would then cancel — or
 * write its results into — the wrong row.
 */
const searchTimers = new WeakMap()

function onSearch(index) {
  const line = lines.value[index]
  line.medicine = null
  clearTimeout(searchTimers.get(line))
  const term = line.query.trim()
  if (term.length < 2) {
    line.matches = []
    line.searched = false
    return
  }
  searchTimers.set(
    line,
    setTimeout(async () => {
      line.searching = true
      try {
        // No item_type filter: an invoice may carry both kinds. The matcher
        // reads the supplier's wording — a trailing "TAB", a pack size —
        // and returns candidates without choosing between them.
        const result = await matchMedicines(term)
        line.matches = result.matches
        line.packHint = result.pack_hint
        line.hasPackMatch = result.has_pack_match
      } catch {
        line.matches = []
        line.packHint = null
        line.hasPackMatch = false
      } finally {
        line.searching = false
        line.searched = true
      }
    }, 250),
  )
}

function chooseMedicine(index, medicine) {
  const line = lines.value[index]
  line.medicine = medicine
  line.query = medicine.brand_name
  line.matches = []
  line.searched = false
  line.packHint = null
  line.hasPackMatch = false
  // A repeat order usually keeps the same terms as the stock already held.
  if (medicine.mrp_per_pack) line.mrp_per_pack = medicine.mrp_per_pack
  if (medicine.gst_rate) line.gst_rate = String(Number(medicine.gst_rate))
}

function clearChoice(index) {
  const line = lines.value[index]
  line.medicine = null
  line.query = ''
  line.matches = []
  line.searched = false
  line.packHint = null
  line.hasPackMatch = false
}

// --- putting a missing product on the master, without losing the invoice --
const quickAdd = ref(null) // { index, itemType, name }

function openQuickAdd(index, itemType) {
  const line = lines.value[index]
  const hint = line.packHint
  // The name without the size the matcher read off it: adding "Volini Gel
  // 30GM" as a brand called "VOLINI GEL 30GM" would put the pack in the
  // name, where it cannot be compared against anything.
  const name = hint ? line.query.replace(hint.text, '').trim() : line.query.trim()
  quickAdd.value = { index, itemType, name, pack: hint }
}

function onQuickAdded(created) {
  const { index } = quickAdd.value
  quickAdd.value = null
  // Shape it like a search result so the line can use it straight away.
  chooseMedicine(index, {
    id: created.id,
    brand_name: created.brand_name,
    composition_text: created.composition_text,
    manufacturer_name: created.manufacturer_name,
    pack_label: created.pack_label,
    units_per_pack: created.units_per_pack,
    item_type: created.item_type,
    mrp_per_pack: created.list_mrp_per_pack,
    gst_rate: null,
  })
}

// --- live totals ----------------------------------------------------------
const round2 = (n) => Math.round((n + Number.EPSILON) * 100) / 100

function lineTax(line) {
  const gross = round2(Number(line.qty_packs || 0) * Number(line.cost_per_pack || 0))
  const discount = round2((gross * Number(line.discount_pct || 0)) / 100)
  const taxable = round2(gross - discount)
  const gst = round2((taxable * Number(line.gst_rate || 0)) / 100)
  return { gross, discount, taxable, gst, total: round2(taxable + gst) }
}

const totals = computed(() => {
  let taxable = 0
  let gst = 0
  for (const line of lines.value) {
    if (!line.medicine || !line.qty_packs) continue
    const t = lineTax(line)
    taxable += t.taxable
    gst += t.gst
  }
  const payable = round2(taxable + gst + Number(header.other_charges || 0))
  const rounded = Math.round(payable)
  return {
    taxable: round2(taxable),
    gst: round2(gst),
    cgst: taxMode.value.intra ? round2(gst / 2) : 0,
    igst: taxMode.value.intra ? 0 : round2(gst),
    roundOff: round2(rounded - payable),
    total: rounded,
  }
})

const unitsPreview = (line) => {
  if (!line.medicine || !line.qty_packs) return null
  const packs = Number(line.qty_packs) + Number(line.free_qty_packs || 0)
  return `${trim(packs * (line.medicine.units_per_pack || 1))} sellable unit(s)`
}

/** Free goods make the real cost lower than the invoice rate. */
const landedPreview = (line) => {
  const packs = Number(line.qty_packs || 0) + Number(line.free_qty_packs || 0)
  if (!packs || !line.cost_per_pack) return null
  const t = lineTax(line)
  return `${rupees.format(t.taxable / packs)} landed per pack`
}

/**
 * What the pack actually earns, and how far the price can be cut.
 *
 * MRP is GST-inclusive and the distributor's rate is not, so comparing them
 * directly overstates the margin by the tax. The tax carved out of MRP is
 * what the shop hands to the government, so the figure worth seeing is
 * (MRP net of GST − landed cost), and the headroom is that margin as a
 * percentage **of MRP** — which is the number a discount is quoted against.
 * A 20 % margin cannot absorb a 25 % discount.
 */
const marginPreview = (line) => {
  const packs = Number(line.qty_packs || 0) + Number(line.free_qty_packs || 0)
  const mrp = Number(line.mrp_per_pack || 0)
  if (!packs || !mrp || !line.cost_per_pack) return null

  const landed = lineTax(line).taxable / packs
  const gstRate = Number(line.gst_rate || 0)
  const mrpExGst = mrp / (1 + gstRate / 100)
  const margin = mrpExGst - landed
  if (margin <= 0) {
    return `Selling at a loss: landed ${rupees.format(landed)} against ${rupees.format(
      mrpExGst,
    )} net of GST.`
  }
  // Headroom is measured against MRP because that is what a discount is
  // taken off at the counter.
  const headroom = (margin / mrp) * 100
  return (
    `Margin ${rupees.format(margin)} per pack — ` +
    `${((margin / landed) * 100).toFixed(1)}% on cost. ` +
    `A discount above ${headroom.toFixed(1)}% sells at a loss.`
  )
}

function nullable(value) {
  const t = String(value ?? '').trim()
  return t === '' ? null : t
}

/** One line, in the shape the API takes. */
function linePayload(line) {
  return {
    medicine_id: line.medicine.id,
    batch_no: line.batch_no.trim(),
    expiry_basis: line.expiry_basis,
    expiry_date: line.expiry_basis === ExpiryBasis.EXPIRY_DATE ? line.expiry_date : null,
    mfg_date: line.expiry_basis === ExpiryBasis.BEST_BEFORE ? line.mfg_date : null,
    best_before_months:
      line.expiry_basis === ExpiryBasis.BEST_BEFORE ? Number(line.best_before_months) : null,
    qty_packs: String(line.qty_packs),
    free_qty_packs: String(line.free_qty_packs || '0'),
    cost_per_pack: String(line.cost_per_pack),
    mrp_per_pack: String(line.mrp_per_pack),
    discount_pct: String(line.discount_pct || '0'),
    gst_rate: line.gst_rate,
    default_discount_pct: nullable(line.default_discount_pct),
    max_discount_pct: nullable(line.max_discount_pct),
  }
}

async function submit() {
  formError.value = null
  formNotice.value = null
  formWarnings.value = []

  const usable = lines.value.filter((l) => l.medicine && l.qty_packs)
  if (!usable.length) {
    formError.value = 'Add at least one item with a product and a quantity.'
    return
  }
  if (addingTo.value === null && !header.supplier_id) {
    formError.value = 'Choose the supplier.'
    return
  }

  saving.value = true
  try {
    // Two ways in: a fresh invoice, or lines onto one already received.
    // Goods from one supplier invoice reach the shelf over more than one
    // sitting, so the second is not an edge case.
    const saved = addingTo.value
      ? await addPurchaseItems(addingTo.value.id, usable.map(linePayload))
      : await createPurchase({
          // A key is a UUID string; Number() would make it NaN.
          supplier_id: header.supplier_id,
          supplier_invoice_no: header.supplier_invoice_no.trim(),
          supplier_invoice_date: header.supplier_invoice_date,
          received_date: header.received_date || null,
          other_charges: String(header.other_charges || '0'),
          payment_status: header.payment_status,
          notes: nullable(header.notes),
          items: usable.map(linePayload),
        })

    formNotice.value = addingTo.value
      ? `${usable.length} item(s) added to ${saved.supplier_invoice_no} ` +
        `(${saved.doc_number}) — ${saved.items.length} item(s) on it now, ` +
        `${money(saved.total_amount)}.`
      : `Received as ${saved.doc_number} — ${saved.items.length} item(s), ` +
        `${money(saved.total_amount)}. Stock updated.`
    formWarnings.value = saved.warnings || []
    stopAddingTo()
    resetForm()
    await loadRegister()
  } catch (err) {
    formError.value = err.message
  } finally {
    saving.value = false
  }
}

// --- register -------------------------------------------------------------
const rows = ref([])
const total = ref(0)
const loading = ref(true)
const registerError = ref(null)
const registerNotice = ref(null)

const filters = reactive({ q: '', supplier_id: '', date_from: '', date_to: '' })
const debouncedQ = useDebounced(computed(() => filters.q), 250)

async function loadRegister() {
  loading.value = true
  registerError.value = null
  try {
    const data = await listPurchases({
      q: filters.q.trim() || undefined,
      supplier_id: filters.supplier_id || undefined,
      date_from: filters.date_from || undefined,
      date_to: filters.date_to || undefined,
      page_size: 50,
    })
    rows.value = data.items
    total.value = data.total
  } catch (err) {
    registerError.value = err.message
  } finally {
    loading.value = false
  }
}

watch([debouncedQ, () => filters.supplier_id, () => filters.date_from, () => filters.date_to],
  loadRegister)

// --- one invoice ----------------------------------------------------------
const open = ref(null)

async function showPurchase(row) {
  registerError.value = null
  try {
    open.value = await fetchPurchase(row.id)
  } catch (err) {
    registerError.value = err.message
  }
}

async function setPaymentStatus(status) {
  try {
    open.value = await updatePurchase(open.value.id, { payment_status: status })
    registerNotice.value = `Marked ${open.value.doc_number} as ${status.toLowerCase()}.`
    await loadRegister()
  } catch (err) {
    registerError.value = err.message
  }
}

async function cancel() {
  const reason = prompt(
    `Cancel purchase ${open.value.doc_number}?\n\n` +
      'The stock it brought in is reversed through the ledger. The invoice and its ' +
      'figures stay on record. Only possible while nothing has been sold from it.\n\n' +
      'Reason:',
  )
  if (!reason || reason.trim().length < 3) return
  registerError.value = null
  try {
    open.value = await cancelPurchase(open.value.id, reason.trim())
    registerNotice.value = `${open.value.doc_number} cancelled and stock reversed.`
    await loadRegister()
  } catch (err) {
    registerError.value = err.message
  }
}

onMounted(async () => {
  await Promise.all([loadSuppliers(), loadRegister()])
})
</script>

<template>
  <section class="stack">
    <div class="card stack">
      <h1>Purchases</h1>
      <p class="muted">
        Receiving a supplier invoice creates the batches, writes the stock ledger and records the
        inward GST. Cost here is <strong>GST-exclusive</strong>, as a distributor bills it — unlike
        MRP, which is GST-inclusive. One invoice can carry medicines and general items together,
        exactly as the distributor billed them.
      </p>
    </div>

    <!-- ---------------------------------------------------------------- -->
    <div class="card stack">
      <div class="form-actions">
        <h2 class="grow">
          {{ addingTo ? `Add items to ${addingTo.supplier_invoice_no}` : 'Receive goods' }}
        </h2>
        <button v-if="addingTo" class="btn" type="button" @click="stopAddingTo">
          Cancel — receive a new invoice instead
        </button>
      </div>

      <!-- Appending to a document that already exists: its supplier, number
           and date are the supplier's and are not being restated here. -->
      <p v-if="addingTo" class="chosen-invoice">
        <strong>{{ addingTo.supplier_invoice_no }}</strong>
        <span class="muted small">
          {{ addingTo.supplier_name }} · {{ addingTo.doc_number }} ·
          received {{ dmy(addingTo.received_date) }} · {{ addingTo.item_count }} item(s) ·
          {{ money(addingTo.total_amount) }}
        </span>
      </p>

      <form class="stack" @submit.prevent="submit">
        <div v-if="!addingTo" class="form-grid">
          <label class="field">
            <span>Supplier</span>
            <select v-model="header.supplier_id" required>
              <option value="" disabled>Choose a supplier…</option>
              <option v-for="s in suppliers" :key="s.id" :value="s.id">{{ s.name }}</option>
            </select>
            <p v-if="chosenSupplier" class="field__hint">{{ taxMode.label }}</p>
            <p v-else class="field__hint">
              None yet? Add one under <RouterLink to="/masters/suppliers">Suppliers</RouterLink>.
            </p>
          </label>
          <label class="field">
            <span>Supplier invoice no.</span>
            <input v-model="header.supplier_invoice_no" required maxlength="50" />
            <p class="field__hint">Receiving the same number twice is refused.</p>
          </label>
          <label class="field">
            <span>Invoice date</span>
            <DmyDateInput v-model="header.supplier_invoice_date" required />
          </label>
          <label class="field">
            <span>Received on</span>
            <DmyDateInput v-model="header.received_date" />
          </label>
          <label class="field">
            <span>Other charges (₹)</span>
            <input v-model="header.other_charges" type="number" step="0.01" min="0" />
            <p class="field__hint">Freight, handling — added after tax.</p>
          </label>
          <label class="field">
            <span>Payment</span>
            <select v-model="header.payment_status">
              <option value="UNPAID">Unpaid</option>
              <option value="PARTIAL">Partly paid</option>
              <option value="PAID">Paid</option>
            </select>
          </label>
        </div>

        <!-- the item list, typed or uploaded ------------------------- -->
        <div class="upload">
          <div class="upload__head">
            <strong>Have the supplier's list as a file?</strong>
            <span class="muted small">
              Items only — the supplier and invoice number above identify the document.
            </span>
          </div>
          <div class="form-actions">
            <label class="btn btn--sm btn--primary file">
              {{ importing ? 'Reading…' : 'Upload CSV or Excel' }}
              <input
                type="file"
                accept=".csv,.xlsx,.xls,text/csv"
                :disabled="importing"
                @change="onFileChosen"
              />
            </label>
            <button class="btn btn--sm" type="button" @click="downloadPurchaseTemplate('csv')">
              CSV template
            </button>
            <button class="btn btn--sm" type="button" @click="downloadPurchaseTemplate('xlsx')">
              Excel template
            </button>
          </div>
          <p v-if="importError" class="alert alert--error">{{ importError }}</p>
          <p v-else-if="importSummary" class="alert alert--ok">
            Read {{ importSummary.total }} row(s) from {{ importSummary.filename }}.
            <template v-if="importSummary.unresolved">
              {{ importSummary.unresolved }} still need a product chosen — nothing is
              received until every row names one.
            </template>
            <template v-else>
              Check each row below and confirm; the file is a claim about what arrived,
              not a receipt.
            </template>
          </p>
        </div>

        <h3>Items</h3>
        <div v-for="(line, index) in lines" :key="index" class="line stack">
          <div class="line__head">
            <!-- Always a live search box: type and the matches appear, with
                 no button to press first. It covers medicines and general
                 goods together, because one invoice carries both. -->
            <label class="field grow">
              <span>Item {{ index + 1 }}</span>
              <input
                v-model="line.query"
                type="search"
                :placeholder="`Search a medicine or general item…`"
                @input="onSearch(index)"
              />
            </label>
            <div class="line__actions">
              <button
                v-if="line.medicine"
                class="btn btn--sm"
                type="button"
                @click="clearChoice(index)"
              >
                Change
              </button>
              <button
                class="btn btn--sm btn--danger"
                type="button"
                :disabled="lines.length === 1 && !line.medicine && !line.query"
                @click="removeLine(index)"
              >
                Remove
              </button>
            </div>
          </div>

          <p v-if="line.importErrors?.length" class="alert alert--error small">
            Row from the file: {{ line.importErrors.join('; ') }}.
          </p>

          <p v-if="line.medicine" class="chosen">
            <strong>{{ line.medicine.brand_name }}</strong>
            <span
              v-if="line.medicine.item_type === ItemType.GENERAL"
              class="badge"
            >General</span>
            <span class="muted small">
              <template v-if="line.medicine.composition_text">
                {{ line.medicine.composition_text }} ·
              </template>
              {{ line.medicine.pack_label }} ·
              {{ line.medicine.units_per_pack }} unit(s) per pack
            </span>
          </p>

          <!-- Candidates, never a decision: a supplier's abbreviation is
               not evidence enough to bind money to a row, so the operator
               picks. A brand in the wrong size is offered too, marked as
               the different product it is. -->
          <template v-else-if="line.matches.length">
            <p
              v-if="line.packHint && !line.hasPackMatch"
              class="alert alert--warn small"
            >
              Nothing on the master is a <strong>{{ line.packHint.text }}</strong> pack.
              {{ line.packHint.text }} and the sizes below are different products — add it
              if that is what arrived.
            </p>
            <ul class="matches">
              <li v-for="m in line.matches" :key="m.medicine.id">
                <button type="button" @click="chooseMedicine(index, m.medicine)">
                  <strong>{{ m.medicine.brand_name }}</strong>
                  <span v-if="m.pack_matches && line.packHint" class="badge badge--ok">
                    {{ line.packHint.text }}
                  </span>
                  <span v-else-if="!m.pack_matches" class="badge badge--warn">
                    different pack
                  </span>
                  <span class="muted small">
                    <template v-if="m.medicine.composition_text">
                      {{ m.medicine.composition_text }} ·
                    </template>
                    <span v-else class="badge">General</span>
                    {{ m.medicine.manufacturer_name }} · {{ m.medicine.pack_label }}
                  </span>
                </button>
              </li>
            </ul>
            <div class="notfound">
              <span class="muted small">None of these?</span>
              <button
                class="btn btn--sm"
                type="button"
                @click="openQuickAdd(index, ItemType.MEDICINE)"
              >
                Add medicine
              </button>
              <button
                class="btn btn--sm"
                type="button"
                @click="openQuickAdd(index, ItemType.GENERAL)"
              >
                Add general item
              </button>
            </div>
          </template>

          <p v-else-if="line.searching" class="muted small">Searching…</p>

          <!-- Nothing on the master. Rather than sending the operator off to
               a catalogue screen and losing a half-keyed invoice, put it on
               the master right here. -->
          <div v-else-if="line.searched" class="notfound">
            <span class="muted small">Nothing matches “{{ line.query.trim() }}”.</span>
            <button
              class="btn btn--sm btn--primary"
              type="button"
              @click="openQuickAdd(index, ItemType.MEDICINE)"
            >
              Add medicine
            </button>
            <button
              class="btn btn--sm btn--primary"
              type="button"
              @click="openQuickAdd(index, ItemType.GENERAL)"
            >
              Add general item
            </button>
          </div>

          <div v-if="line.medicine" class="form-grid">
            <label class="field">
              <span>Batch no. <template v-if="line.medicine.item_type === ItemType.GENERAL">(optional)</template></span>
              <input
                v-model="line.batch_no"
                :required="line.medicine.item_type !== ItemType.GENERAL"
                maxlength="60"
              />
            </label>
            <label class="field">
              <span>Shelf life</span>
              <select v-model="line.expiry_basis">
                <option v-for="(label, value) in EXPIRY_BASIS_LABEL" :key="value" :value="value">
                  {{ label }}
                </option>
              </select>
            </label>
            <label v-if="line.expiry_basis === ExpiryBasis.EXPIRY_DATE" class="field">
              <span>Expiry</span>
              <DmyDateInput v-model="line.expiry_date" required />
            </label>
            <template v-else-if="line.expiry_basis === ExpiryBasis.BEST_BEFORE">
              <label class="field">
                <span>Manufactured on</span>
                <DmyDateInput v-model="line.mfg_date" required />
              </label>
              <label class="field">
                <span>Best before (months)</span>
                <input
                  v-model="line.best_before_months"
                  type="number"
                  step="1"
                  min="1"
                  max="600"
                  required
                />
              </label>
            </template>
            <label class="field">
              <span>Qty (packs)</span>
              <input v-model="line.qty_packs" type="number" step="0.001" min="0.001" required />
              <p v-if="unitsPreview(line)" class="field__hint">{{ unitsPreview(line) }}</p>
            </label>
            <label class="field">
              <span>Free packs</span>
              <input v-model="line.free_qty_packs" type="number" step="0.001" min="0" />
              <p class="field__hint">Scheme goods — stocked, not billed.</p>
            </label>
            <label class="field">
              <span>MRP per pack (₹)</span>
              <input v-model="line.mrp_per_pack" type="number" step="0.01" min="0.01" required />
              <p class="field__hint">Printed on the box, GST-inclusive.</p>
            </label>
            <label class="field">
              <span>Rate per pack (₹)</span>
              <input v-model="line.cost_per_pack" type="number" step="0.01" min="0" required />
              <p v-if="landedPreview(line)" class="field__hint">{{ landedPreview(line) }}</p>
            </label>
            <label class="field">
              <span>Trade discount %</span>
              <input v-model="line.discount_pct" type="number" step="0.01" min="0" max="100" />
            </label>
            <label class="field">
              <span>GST %</span>
              <select v-model="line.gst_rate">
                <option value="0">0</option>
                <option value="5">5</option>
                <option value="12">12</option>
                <option value="18">18</option>
                <option value="28">28</option>
              </select>
            </label>
            <label class="field">
              <span>Retail default disc. %</span>
              <input v-model="line.default_discount_pct" type="number" step="0.01" min="0" max="100" />
              <p class="field__hint">Blank uses the store policy.</p>
            </label>
            <label class="field">
              <span>Retail max disc. %</span>
              <input v-model="line.max_discount_pct" type="number" step="0.01" min="0" max="100" />
            </label>
          </div>

          <p v-if="line.medicine && line.qty_packs" class="line__total muted">
            Taxable {{ money(lineTax(line).taxable) }} · GST {{ money(lineTax(line).gst) }} ·
            <strong>{{ money(lineTax(line).total) }}</strong>
          </p>
          <p v-if="marginPreview(line)" class="line__margin">{{ marginPreview(line) }}</p>
        </div>

        <div class="form-actions">
          <button class="btn btn--sm" type="button" @click="addLine">+ Add item</button>
        </div>

        <!-- totals --------------------------------------------------- -->
        <div v-if="showCost" class="totals">
          <div><span>Taxable</span><strong>{{ money(totals.taxable) }}</strong></div>
          <div v-if="taxMode.intra">
            <span>CGST</span><strong>{{ money(totals.cgst) }}</strong>
          </div>
          <div v-if="taxMode.intra">
            <span>SGST</span><strong>{{ money(totals.gst - totals.cgst) }}</strong>
          </div>
          <div v-else><span>IGST</span><strong>{{ money(totals.igst) }}</strong></div>
          <div v-if="Number(header.other_charges)">
            <span>Other charges</span><strong>{{ money(header.other_charges) }}</strong>
          </div>
          <div><span>Round off</span><strong>{{ money(totals.roundOff) }}</strong></div>
          <div class="totals__grand">
            <span>Payable</span><strong>{{ money(totals.total) }}</strong>
          </div>
        </div>
        <p class="muted small">The server recomputes these; its figures are what get stored.</p>

        <p v-if="formError" class="alert alert--error">{{ formError }}</p>
        <p v-if="formNotice" class="alert alert--ok">{{ formNotice }}</p>
        <p v-for="(w, i) in formWarnings" :key="i" class="alert alert--warn">{{ w }}</p>

        <div class="form-actions">
          <button class="btn btn--primary" type="submit" :disabled="saving">
            {{ saving ? 'Posting…' : 'Receive into stock' }}
          </button>
          <button class="btn" type="button" @click="resetForm">Clear</button>
        </div>
      </form>
    </div>

    <!-- ---------------------------------------------------------------- -->
    <div class="card stack">
      <h2>Purchase register</h2>
      <div class="form-actions">
        <input v-model="filters.q" class="grow" type="search" placeholder="Invoice number…" />
        <select v-model="filters.supplier_id">
          <option value="">All suppliers</option>
          <option v-for="s in suppliers" :key="s.id" :value="s.id">{{ s.name }}</option>
        </select>
        <DmyDateInput v-model="filters.date_from" title="Received from" />
        <DmyDateInput v-model="filters.date_to" title="Received to" />
      </div>

      <p v-if="registerError" class="alert alert--error">{{ registerError }}</p>
      <p v-if="registerNotice" class="alert alert--ok">{{ registerNotice }}</p>
      <p v-if="loading" class="muted">Loading…</p>
      <p v-else-if="!rows.length" class="muted">No purchases recorded yet.</p>

      <div v-else class="table-wrap">
        <table class="data">
          <thead>
            <tr>
              <th>Doc no.</th>
              <th>Supplier</th>
              <th>Their invoice</th>
              <th>Received</th>
              <th class="numeric">Items</th>
              <th v-if="showCost" class="numeric">Taxable</th>
              <th v-if="showCost" class="numeric">Total</th>
              <th>Payment</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            <tr
              v-for="row in rows"
              :key="row.id"
              :class="{ 'row--selected': open?.id === row.id, cancelled: row.is_cancelled }"
              @click="showPurchase(row)"
            >
              <td>{{ row.doc_number }}</td>
              <td>{{ row.supplier_name }}</td>
              <td>
                {{ row.supplier_invoice_no }}
                <small class="muted"><br />{{ dmy(row.supplier_invoice_date) }}</small>
              </td>
              <td>{{ dmy(row.received_date) }}</td>
              <td class="numeric">{{ row.item_count }}</td>
              <td v-if="showCost" class="numeric">{{ money(row.subtotal_taxable) }}</td>
              <td v-if="showCost" class="numeric">{{ money(row.total_amount) }}</td>
              <td>
                <span v-if="row.is_cancelled" class="badge badge--danger">Cancelled</span>
                <span v-else :class="['badge', row.payment_status === 'PAID' ? 'badge--ok' : 'badge--warn']">
                  {{ row.payment_status }}
                </span>
              </td>
              <td class="numeric">
                <!-- Straight from the register, without opening the invoice
                     first: the common case is a carton found the next
                     morning. @click.stop so the row does not also open. -->
                <button
                  v-if="!row.is_cancelled && auth.can('purchases:receive')"
                  class="btn btn--sm"
                  @click.stop="startAddingTo(row)"
                >
                  Add items
                </button>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
      <p v-if="total > rows.length" class="muted small">
        Showing {{ rows.length }} of {{ total }} — narrow the filters to see the rest.
      </p>
    </div>

    <!-- ---------------------------------------------------------------- -->
    <div v-if="open" class="card stack">
      <h2>{{ open.doc_number }} — {{ open.supplier_name }}</h2>
      <p class="muted">
        Invoice {{ open.supplier_invoice_no }} dated {{ dmy(open.supplier_invoice_date) }} · received
        {{ dmy(open.received_date) }} · {{ open.is_intra_state ? 'CGST + SGST' : 'IGST' }}
      </p>

      <p v-if="open.is_cancelled" class="alert alert--warn">
        Cancelled{{ open.cancelled_at ? ` on ${open.cancelled_at.slice(0, 10)}` : '' }} —
        {{ open.cancel_reason }}. The stock it brought in has been reversed.
      </p>

      <div class="table-wrap">
        <table class="data">
          <thead>
            <tr>
              <th>Medicine</th>
              <th>Batch</th>
              <th>Expiry</th>
              <th class="numeric">Packs</th>
              <th class="numeric">Free</th>
              <th class="numeric">Units</th>
              <th v-if="showCost" class="numeric">Rate</th>
              <th class="numeric">MRP</th>
              <th v-if="showCost" class="numeric">Disc.</th>
              <th class="numeric">GST</th>
              <th v-if="showCost" class="numeric">Taxable</th>
              <th v-if="showCost" class="numeric">Line total</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="item in open.items" :key="item.id">
              <td>{{ item.medicine_name }}</td>
              <td>{{ item.batch_no }}</td>
              <td>{{ item.has_expiry === false ? '—' : dmy(item.expiry_date) }}</td>
              <td class="numeric">{{ trim(item.qty_packs) }}</td>
              <td class="numeric">{{ trim(item.free_qty_packs) }}</td>
              <td class="numeric">{{ trim(item.qty_units) }}</td>
              <td v-if="showCost" class="numeric">{{ money(item.cost_per_pack) }}</td>
              <td class="numeric">{{ money(item.mrp_per_pack) }}</td>
              <td v-if="showCost" class="numeric">{{ item.discount_pct }}%</td>
              <td class="numeric">{{ item.gst_rate }}%</td>
              <td v-if="showCost" class="numeric">{{ money(item.taxable_value) }}</td>
              <td v-if="showCost" class="numeric">{{ money(item.line_total) }}</td>
            </tr>
          </tbody>
        </table>
      </div>

      <div class="totals">
        <div><span>Taxable</span><strong>{{ money(open.subtotal_taxable) }}</strong></div>
        <div v-if="open.is_intra_state">
          <span>CGST</span><strong>{{ money(open.cgst_amount) }}</strong>
        </div>
        <div v-if="open.is_intra_state">
          <span>SGST</span><strong>{{ money(open.sgst_amount) }}</strong>
        </div>
        <div v-else><span>IGST</span><strong>{{ money(open.igst_amount) }}</strong></div>
        <div v-if="Number(open.other_charges)">
          <span>Other charges</span><strong>{{ money(open.other_charges) }}</strong>
        </div>
        <div><span>Round off</span><strong>{{ money(open.round_off) }}</strong></div>
        <div class="totals__grand">
          <span>Total</span><strong>{{ money(open.total_amount) }}</strong>
        </div>
      </div>

      <div v-if="!open.is_cancelled" class="form-actions">
        <!-- Goods from one invoice reach the shelf over more than one
             sitting, and a medicine and a general item are keyed from
             different screens onto the same document. -->
        <button
          v-if="auth.can('purchases:receive')"
          class="btn btn--sm btn--primary"
          @click="startAddingTo(open)"
        >
          Add items
        </button>
        <button
          v-for="status in ['UNPAID', 'PARTIAL', 'PAID']"
          :key="status"
          class="btn btn--sm"
          :disabled="open.payment_status === status"
          @click="setPaymentStatus(status)"
        >
          Mark {{ status.toLowerCase() }}
        </button>
        <button class="btn btn--sm btn--danger" @click="cancel">Cancel purchase</button>
      </div>
    </div>
    <!-- Putting a missing product on the master without abandoning the
         invoice being keyed. -->
    <QuickAddProductDialog
      v-if="quickAdd"
      :item-type="quickAdd.itemType"
      :initial-name="quickAdd.name"
      :initial-pack="quickAdd.pack"
      @close="quickAdd = null"
      @created="onQuickAdded"
    />
  </section>
</template>

<style scoped>
.grow {
  flex: 1;
  min-width: 10rem;
}

.line {
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 0.85rem;
}

.line__head {
  display: flex;
  justify-content: space-between;
  align-items: flex-end;
  gap: 0.75rem;
}

.line__head .grow {
  flex: 1;
  min-width: 12rem;
}

/* The chosen product, and the offer to create one that does not exist. */
.chosen {
  margin: 0;
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: 0.4rem;
}

.notfound {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 0.5rem;
  padding: 0.5rem 0.75rem;
  border: 1px dashed var(--border-strong);
  border-radius: var(--radius);
  background: var(--surface-sunken);
}

.line__head p {
  margin: 0.15rem 0 0;
}

.line__actions {
  white-space: nowrap;
}

.line__actions button + button {
  margin-left: 0.35rem;
}

.line__total {
  margin: 0;
  text-align: right;
  font-size: 0.88rem;
}

/* Fine print, but the figure the retail discount is set from. */
.line__margin {
  margin: 0.15rem 0 0;
  text-align: right;
  font-size: 0.78rem;
  color: var(--text-muted);
}

.upload {
  border: 1px dashed var(--border-strong);
  border-radius: var(--radius);
  background: var(--surface-sunken);
  padding: var(--sp-3);
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
}

.upload__head {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: 0.5rem;
}

/* The real input is hidden behind the label, which is the only way to make
   a file picker look like the buttons beside it. */
.file {
  position: relative;
  overflow: hidden;
  cursor: pointer;
}

.file input[type='file'] {
  position: absolute;
  inset: 0;
  opacity: 0;
  cursor: pointer;
}

.chosen-invoice {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: 0.5rem;
  margin: 0;
  padding: 0.45rem 0.7rem;
  border: 1px solid var(--accent);
  background: var(--accent-soft);
  border-radius: var(--radius);
}

.picker input[type='search'] {
  width: 100%;
  font: inherit;
  padding: 0.45rem 0.75rem;
  border: 1px solid var(--border);
  border-radius: var(--radius);
}

.matches {
  list-style: none;
  margin: 0.4rem 0 0;
  padding: 0;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  max-height: 220px;
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

.totals {
  margin-left: auto;
  min-width: 18rem;
  font-size: 0.92rem;
}

.totals > div {
  display: flex;
  justify-content: space-between;
  padding: 0.2rem 0;
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

.cancelled td {
  opacity: 0.55;
  text-decoration: line-through;
}
</style>
