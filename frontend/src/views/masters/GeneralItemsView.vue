<script setup>
/**
 * General items — everything the shop sells that is not a medicine: soap,
 * nappies, a thermometer, a hot water bag.
 *
 * They get their own screen because nothing on a medicine form applies to
 * them: no salt, no schedule, no dosage form, and often no expiry date at
 * all. Sharing one form meant half of it greyed out.
 *
 * They do **not** get their own table. A customer buys shampoo and a strip of
 * paracetamol together and expects one invoice, so the catalogue, the batch,
 * the stock ledger and the sale line are the same machinery either way —
 * `item_type` is the only thing that differs. Splitting storage would mean
 * merging two carts at the till.
 *
 * The screen carries the whole life of a general item: the master record,
 * what is on the shelf, and the stock coming in.
 */
import { computed, onMounted, reactive, ref, watch } from 'vue'

import {
  createMedicine,
  deleteMedicine,
  downloadMedicineTemplate,
  importMedicines,
  listMedicines,
  updateMedicine,
} from '@/api/catalog'
import { adjustBatch, listBatches, updateBatch } from '@/api/inventory'
import ManufacturerPicker from '@/components/ManufacturerPicker.vue'
import ModalDialog from '@/components/ModalDialog.vue'
import { useDebounced } from '@/composables/useDebounced'
import {
  EXPIRY_BASIS_LABEL,
  ExpiryBasis,
  ItemType,
  PACK_SIZE_LABEL,
  PACK_UNITS_BY_FORM,
  SELLABLE_UNIT_LABEL,
} from '@/constants'
import { useAuthStore } from '@/stores/auth'

const auth = useAuthStore()

const rupees = new Intl.NumberFormat('en-IN', {
  style: 'currency',
  currency: 'INR',
  minimumFractionDigits: 2,
})
const money = (v) => (v === null || v === undefined ? '—' : rupees.format(Number(v)))
const trim = (v) => Number(v).toLocaleString('en-IN', { maximumFractionDigits: 3 })

const nullable = (value) => {
  const t = String(value ?? '').trim()
  return t === '' ? null : t
}

const PACK_UNITS = PACK_UNITS_BY_FORM.GENERAL

/** Below the item's own reorder level, where one has been set. */
const isLow = (row) =>
  row.reorder_level_units !== null &&
  row.reorder_level_units !== undefined &&
  Number(row.available_units) <= Number(row.reorder_level_units)

// --- what is on the shelf -------------------------------------------------
const rows = ref([])
const loading = ref(true)
const error = ref(null)
const notice = ref(null)
const query = ref('')
const debouncedQuery = useDebounced(query, 250)

async function load() {
  loading.value = true
  error.value = null
  try {
    // The master list, not the stock view: it carries the pack, HSN and
    // reorder level the edit form needs *and* the live stock figures, so one
    // call feeds both the table and the form.
    const data = await listMedicines({
      q: query.value.trim() || undefined,
      item_type: ItemType.GENERAL,
      is_active: true,
      page_size: 200,
    })
    rows.value = data.items
  } catch (err) {
    error.value = err.message
  } finally {
    loading.value = false
  }
}

watch(debouncedQuery, load)

// --- the master record ----------------------------------------------------
const blankForm = () => ({
  brand_name: '',
  pack_size: 1,
  pack_unit: 'PIECE',
  pack_label: '',
  hsn_code: '',
  list_mrp_per_pack: '',
  reorder_level_units: '',
  rack_location: '',
})

const form = reactive(blankForm())
// {id, name} from ManufacturerPicker; a null id means "create this one".
const manufacturer = ref(null)
const editingId = ref(null)
const showForm = ref(false)
const saving = ref(false)
const formError = ref(null)

const packSizeLabel = computed(() => PACK_SIZE_LABEL[form.pack_unit] || 'Pack size')
const isCountable = computed(() => form.pack_unit === 'PIECE')

const packHint = computed(() =>
  isCountable.value
    ? 'Each piece is sellable, so one soap can go out of a box of twelve.'
    : 'Sold as one whole pack — the volume or weight is a label, not a sellable quantity.',
)

