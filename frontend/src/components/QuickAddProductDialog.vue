<script setup>
/**
 * Put a product on the master without leaving the invoice being keyed.
 *
 * A distributor's invoice regularly carries something the shop has never
 * stocked. Sending the operator off to the catalogue screen means losing a
 * half-entered invoice, so the same two facts — what it is, and how the pack
 * is measured — are collected here and the row is created in place.
 *
 * Only identity is asked for. MRP, GST rate and cost belong to the
 * consignment and are already on the invoice line behind this dialog.
 */
import { computed, nextTick, ref, watch } from 'vue'

import { createMedicine } from '@/api/catalog'
import CompositionPicker from '@/components/CompositionPicker.vue'
import ManufacturerPicker from '@/components/ManufacturerPicker.vue'
import ModalDialog from '@/components/ModalDialog.vue'
import {
  COUNTABLE_UNITS,
  DEFAULT_PACK_SIZE,
  DosageForm,
  ItemType,
  PACK_SIZE_LABEL,
  PACK_UNITS_BY_FORM,
} from '@/constants'

const props = defineProps({
  /** MEDICINE or GENERAL — which form to show. */
  itemType: { type: String, required: true },
  /** Prefills the name box with whatever was typed in the search. */
  initialName: { type: String, default: '' },
  /**
   * The pack size read off the supplier's line, when there was one:
   * `{text, size, unit}`. Prefilled because the whole reason this dialog
   * opened is often that the brand exists but *this size* does not — and a
   * size retyped by hand is a size that can be keyed differently.
   */
  initialPack: { type: Object, default: null },
})

const emit = defineEmits(['close', 'created'])

const isGeneral = computed(() => props.itemType === ItemType.GENERAL)

const form = ref({
  brand_name: props.initialName,
  dosage_form: DosageForm.TABLET,
  pack_size: DEFAULT_PACK_SIZE.TABLET,
  pack_unit: 'TABLET',
  pack_label: '',
  schedule_type: 'OTC',
  hsn_code: '',
})

// A general item carries the GENERAL placeholder so pack resolution, which
// keys off the dosage form on both sides, has something to work with.
const effectiveForm = computed(() =>
  isGeneral.value ? DosageForm.GENERAL : form.value.dosage_form,
)

const packUnitOptions = computed(() => PACK_UNITS_BY_FORM[effectiveForm.value] || ['UNIT'])
const packSizeLabel = computed(() => PACK_SIZE_LABEL[form.value.pack_unit] || 'Pack size')
const isCountable = computed(() => COUNTABLE_UNITS.includes(form.value.pack_unit))

const packHint = computed(() => {
  if (!isCountable.value) {
    return 'Sold as one whole pack — the volume or weight is a label, not a quantity.'
  }
  return isGeneral.value
    ? 'Each piece is sellable, so one soap can go out of a box of twelve.'
    : 'Each piece is sellable, so a single tablet can be sold.'
})

// Set the defaults once, on open, rather than watching item_type: the
// dialog is created fresh for each kind.
if (isGeneral.value) {
  form.value.pack_unit = 'PIECE'
  form.value.pack_size = 1
}

/** The first dosage form measured in this unit — GM is an ointment, ML a syrup. */
function formMeasuredIn(unit) {
  return Object.keys(PACK_UNITS_BY_FORM).find(
    (name) => name !== DosageForm.GENERAL && PACK_UNITS_BY_FORM[name].includes(unit),
  )
}

// A size read off the invoice wins over the form default, and carries the
// form with it: "VOLINI GEL 30GM" arrives with the dialog defaulted to
// tablets, where grams are not a legal measure. COUNT means the number was a
// strip count ("15'S"), which is the pack size for a countable pack; any
// other unit names the measure directly.
const applyingHint = ref(false)

if (props.initialPack) {
  const { size, unit } = props.initialPack
  applyingHint.value = true
  if (unit === 'COUNT') {
    form.value.pack_size = Number(size)
  } else if (packUnitOptions.value.includes(unit)) {
    form.value.pack_unit = unit
    form.value.pack_size = Number(size)
  } else if (!isGeneral.value && formMeasuredIn(unit)) {
    form.value.dosage_form = formMeasuredIn(unit)
    form.value.pack_unit = unit
    form.value.pack_size = Number(size)
  }
  // The watcher below runs after setup; let it see the hint and stand down.
  nextTick(() => {
    applyingHint.value = false
  })
}

watch(effectiveForm, (next) => {
  // Only when the operator actually changes the form. Firing while the
  // invoice's own size is being applied would throw it away.
  if (applyingHint.value) return
  form.value.pack_unit = (PACK_UNITS_BY_FORM[next] || ['UNIT'])[0]
  form.value.pack_size = DEFAULT_PACK_SIZE[next] ?? 1
  form.value.pack_label = ''
})

