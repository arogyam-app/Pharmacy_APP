<script setup>
import { computed, nextTick, onMounted, ref, watch } from 'vue'


import {
  createMedicine,
  deleteMedicine,
  downloadMedicineTemplate,
  importMedicines,
  listMedicines,
  updateMedicine,
} from '@/api/catalog'
import AlternativesPopover from '@/components/AlternativesPopover.vue'
import CompositionPicker from '@/components/CompositionPicker.vue'
import ManufacturerPicker from '@/components/ManufacturerPicker.vue'
import ModalDialog from '@/components/ModalDialog.vue'
import { useAlternatives } from '@/composables/useAlternatives'
import { useDebounced } from '@/composables/useDebounced'
import {
  COUNTABLE_UNITS,
  DEFAULT_PACK_SIZE,
  DosageForm,
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

function money(value) {
  return value === null || value === undefined ? '—' : rupees.format(Number(value))
}

/** "₹1.40 / tablet" — the figure that actually decides a substitution. */
function perUnit(medicine) {
  if (medicine.unit_mrp === null || medicine.unit_mrp === undefined) return null
  const noun = SELLABLE_UNIT_LABEL[medicine.pack_unit] || 'unit'
  return `${rupees.format(Number(medicine.unit_mrp))} / ${noun}`
}

// --- the list -------------------------------------------------------------
//
// The screen used to show nothing until two characters were typed, which
// meant a new shop opened it and saw an empty page. It lists the master
// instead, a page at a time, and the box filters that list — the same shape
// as Products, where it reads better.
const query = ref('')
const debouncedQuery = useDebounced(query, 250)
const results = ref([])
const total = ref(0)
const page = ref(1)
const pageSize = 25
const searching = ref(false)
const searchError = ref(null)
const searched = ref(false)

const pageCount = computed(() => Math.max(1, Math.ceil(total.value / pageSize)))

async function load() {
  searching.value = true
  searchError.value = null
  try {
    // This screen maintains medicines; general goods have their own.
    const data = await listMedicines({
      q: query.value.trim() || undefined,
      item_type: ItemType.MEDICINE,
      page: page.value,
      page_size: pageSize,
    })
    results.value = data.items
    total.value = data.total
    searched.value = true
  } catch (err) {
    searchError.value = err.message
  } finally {
    searching.value = false
  }
}

// A new filter starts at the first page: staying on page 4 of a list that
// now has one page shows nothing at all.
watch(debouncedQuery, () => {
  page.value = 1
  load()
})

function changePage(delta) {
  const next = page.value + delta
  if (next < 1 || next > pageCount.value) return
  page.value = next
  load()
}

// --- alternatives ---------------------------------------------------------
//
// It used to be a column beside the table that said "select a medicine" for
// most of the day. Substitution is a glance, so it is a popover on a second
// of hover — and the table gets the width back.
const alt = useAlternatives()

// --- add / edit -----------------------------------------------------------
// Identity only. MRP, GST rate and discount limits are entered against a
// batch under Inventory, because they change with every consignment.
const blankForm = () => ({
  brand_name: '',
  item_type: ItemType.MEDICINE,
  dosage_form: DosageForm.TABLET,
  pack_size: DEFAULT_PACK_SIZE.TABLET,
  pack_unit: 'TABLET',
  pack_label: '',
  schedule_type: 'OTC',
  hsn_code: '',
  list_mrp_per_pack: '',
  reorder_level_units: '',
  rack_location: '',
})

const form = ref(blankForm())
const composition = ref(null) // {id, composition_text} from CompositionPicker
// {id, name} from ManufacturerPicker. A null id means "create this one",
// which the server does from `manufacturer_name`.
const manufacturer = ref(null)
const showForm = ref(false)
const saving = ref(false)
const formError = ref(null)
const formNotice = ref(null)

// --- pack measure follows the dosage form ---------------------------------
const packUnitOptions = computed(() => PACK_UNITS_BY_FORM[form.value.dosage_form] || ['UNIT'])
const packSizeLabel = computed(() => PACK_SIZE_LABEL[form.value.pack_unit] || 'Pack size')
const isCountable = computed(() => COUNTABLE_UNITS.includes(form.value.pack_unit))

const packHint = computed(() =>
  isCountable.value
    ? 'Each piece is sellable, so a single tablet can be sold.'
    : 'Sold as one whole pack — the volume or weight is a label, not a sellable quantity.',
)

// Switching form resets the measure to that form's default rather than
// carrying over a meaningless "10 tablets" onto a syrup.
//
// Loading an existing medicine must not trigger it: that arrives with a pack
// already decided, and resetting it to the form default would quietly restate
// what was received. The flag distinguishes a user changing the dropdown from
// the form being filled in.
const loadingProduct = ref(false)

watch(() => form.value.dosage_form, (next) => {
  if (loadingProduct.value) return
  form.value.pack_unit = (PACK_UNITS_BY_FORM[next] || ['UNIT'])[0]
  form.value.pack_size = DEFAULT_PACK_SIZE[next] ?? 1
  form.value.pack_label = ''
})

/** A strip holds whole tablets, so refuse "." outright on countable forms. */
function blockDecimalPoint(event) {
  if (isCountable.value && (event.key === '.' || event.key === ',' || event.key === 'e')) {
    event.preventDefault()
  }
}

// Switching from a syrup (100 ml) to tablets must not leave "100.5" behind.
watch([() => form.value.pack_size, isCountable], ([size, countable]) => {
  if (countable && size !== '' && size !== null) {
    const whole = Math.floor(Number(size))
    if (Number.isFinite(whole) && whole !== Number(size)) {
      form.value.pack_size = Math.max(1, whole)
    }
  }
})

function nullable(value) {
  const trimmed = String(value ?? '').trim()
  return trimmed === '' ? null : trimmed
}

// Non-null while editing an existing medicine; null when adding a new one.
const editingId = ref(null)

async function startEdit(medicine) {
  loadingProduct.value = true
  editingId.value = medicine.id
  showForm.value = true
  formError.value = null
  formNotice.value = null
  composition.value = medicine.composition_id
    ? { id: medicine.composition_id, composition_text: medicine.composition_text }
    : null
  manufacturer.value = {
    id: medicine.manufacturer_id,
    name: medicine.manufacturer_name,
  }
  form.value = {
    brand_name: medicine.brand_name,
    item_type: medicine.item_type || ItemType.MEDICINE,
    dosage_form: medicine.dosage_form,
    pack_size: medicine.pack_size,
    pack_unit: medicine.pack_unit,
    pack_label: medicine.pack_label || '',
    schedule_type: medicine.schedule_type,
    hsn_code: medicine.hsn_code || '',
    list_mrp_per_pack: medicine.list_mrp_per_pack ?? '',
    reorder_level_units: medicine.reorder_level_units ?? '',
    rack_location: medicine.rack_location || '',
  }
  // Let the watcher see the loaded values and decline to reset them.
  await nextTick()
  loadingProduct.value = false
}

function cancelEdit() {
  editingId.value = null
  form.value = blankForm()
  composition.value = null
  manufacturer.value = null
  formError.value = null
}

/** Open the dialog on a blank form. */
function startAdd() {
  editingId.value = null
  form.value = blankForm()
  composition.value = null
  manufacturer.value = null
  formError.value = null
  formNotice.value = null
  showForm.value = true
}

function closeForm() {
  showForm.value = false
  cancelEdit()
}

/**
 * Save the medicine, then either close or stay open for the next one.
 *
 * `keepOpen` clears the form but holds on to the maker: cataloguing happens
 * in runs — a delivery brings six new brands from one company — and retyping
 * the maker six times is where the duplicate spellings came from.
 */
async function submitForm(keepOpen = false) {
  // Soap has no salt; everything else on the master does.
  if (!composition.value) {
    formError.value = 'Choose or create a composition first.'
    return
  }
  if (!manufacturer.value) {
    formError.value = 'Choose the maker, or use what you typed to create one.'
    return
  }
  saving.value = true
  formError.value = null
  formNotice.value = null

  const payload = {
    brand_name: form.value.brand_name.trim(),
    item_type: ItemType.MEDICINE,
    composition_id: composition.value.id,
    dosage_form: form.value.dosage_form,
    pack_size: String(form.value.pack_size),
    pack_unit: form.value.pack_unit,
    pack_label: form.value.pack_label.trim(),
    schedule_type: form.value.schedule_type,
    hsn_code: nullable(form.value.hsn_code),
    list_mrp_per_pack: nullable(form.value.list_mrp_per_pack),
    reorder_level_units: nullable(form.value.reorder_level_units),
    rack_location: nullable(form.value.rack_location),
  }

  try {
    if (editingId.value) {
      const saved = await updateMedicine(editingId.value, payload)
      formNotice.value = `Updated ${saved.brand_name} — ${saved.pack_label}.`
      editingId.value = null
      form.value = blankForm()
      composition.value = null
      manufacturer.value = null
      showForm.value = false
      await rerunSearch()
    } else {
      const created = await createMedicine({
        ...payload,
        // Either an existing maker or a name to create one from — the
        // server takes whichever is set.
        manufacturer_id: manufacturer.value.id,
        manufacturer_name: manufacturer.value.id ? null : manufacturer.value.name,
      })
      formNotice.value =
        `Added ${created.brand_name} — ${created.pack_label}` +
        `${created.composition_text ? `, ${created.composition_text}` : ''}. ` +
        'Add a batch under Inventory to give it stock and a price.'
      const maker = manufacturer.value
      form.value = blankForm()
      composition.value = null
      manufacturer.value = null
      page.value = 1
      await load()

      if (keepOpen) {
        // A maker with no id has just been created; carry the real row
        // forward so the next brand does not create a second one.
        manufacturer.value = maker.id
          ? maker
          : { id: created.manufacturer_id, name: created.manufacturer_name }
      } else {
        showForm.value = false
      }
    }
  } catch (err) {
    formError.value = err.message
  } finally {
    saving.value = false
  }
}

/** Re-read the page currently shown, after a save or a delete. */
async function rerunSearch() {
  await load()
}

async function removeMedicine(medicine) {
  const ok = confirm(
    `Delete ${medicine.brand_name}?\n\n` +
      'If it has stock on record it will be deactivated instead, so its ' +
      'batch history is kept.',
  )
  if (!ok) return
  try {
    const result = await deleteMedicine(medicine.id)
    searchError.value = null
    formNotice.value = result.message
    results.value = results.value.filter((r) => r.id !== medicine.id)
    if (alt.medicine.value?.id === medicine.id) alt.close()
    if (editingId.value === medicine.id) cancelEdit()
  } catch (err) {
    searchError.value = err.message
  }
}

// --- bulk upload ----------------------------------------------------------
const showImport = ref(false)
const importFile = ref(null)
const importUpdate = ref(false)
const importing = ref(false)
const importResult = ref(null)
const importError = ref(null)
const templateBusy = ref(false)

/**
 * Which column layout the file is in. `auto` reads it from the header row,
 * which is what makes an untouched price list work first time.
 */
const layout = ref('auto')
const templateLayout = computed(() =>
  layout.value === 'price_list' ? 'price_list' : 'standard',
)

const LAYOUT_NAME = {
  standard: 'the shop’s own template',
  price_list: 'a price list',
}

function onFilePicked(event) {
  importFile.value = event.target.files?.[0] || null
  importResult.value = null
  importError.value = null
}

async function getTemplate(format) {
  templateBusy.value = true
  importError.value = null
  try {
    await downloadMedicineTemplate(format, templateLayout.value)
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
      layout: layout.value,
    })
    // The list behind the dialog is now stale by however many rows landed.
    page.value = 1
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

onMounted(load)

const emptyMessage = computed(() => {
  if (searching.value || results.value.length) return null
  if (!searched.value) return null
  return query.value.trim()
    ? 'Nothing matches that.'
    : 'No medicines on the master yet. Add one, or bulk-upload a list.'
})
</script>

<template>
  <section class="stack">
    <div class="card stack">
      <h1>Medicines</h1>
      <p class="muted">
        The whole master, a page at a time. The box filters it both ways: type
        <strong>Crocin</strong> or type <strong>Paracetamol</strong>. Pick a row to see every
        other brand with the same salt composition. Soap, nappies and the like live under
        <RouterLink to="/general/products">Products</RouterLink>.
      </p>
    </div>

    <template v-if="auth.isAdmin">
      <div class="card stack">
        <div class="form-actions">
          <h2 class="grow">The medicine master</h2>
          <button class="btn btn--primary" @click="startAdd">Add a medicine</button>
          <button class="btn" @click="showImport = true">Bulk upload</button>
        </div>
        <p v-if="formNotice && !showForm" class="alert alert--ok">{{ formNotice }}</p>
      </div>

      <ModalDialog
        v-if="showForm"
        wide
        :title="editingId ? 'Edit medicine' : 'Add a medicine'"
        @close="closeForm"
      >
        <form class="stack" @submit.prevent="submitForm(false)">
          <!-- Full width: picking an existing composition is what links the
               new brand to its substitutes. -->
          <CompositionPicker v-model="composition" />

          <div class="form-grid">
            <label class="field">
              <span>Brand name</span>
              <input v-model="form.brand_name" required />
            </label>
            <div class="field">
              <ManufacturerPicker v-model="manufacturer" :disabled="Boolean(editingId)" />
              <p class="field__hint">
                {{
                  editingId
                    ? 'Change the maker under Manufacturers.'
                    : 'Pick an existing one — four spellings of Cipla is four makers.'
                }}
              </p>
            </div>
            <label class="field">
              <span>Form</span>
              <select v-model="form.dosage_form">
                <option
                  v-for="value in Object.values(DosageForm).filter((v) => v !== DosageForm.GENERAL)"
                  :key="value"
                  :value="value"
                >
                  {{ value }}
                </option>
              </select>
            </label>

            <!-- Pack measure changes with the dosage form: tablets per strip
                 for a strip, millilitres for a syrup, grams for an ointment. -->
            <label v-if="packUnitOptions.length > 1" class="field">
              <span>Measured in</span>
              <select v-model="form.pack_unit">
                <option v-for="unit in packUnitOptions" :key="unit" :value="unit">
                  {{ unit }}
                </option>
              </select>
            </label>
            <label class="field">
              <span>{{ packSizeLabel }}</span>
              <!-- min and step must agree: with min=0.001 and step=1 the
                   browser only accepts 0.001, 1.001, 2.001… so a plain "10"
                   was rejected. Countable forms take whole numbers only. -->
              <input
                v-model="form.pack_size"
                type="number"
                :step="isCountable ? 1 : 0.001"
                :min="isCountable ? 1 : 0.001"
                inputmode="numeric"
                required
                @keydown="blockDecimalPoint"
              />
              <p class="field__hint">{{ packHint }}</p>
            </label>
            <label class="field">
              <span>Pack label</span>
              <input v-model="form.pack_label" placeholder="auto" />
              <p class="field__hint">Leave blank to generate it.</p>
            </label>
            <label class="field">
              <span>Schedule</span>
              <select v-model="form.schedule_type">
                <option value="OTC">OTC</option>
                <option value="SCHEDULE_H">Schedule H</option>
                <option value="SCHEDULE_H1">Schedule H1</option>
                <option value="SCHEDULE_X">Schedule X</option>
                <option value="OTHER">Other</option>
              </select>
            </label>
            <label class="field">
              <span>HSN code</span>
              <input v-model="form.hsn_code" maxlength="10" />
            </label>
            <label class="field">
              <span>List MRP / pack</span>
              <input v-model="form.list_mrp_per_pack" type="number" step="0.01" min="0" />
              <p class="field__hint">
                Reference only — a sale is billed from the MRP on the batch.
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
          <p v-if="formNotice" class="alert alert--ok">{{ formNotice }}</p>

          <div class="form-actions">
            <button class="btn btn--primary" type="submit" :disabled="saving">
              {{ saving ? 'Saving…' : editingId ? 'Save changes' : 'Add medicine' }}
            </button>
            <!-- Cataloguing is done in runs — a delivery brings six new
                 brands, not one. This saves and stays open, keeping the
                 maker because those six are usually from one. -->
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

      <ModalDialog v-if="showImport" wide title="Bulk upload medicines" @close="closeImport">
        <p class="muted">
          CSV or Excel with a header row, in either of two layouts. Start from a template —
          it is generated from the columns the importer reads, so a file built on it cannot
          have the wrong headers.
        </p>

        <div class="toolbar">
          <label class="field field--inline">
            <span>File layout</span>
            <select v-model="layout">
              <option value="auto">Work it out from the header row</option>
              <option value="standard">The shop’s own template</option>
              <option value="price_list">A price list (Medicine Name, Price, Pack Info…)</option>
            </select>
          </label>
          <button class="btn" :disabled="templateBusy" @click="getTemplate('csv')">
            Download CSV template
          </button>
          <button class="btn" :disabled="templateBusy" @click="getTemplate('xlsx')">
            Download Excel template
          </button>
        </div>

        <p v-if="templateLayout === 'standard'" class="muted small">
          <strong>The shop’s own template.</strong> Required: <code>brand_name</code>,
          <code>manufacturer</code>, <code>composition</code>. Optional:
          <code>therapeutic_class</code>, <code>dosage_form</code>, <code>pack_size</code>,
          <code>pack_unit</code>, <code>pack_label</code>, <code>schedule_type</code>,
          <code>hsn_code</code>, <code>list_mrp_per_pack</code>,
          <code>reorder_level_units</code>, <code>rack_location</code>. GST rate and
          discounts are not here — they arrive with each consignment and belong to the batch.
        </p>
        <p v-else class="muted small">
          <strong>A price list.</strong> Columns: <code>Medicine Name</code>,
          <code>Price (INR)</code>, <code>Manufacturer</code>, <code>Salt/Composition</code>,
          <code>Pack Info</code>, <code>Rx Required</code> — only the name, manufacturer and
          composition have to be filled in. <code>Pack Info</code> is read as written
          (“strip of 10 tablets”, “bottle of 30 ml Oral Suspension”, “vial of 1 Powder for
          Injection”) and translated into a dosage form, a pack size and the unit it is
          measured in; where the phrase names no form, the medicine is filed as OTHER rather
          than guessed at. <code>Price</code> is kept as a reference figure — sales are
          billed from the MRP on the batch.
        </p>

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
            <template v-if="importResult.layout">
              · read as {{ LAYOUT_NAME[importResult.layout] || importResult.layout }}
            </template>
          </p>
          <p v-for="note in importResult.warnings || []" :key="note" class="alert alert--warn">
            {{ note }}
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
    </template>

    <div class="card stack">
      <div class="form-actions">
        <input
          v-model="query"
          class="search grow"
          type="search"
          placeholder="Filter by brand or generic name…"
        />
        <span class="muted small">{{ total }} medicine(s)</span>
      </div>

      <p v-if="searching" class="muted">Loading…</p>
      <p v-else-if="searchError" class="alert alert--error">{{ searchError }}</p>
      <p v-else-if="emptyMessage" class="muted">{{ emptyMessage }}</p>

      <div v-else-if="results.length">
        <div class="table-wrap">
          <table class="data">
            <thead>
              <tr>
                <th>Brand</th>
                <th>Composition</th>
                <th>Manufacturer</th>
                <th>Pack</th>
                <th class="numeric">MRP</th>
                <th class="numeric">Per unit</th>
                <th class="numeric">Max disc.</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              <tr
                v-for="medicine in results"
                :key="medicine.id"
                :class="{ 'row--selected': alt.medicine.value?.id === medicine.id }"
                @mouseenter="alt.hover(medicine, $event)"
                @mousemove="alt.hover(medicine, $event)"
                @mouseleave="alt.leave()"
                @click="alt.show(medicine, $event)"
              >
                <td>
                  <strong>{{ medicine.brand_name }}</strong>
                  <span v-if="medicine.is_prescription_required" class="badge badge--warn rx">
                    Rx
                  </span>
                </td>
                <td class="composition">{{ medicine.composition_text || '—' }}</td>
                <td>{{ medicine.manufacturer_name }}</td>
                <td>{{ medicine.pack_label || '—' }}</td>
                <!-- Stock decides the price. With none yet, the price list's
                     figure is shown, marked so it is not mistaken for one
                     anybody can be charged. -->
                <td class="numeric">
                  <template v-if="medicine.mrp_per_pack !== null">
                    {{ money(medicine.mrp_per_pack) }}
                  </template>
                  <span
                    v-else-if="medicine.list_mrp_per_pack"
                    class="listed"
                    title="From the price list. A sale is billed from the MRP on the batch."
                  >
                    {{ money(medicine.list_mrp_per_pack) }}
                    <span class="badge">list</span>
                  </span>
                  <template v-else>—</template>
                </td>
                <td class="numeric muted">{{ perUnit(medicine) || '—' }}</td>
                <td class="numeric">{{ medicine.effective_max_discount_pct }}%</td>
                <td class="numeric actions">
                  <template v-if="auth.isAdmin">
                    <button class="btn btn--sm" @click.stop="startEdit(medicine)">Edit</button>
                    <button
                      class="btn btn--sm btn--danger"
                      @click.stop="removeMedicine(medicine)"
                    >
                      Delete
                    </button>
                  </template>
                </td>
              </tr>
            </tbody>
          </table>
        </div>

      </div>

      <div v-if="total > pageSize" class="form-actions">
        <button class="btn btn--sm" :disabled="page === 1" @click="changePage(-1)">
          Previous
        </button>
        <span class="muted small">Page {{ page }} of {{ pageCount }}</span>
        <button class="btn btn--sm" :disabled="page >= pageCount" @click="changePage(1)">
          Next
        </button>
      </div>
    </div>

    <!-- Same-salt alternatives, after a second of resting on a row.
         Clicking a row opens it at once, which is the path a touchscreen
         and a keyboard have. -->
    <AlternativesPopover
      v-if="alt.open.value"
      :medicine="alt.medicine.value"
      :rows="alt.rows.value"
      :loading="alt.loading.value"
      :anchor="alt.anchor.value"
      @hold="alt.hold()"
      @leave="alt.leave()"
      @pick="alt.close()"
    />
  </section>
</template>

<style scoped>
.search {
  width: 100%;
  font-size: 1rem;
  padding: 0.4rem 0.7rem;
}

.search:focus {
  outline: 2px solid var(--accent);
  outline-offset: -1px;
}

tbody tr {
  cursor: pointer;
}

.row--selected {
  background: var(--accent-soft) !important;
}

.kind {
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: var(--sp-2) var(--sp-3);
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--sp-3);
}

.field__legend {
  font-size: 0.78rem;
  font-weight: 600;
  color: var(--text-muted);
  padding: 0 var(--sp-1);
}

.kind__option {
  display: inline-flex;
  align-items: center;
  gap: 0.35rem;
  font-size: 0.9rem;
}

.kind .field__hint {
  flex-basis: 100%;
  margin: 0;
}

.composition {
  font-size: 0.85rem;
  color: var(--text-muted);
  white-space: normal;
}

.rx {
  margin-left: 0.4rem;
}






.small {
  font-size: 0.82rem;
}

.grow {
  flex: 1;
  margin: 0;
}

.actions {
  white-space: nowrap;
}

.actions button + button {
  margin-left: 0.35rem;
}

.inline {
  display: inline-flex;
  align-items: center;
  gap: 0.4rem;
  font-size: 0.9rem;
}

/* A layout picker that sits in the toolbar rather than the form grid. */
.field--inline {
  flex-direction: row;
  align-items: center;
  gap: var(--sp-2);
  margin: 0;
}

.field--inline > span {
  white-space: nowrap;
}

.listed {
  color: var(--text-muted);
}

.listed .badge {
  font-size: 0.65rem;
  margin-left: 0.25rem;
}
</style>