const unitNoun = computed(() => SELLABLE_UNIT_LABEL[form.pack_unit] || 'unit')

/** A box holds a whole number of soaps, so refuse "." outright. */
function blockDecimalPoint(event) {
  if (isCountable.value && ['.', ',', 'e'].includes(event.key)) event.preventDefault()
}

function startAdd() {
  editingId.value = null
  Object.assign(form, blankForm())
  manufacturer.value = null
  formError.value = null
  showForm.value = true
}

function startEdit(row) {
  editingId.value = row.id
  formError.value = null
  showForm.value = true
  manufacturer.value = { id: row.manufacturer_id, name: row.manufacturer_name }
  Object.assign(form, {
    brand_name: row.brand_name,
    pack_size: row.pack_size ?? 1,
    pack_unit: row.pack_unit,
    pack_label: row.pack_label || '',
    hsn_code: row.hsn_code || '',
    list_mrp_per_pack: row.list_mrp_per_pack ?? '',
    reorder_level_units: row.reorder_level_units ?? '',
    rack_location: row.rack_location || '',
  })
}

function cancelEdit() {
  editingId.value = null
  Object.assign(form, blankForm())
  manufacturer.value = null
  formError.value = null
}

function closeForm() {
  showForm.value = false
  cancelEdit()
}

/**
 * Save, then either close or stay open for the next one.
 *
 * `keepOpen` clears the form but holds the maker: a toiletries delivery
 * brings a dozen items from one company, and retyping the maker a dozen
 * times is how the same maker ends up filed under four spellings.
 */
async function submitForm(keepOpen = false) {
  if (!manufacturer.value) {
    formError.value = 'Choose the maker, or use what you typed to create one.'
    return
  }
  saving.value = true
  formError.value = null
  notice.value = null

  // No composition, no schedule, no dosage form — the server normalises all
  // three for a general item, and sending them would only be noise.
  const payload = {
    brand_name: form.brand_name.trim(),
    item_type: ItemType.GENERAL,
    pack_size: String(form.pack_size),
    pack_unit: form.pack_unit,
    pack_label: form.pack_label.trim(),
    hsn_code: nullable(form.hsn_code),
    list_mrp_per_pack: nullable(form.list_mrp_per_pack),
    reorder_level_units: nullable(form.reorder_level_units),
    rack_location: nullable(form.rack_location),
  }

  try {
    if (editingId.value) {
      const saved = await updateMedicine(editingId.value, payload)
      notice.value = `Updated ${saved.brand_name} — ${saved.pack_label}.`
      cancelEdit()
      showForm.value = false
    } else {
      const created = await createMedicine({
        ...payload,
        manufacturer_id: manufacturer.value.id,
        manufacturer_name: manufacturer.value.id ? null : manufacturer.value.name,
      })
      notice.value =
        `Added ${created.brand_name} — ${created.pack_label}. ` +
        'Receive stock below to give it a price.'
      const maker = manufacturer.value
      Object.assign(form, blankForm())
      manufacturer.value = null

      if (keepOpen) {
        // A maker with no id has just been created; carry the real row
        // forward so the next item does not create a second one.
        manufacturer.value = maker.id
          ? maker
          : { id: created.manufacturer_id, name: created.manufacturer_name }
      } else {
        showForm.value = false
      }
    }
    await load()
  } catch (err) {
    formError.value = err.message
  } finally {
    saving.value = false
  }
}

async function removeItem(row) {
  const ok = confirm(
    `Delete ${row.brand_name}?\n\n` +
      'An item that has never been stocked is deleted outright; one with ' +
      'batches is deactivated instead, so its stock history survives.',
  )
  if (!ok) return
  try {
    const result = await deleteMedicine(row.id)
    notice.value = result.message
    await load()
  } catch (err) {
    error.value = err.message
  }
}

// --- bulk upload ----------------------------------------------------------
//
// The same importer the medicine master uses, on its `general` layout: no
// composition column and no schedule, because a bar of soap has neither.
const showImport = ref(false)
const importFile = ref(null)
const importUpdate = ref(false)
const importing = ref(false)
const importError = ref(null)
const importResult = ref(null)
const templateBusy = ref(false)