const composition = ref(null)
// {id, name} from ManufacturerPicker; a null id means "create this one".
const manufacturer = ref(null)
const saving = ref(false)
const error = ref(null)
const added = ref([])

function nullable(value) {
  const t = String(value ?? '').trim()
  return t === '' ? null : t
}

/**
 * Save, then either use it on the line behind or stay open for the next one.
 *
 * A supplier's invoice that carries one product the shop has never stocked
 * usually carries three. `keepOpen` clears the form but holds on to the
 * maker, because those three are nearly always from the same one.
 */
async function submit(keepOpen = false) {
  if (!isGeneral.value && !composition.value) {
    error.value = 'Choose or create a composition first.'
    return
  }
  if (!manufacturer.value) {
    error.value = 'Choose the maker, or use what you typed to create one.'
    return
  }
  saving.value = true
  error.value = null
  try {
    const created = await createMedicine({
      brand_name: form.value.brand_name.trim(),
      item_type: props.itemType,
      manufacturer_id: manufacturer.value.id,
      manufacturer_name: manufacturer.value.id ? null : manufacturer.value.name,
      composition_id: isGeneral.value ? null : composition.value.id,
      dosage_form: effectiveForm.value,
      pack_size: String(form.value.pack_size),
      pack_unit: form.value.pack_unit,
      pack_label: form.value.pack_label.trim(),
      schedule_type: isGeneral.value ? 'OTC' : form.value.schedule_type,
      hsn_code: nullable(form.value.hsn_code),
    })
    if (!keepOpen) {
      // Hand it straight back so the line it was created for can use it.
      emit('created', created)
      return
    }

    added.value.push(created.brand_name)
    composition.value = null
    form.value = {
      ...form.value,
      brand_name: '',
      pack_label: '',
      hsn_code: '',
    }
  } catch (err) {
    error.value = err.message
  } finally {
    saving.value = false
  }
}
</script>

<template>
  <ModalDialog
    :title="isGeneral ? 'Add a general item' : 'Add a medicine'"
    @close="emit('close')"
  >
    <form class="stack" @submit.prevent="submit(false)">
      <p class="muted small">
        Identity only. Price, GST rate and cost come from the invoice line you are keying.
      </p>
      <p v-if="initialPack" class="alert alert--warn small">
        The supplier's line says <strong>{{ initialPack.text }}</strong>. A different size of
        the same brand is a different product, so check the pack below is the one that
        arrived.
      </p>

      <CompositionPicker v-if="!isGeneral" v-model="composition" />

      <div class="form-grid">
        <label class="field">
          <span>{{ isGeneral ? 'Item name' : 'Brand name' }}</span>
          <input v-model="form.brand_name" required autofocus />
        </label>
        <div class="field">
          <ManufacturerPicker
            v-model="manufacturer"
            :label="isGeneral ? 'Maker / brand owner' : 'Manufacturer'"
          />
          <p class="field__hint">
            Pick an existing one — four spellings of Cipla is four makers.
          </p>
        </div>

        <label v-if="!isGeneral" class="field">
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

        <label v-if="packUnitOptions.length > 1" class="field">
          <span>Measured in</span>
          <select v-model="form.pack_unit">
            <option v-for="unit in packUnitOptions" :key="unit" :value="unit">{{ unit }}</option>
          </select>
        </label>
        <label class="field">
          <span>{{ packSizeLabel }}</span>
          <input
            v-model="form.pack_size"
            type="number"
            :step="isCountable ? 1 : 0.001"
            :min="isCountable ? 1 : 0.001"
            required
          />
          <p class="field__hint">{{ packHint }}</p>
        </label>
        <label class="field">
          <span>Pack label</span>
          <input v-model="form.pack_label" placeholder="auto" />
        </label>
        <label v-if="!isGeneral" class="field">
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
      </div>

      <p v-if="error" class="alert alert--error">{{ error }}</p>

      <p v-if="added.length" class="alert alert--ok small">
        Added {{ added.join(', ') }}. The maker is kept for the next one.
      </p>

      <div class="form-actions">
        <button class="btn btn--primary" type="submit" :disabled="saving">
          {{ saving ? 'Saving…' : 'Add and use it' }}
        </button>
        <button class="btn" type="button" :disabled="saving" @click="submit(true)">
          Add another
        </button>
        <button class="btn" type="button" @click="emit('close')">Cancel</button>
      </div>
    </form>
  </ModalDialog>
</template>

<style scoped>
.small {
  font-size: 0.85rem;
}
</style>