function onFilePicked(event) {
  importFile.value = event.target.files?.[0] || null
  importResult.value = null
  importError.value = null
}

async function getTemplate(format) {
  templateBusy.value = true
  importError.value = null
  try {
    await downloadMedicineTemplate(format, 'general')
  } catch (err) {
    importError.value = err.message
  } finally {
    templateBusy.value = false
  }
}

async function runImport() {
  if (!importFile.value) return
  importing.value = true
  importError.value = null
  try {
    importResult.value = await importMedicines(importFile.value, {
      updateExisting: importUpdate.value,
      layout: 'general',
    })
    await load()
  } catch (err) {
    importError.value = err.message
  } finally {
    importing.value = false
  }
}

function closeImport() {
  showImport.value = false
  importFile.value = null
  importError.value = null
  importResult.value = null
}

// --- stock: receive a consignment, or correct one -------------------------
//
// The same panel as Inventory, scoped to general goods, so somebody stocking
// the shelf of toiletries never has to go looking for it among medicines.
const canManageStock = computed(() => auth.canAny('inventory:adjust', 'purchases:receive'))

const stockFor = ref(null)
const batches = ref([])
const loadingBatches = ref(false)
const selectedBatchId = ref('')

const selectedBatch = computed(
  () => batches.value.find((b) => String(b.id) === selectedBatchId.value) || null,
)

const packUnits = computed(
  () => Number(selectedBatch.value?.units_per_pack || stockFor.value?.units_per_pack || 1),
)

const blankStock = () => ({
  batch_no: '',
  expiry_basis: ExpiryBasis.NONE,
  expiry_date: '',
  mfg_date: '',
  best_before_months: '',
  qty_packs: '',
  mrp_per_pack: '',
  cost_per_pack: '',
  gst_rate: '18',
  default_discount_pct: '',
  max_discount_pct: '',
  reason: '',
})

const stockForm = reactive(blankStock())
const savingStock = ref(false)
const stockError = ref(null)
const stockNotice = ref(null)

async function openStock(row) {
  stockFor.value = row
  stockError.value = null
  stockNotice.value = null
  await loadBatches()
}

async function loadBatches(keepId = null) {
  if (!stockFor.value) return
  loadingBatches.value = true
  try {
    const data = await listBatches({
      medicine_id: stockFor.value.id,
      include_expired: true,
      include_empty: true,
      page_size: 100,
    })
    batches.value = data.items
    selectBatch(keepId ?? data.items[0]?.id ?? '')
  } catch (err) {
    batches.value = []
    selectBatch('')
    stockError.value = err.message
  } finally {
    loadingBatches.value = false
  }
}

function selectBatch(id) {
  selectedBatchId.value = id === '' || id === null ? '' : String(id)
  const batch = selectedBatch.value
  if (!batch) {
    Object.assign(stockForm, blankStock(), {
      mrp_per_pack: stockFor.value?.mrp_per_pack ?? '',
      gst_rate: stockFor.value?.gst_rate ? String(Number(stockFor.value.gst_rate)) : '18',
    })
    return
  }
  Object.assign(stockForm, {
    batch_no: batch.batch_no,
    expiry_basis: batch.expiry_basis || ExpiryBasis.EXPIRY_DATE,
    expiry_date: batch.has_expiry === false ? '' : batch.expiry_date,
    mfg_date: batch.mfg_date ?? '',
    best_before_months: batch.best_before_months ?? '',
    qty_packs: String(batch.qty_available_packs),
    mrp_per_pack: String(batch.mrp_per_pack),
    cost_per_pack: batch.cost_per_pack ?? '',
    gst_rate: String(Number(batch.gst_rate)),
    default_discount_pct: batch.default_discount_pct ?? '',
    max_discount_pct: batch.max_discount_pct ?? '',
    reason: '',
  })
}

const round3 = (n) => Math.round(n * 1000) / 1000

/** Signed units between what is keyed and what the ledger says is there. */
const qtyDelta = computed(() => {
  const batch = selectedBatch.value
  if (!batch || stockForm.qty_packs === '' || !Number.isFinite(Number(stockForm.qty_packs))) {
    return 0
  }
  return round3(
    round3(Number(stockForm.qty_packs) * packUnits.value) - Number(batch.qty_available_units),
  )
})

/** The expiry fields, resolved the way the server will resolve them. */
function expiryPayload() {
  return {
    expiry_basis: stockForm.expiry_basis,
    expiry_date:
      stockForm.expiry_basis === ExpiryBasis.EXPIRY_DATE ? stockForm.expiry_date : null,
    mfg_date: stockForm.expiry_basis === ExpiryBasis.BEST_BEFORE ? stockForm.mfg_date : null,
    best_before_months:
      stockForm.expiry_basis === ExpiryBasis.BEST_BEFORE
        ? Number(stockForm.best_before_months)
        : null,
  }
}

async function submitStock() {
  stockError.value = null
  stockNotice.value = null
  savingStock.value = true
  try {
    // Only corrections here. A batch that is not on file yet has to arrive
    // on a supplier invoice, which is Product inventory's job — stock with
    // no document behind it cannot be reconciled with what the shop was
    // billed, and this screen has no invoice picker.
    await amendBatch(selectedBatch.value)
  } catch (err) {
    stockError.value = err.message
  } finally {
    savingStock.value = false
  }
}

/** Terms are corrected in place; quantity moves through the ledger. */
async function amendBatch(batch) {
  const delta = qtyDelta.value
  const reason = stockForm.reason.trim()
  if (delta && reason.length < 3) {
    stockError.value =
      'A quantity change needs a reason of at least 3 characters — it goes to the stock ledger.'
    return
  }

  const changes = { mrp_per_pack: String(stockForm.mrp_per_pack), gst_rate: stockForm.gst_rate }
  if (auth.isAdmin) changes.cost_per_pack = nullable(stockForm.cost_per_pack)
  changes.default_discount_pct = nullable(stockForm.default_discount_pct)
  changes.max_discount_pct = nullable(stockForm.max_discount_pct)
  Object.assign(changes, expiryPayload())

  await updateBatch(batch.id, changes)
  const done = ['terms updated']
  if (delta) {
    await adjustBatch(batch.id, delta, reason)
    done.push(`${delta > 0 ? '+' : '−'}${trim(Math.abs(delta))} unit(s)`)
  }
  stockNotice.value = `Batch ${batch.batch_no}: ${done.join(', ')}.`
  await refresh(batch.id)
}

async function refresh(batchId) {
  await load()
  await loadBatches(batchId)
  stockForm.reason = ''
}

onMounted(load)
</script>

<template>
  <section class="stack">
    <div class="card stack">
      <h1>General items</h1>
      <p class="muted">
        Everything the shop sells that is not a medicine — soap, nappies, a thermometer. No
        salt, no schedule, and often no expiry date: a bottle prints a manufacturing date and
        a best-before period, a comb prints nothing at all. They still bill on the
        <strong>same invoice</strong> as a medicine, so a customer buying both gets one bill.
      </p>
    </div>

    <p v-if="error" class="alert alert--error">{{ error }}</p>
    <p v-if="notice" class="alert alert--ok">{{ notice }}</p>

    <!-- ---------------- the master ---------------- -->
    <div class="card stack">
      <div class="form-actions">
        <h2 class="grow">On the master</h2>
        <input v-model="query" class="grow" type="search" placeholder="Filter…" />
        <button v-if="auth.can('medicines:create')" class="btn btn--primary" @click="startAdd">
          Add an item
        </button>
        <button v-if="auth.can('medicines:create')" class="btn" @click="showImport = true">
          Bulk upload
        </button>
      </div>

      <p v-if="loading" class="muted">Loading…</p>
      <p v-else-if="!rows.length" class="muted">
        No general items yet. Add one to start stocking it.
      </p>

      <div v-else class="table-wrap">
        <table class="data">
          <thead>
            <tr>
              <th>Item</th>
              <th>Maker</th>
              <th>Pack</th>
              <th class="numeric">In stock</th>
              <th class="numeric">MRP</th>
              <th class="numeric">GST</th>
              <th>Nearest expiry</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            <tr
              v-for="row in rows"
              :key="row.id"
              :class="{ 'row--selected': stockFor?.id === row.id }"
            >
              <td>
                <strong>{{ row.brand_name }}</strong>
                <span v-if="isLow(row)" class="badge badge--warn">Low</span>
              </td>
              <td>{{ row.manufacturer_name }}</td>
              <td>{{ row.pack_label }}</td>
              <td class="numeric">
                {{ trim(row.available_units) }}
                <span class="muted">{{ SELLABLE_UNIT_LABEL[row.pack_unit] || 'unit' }}(s)</span>
              </td>
              <td class="numeric">{{ money(row.mrp_per_pack) }}</td>
              <td class="numeric">{{ row.gst_rate ? `${row.gst_rate}%` : '—' }}</td>
              <td>{{ row.nearest_expiry || '—' }}</td>
              <td class="numeric actions">
                <button v-if="canManageStock" class="btn btn--sm" @click="openStock(row)">
                  Stock
                </button>
                <button
                  v-if="auth.can('medicines:edit')"
                  class="btn btn--sm"
                  @click="startEdit(row)"
                >
                  Edit
                </button>
                <button
                  v-if="auth.can('medicines:delete')"
                  class="btn btn--sm btn--danger"
                  @click="removeItem(row)"
                >
                  Delete
                </button>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>

    <!-- ---------------- add / edit ---------------- -->
    <ModalDialog
      v-if="showForm && auth.canAny('medicines:create', 'medicines:edit')"
      wide
      :title="editingId ? 'Edit item' : 'Add a general item'"
      @close="closeForm"
    >
      <form class="stack" @submit.prevent="submitForm(false)">
        <div class="form-grid">
          <label class="field">
            <span>Item name</span>
            <input v-model="form.brand_name" required placeholder="Dettol soap" />
          </label>
          <div class="field">
            <ManufacturerPicker
              v-model="manufacturer"
              label="Maker / brand owner"
              placeholder="Search makers, e.g. Reckitt"
              :disabled="Boolean(editingId)"
            />
            <p class="field__hint">
              {{
                editingId
                  ? 'Change it under Manufacturers.'
                  : 'Pick an existing one so the same maker is not filed twice.'
              }}
            </p>
          </div>
          <label class="field">
            <span>Measured in</span>
            <select v-model="form.pack_unit">
              <option v-for="unit in PACK_UNITS" :key="unit" :value="unit">{{ unit }}</option>
            </select>
            <p class="field__hint">{{ packHint }}</p>
          </label>
          <label class="field">
            <span>{{ packSizeLabel }}</span>
            <input
              v-model="form.pack_size"
              type="number"
              :step="isCountable ? 1 : 0.001"
              :min="isCountable ? 1 : 0.001"
              required
              @keydown="blockDecimalPoint"
            />
            <p class="field__hint">
              {{ isCountable ? `Sells as ${form.pack_size || 1} ${unitNoun}(s).` : '' }}
            </p>
          </label>
          <label class="field">
            <span>Pack label</span>
            <input v-model="form.pack_label" placeholder="auto" />
            <p class="field__hint">Leave blank to generate it.</p>
          </label>
          <label class="field">
            <span>HSN code</span>
            <input v-model="form.hsn_code" maxlength="10" />
          </label>
          <label class="field">
            <span>List MRP / pack</span>
            <input v-model="form.list_mrp_per_pack" type="number" step="0.01" min="0" />
            <p class="field__hint">Reference only — a sale bills the MRP on the batch.</p>
          </label>
          <label class="field">
            <span>Reorder level (units)</span>
            <input v-model="form.reorder_level_units" type="number" step="0.001" min="0" />
          </label>
          <label class="field">
            <span>Rack</span>
            <input v-model="form.rack_location" maxlength="50" />
          </label>
        </div>

        <p v-if="formError" class="alert alert--error">{{ formError }}</p>

        <div class="form-actions">
          <button class="btn btn--primary" type="submit" :disabled="saving">
            {{ saving ? 'Saving…' : editingId ? 'Save item' : 'Add item' }}
          </button>
          <!-- General goods are catalogued in runs — a toiletries delivery
               brings a dozen. This saves and stays open, keeping the maker. -->
          <button
            v-if="!editingId"
            class="btn"
            type="button"
            :disabled="saving"
            @click="submitForm(true)"
          >
            Add another
          </button>
          <button class="btn" type="button" @click="closeForm">Cancel</button>
        </div>
      </form>
    </ModalDialog>

    <!-- ---------------- bulk upload ---------------- -->
    <ModalDialog v-if="showImport" wide title="Bulk upload general items" @close="closeImport">
      <p class="muted">
        CSV or Excel with a header row. Only <code>brand_name</code> and
        <code>manufacturer</code> have to be filled in — there is no composition and no
        schedule, because a bar of soap has neither. Start from a template: it is generated
        from the columns the importer reads, so a file built on it cannot have the wrong
        headers.
      </p>

      <div class="form-actions">
        <button class="btn" :disabled="templateBusy" @click="getTemplate('csv')">
          CSV template
        </button>
        <button class="btn" :disabled="templateBusy" @click="getTemplate('xlsx')">
          Excel template
        </button>
      </div>

      <div class="form-actions">
        <input
          type="file"
          accept=".csv,.xlsx,text/csv,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
          @change="onFilePicked"
        />
        <label class="inline">
          <input v-model="importUpdate" type="checkbox" />
          Update rows that already exist
        </label>
        <button class="btn btn--primary" :disabled="!importFile || importing" @click="runImport">
          {{ importing ? 'Uploading…' : 'Upload' }}
        </button>
      </div>

      <p v-if="importError" class="alert alert--error">{{ importError }}</p>

      <template v-if="importResult">
        <p :class="['alert', importResult.errors.length ? 'alert--warn' : 'alert--ok']">
          {{ importResult.total_rows }} rows read · {{ importResult.created }} created ·
          {{ importResult.updated }} updated · {{ importResult.skipped }} skipped ·
          {{ importResult.errors.length }} rejected
        </p>
        <div v-if="importResult.errors.length" class="table-wrap">
          <table class="data">
            <thead>
              <tr>
                <th>Row</th>
                <th>Field</th>
                <th>Problem</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="err in importResult.errors" :key="`${err.row}-${err.field}`">
                <td>{{ err.row }}</td>
                <td>{{ err.field || '—' }}</td>
                <td>{{ err.message }}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </template>

      <div class="form-actions">
        <button class="btn" type="button" @click="closeImport">
          {{ importResult ? 'Close' : 'Cancel' }}
        </button>
      </div>
    </ModalDialog>

    <!-- ---------------- stock in and corrections ---------------- -->
    <div v-if="stockFor && canManageStock" class="card stack">
      <div class="form-actions">
        <h2 class="grow">Stock — {{ stockFor.brand_name }}</h2>
        <button class="btn btn--sm" @click="stockFor = null">Close</button>
      </div>
      <p class="muted">
        Correcting what is already on the shelf. New stock arrives on a supplier invoice —
        record it under <RouterLink to="/purchases">Purchases</RouterLink>, or add it to an
        invoice already received from
        <RouterLink to="/general/inventory">Product inventory</RouterLink>.
      </p>

      <label class="field">
        <span>Batch</span>
        <select
          :value="selectedBatchId"
          :disabled="loadingBatches"
          @change="selectBatch($event.target.value)"
        >
          <option v-for="b in batches" :key="b.id" :value="String(b.id)">
            {{ b.batch_no }} ·
            {{ b.has_expiry === false ? 'no expiry' : `exp ${b.expiry_date}` }} ·
            {{ trim(b.qty_available_units) }} unit(s)
          </option>
        </select>
        <p class="field__hint">
          A batch that is not listed arrives on a supplier invoice — record it under
          <RouterLink to="/general/inventory">Product inventory</RouterLink>, which asks
          which invoice it came in on.
        </p>
      </label>

      <form v-if="selectedBatch" class="stack" @submit.prevent="submitStock">
        <div class="form-grid">
          <label class="field">
            <span>Batch / lot number</span>
            <input v-model="stockForm.batch_no" disabled maxlength="60" />
            <p class="field__hint">Fixed once received.</p>
          </label>

          <!-- Most general goods print a manufacturing date and a
               best-before period, and plenty print nothing at all. -->
          <label class="field">
            <span>Shelf life</span>
            <select v-model="stockForm.expiry_basis">
              <option v-for="(label, value) in EXPIRY_BASIS_LABEL" :key="value" :value="value">
                {{ label }}
              </option>
            </select>
          </label>
          <label v-if="stockForm.expiry_basis === ExpiryBasis.EXPIRY_DATE" class="field">
            <span>Expiry date</span>
            <input v-model="stockForm.expiry_date" type="date" required />
          </label>
          <template v-else-if="stockForm.expiry_basis === ExpiryBasis.BEST_BEFORE">
            <label class="field">
              <span>Manufactured on</span>
              <input v-model="stockForm.mfg_date" type="date" required />
            </label>
            <label class="field">
              <span>Best before (months)</span>
              <input
                v-model="stockForm.best_before_months"
                type="number"
                step="1"
                min="1"
                max="600"
                required
              />
            </label>
          </template>

          <label class="field">
            <span>{{ selectedBatch ? 'Quantity on hand (packs)' : 'Quantity (packs)' }}</span>
            <input
              v-model="stockForm.qty_packs"
              type="number"
              step="0.001"
              :min="selectedBatch ? 0 : 0.001"
              required
            />
            <p v-if="selectedBatch && qtyDelta" class="field__hint field__hint--warn">
              {{ qtyDelta > 0 ? 'Adds' : 'Removes' }} {{ trim(Math.abs(qtyDelta)) }} unit(s) —
              posted to the stock ledger.
            </p>
          </label>
          <label v-if="selectedBatch" class="field field--wide">
            <span>Reason {{ qtyDelta ? '(required)' : '' }}</span>
            <input
              v-model="stockForm.reason"
              maxlength="200"
              :required="Boolean(qtyDelta)"
              placeholder="Recount, breakage, goods received…"
            />
          </label>

          <label class="field">
            <span>MRP per pack (₹)</span>
            <input v-model="stockForm.mrp_per_pack" type="number" step="0.01" min="0.01" required />
          </label>
          <label v-if="auth.isAdmin" class="field">
            <span>Cost per pack (₹)</span>
            <input v-model="stockForm.cost_per_pack" type="number" step="0.01" min="0" />
            <p class="field__hint">Never shown to sales staff.</p>
          </label>
          <label class="field">
            <span>GST %</span>
            <select v-model="stockForm.gst_rate">
              <option value="0">0</option>
              <option value="5">5</option>
              <option value="12">12</option>
              <option value="18">18</option>
              <option value="28">28</option>
            </select>
            <p class="field__hint">General goods often sit in a different slab to medicines.</p>
          </label>
          <label class="field">
            <span>Default discount %</span>
            <input
              v-model="stockForm.default_discount_pct"
              type="number"
              step="0.01"
              min="0"
              max="100"
            />
          </label>
          <label class="field">
            <span>Max discount %</span>
            <input
              v-model="stockForm.max_discount_pct"
              type="number"
              step="0.01"
              min="0"
              max="100"
            />
          </label>
        </div>

        <p v-if="stockError" class="alert alert--error">{{ stockError }}</p>
        <p v-if="stockNotice" class="alert alert--ok">{{ stockNotice }}</p>

        <div class="form-actions">
          <button class="btn btn--primary" type="submit" :disabled="savingStock">
            {{ savingStock ? 'Saving…' : 'Update stock' }}
          </button>
        </div>
      </form>
    </div>
  </section>
</template>

<style scoped>
.grow {
  flex: 1;
  min-width: 8rem;
}

.actions {
  white-space: nowrap;
}

.actions button + button {
  margin-left: 0.35rem;
}

.field--wide {
  grid-column: 1 / -1;
}

.inline {
  display: inline-flex;
  align-items: center;
  gap: 0.4rem;
  font-size: 0.9rem;
  white-space: nowrap;
}

.field__hint--warn {
  color: var(--warn);
  font-weight: 600;
}

.row--selected {
  background: var(--accent-soft) !important;
}
</style>
