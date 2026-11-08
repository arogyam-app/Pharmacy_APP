<script setup>
/**
 * Builds a new composition one salt at a time.
 *
 * Each row is a salt plus its strength. The salt box suggests existing salts
 * as you type but accepts a brand-new name too, so a formulation the shop has
 * never stocked can be entered without leaving the medicine form.
 */
import { computed, onMounted, ref, watch } from 'vue'

import { createComposition, fetchSimilarCompositions, listSalts } from '@/api/catalog'
import { useDebounced } from '@/composables/useDebounced'

const emit = defineEmits(['close', 'created', 'use-existing'])
const props = defineProps({
  initialText: { type: String, default: '' },
})

const rows = ref([{ name: '', strength: '' }])
const knownSalts = ref([])
const therapeuticClass = ref('')
const saving = ref(false)
const error = ref(null)

// Suggestions for whichever salt box was last typed in.
const saltQuery = ref('')
const debouncedSaltQuery = useDebounced(saltQuery, 200)

watch(debouncedSaltQuery, async (value) => {
  const term = value.trim()
  if (term.length < 2) return
  try {
    const data = await listSalts({ q: term, page_size: 20 })
    knownSalts.value = data.items
  } catch {
    // Suggestions are a convenience; typing a new salt must still work.
  }
})

onMounted(async () => {
  if (props.initialText.trim()) {
    // Pre-split anything already typed in the medicine form.
    rows.value = props.initialText.split('+').map((part) => {
      const text = part.trim()
      // Brackets first, so canonical text pasted back in splits the way it
      // was written. The closing one is optional — a truncated paste is far
      // commoner than a perfect one. `%` carries no trailing word boundary,
      // and insisting on one is what made every "1% w/v" parse as part of
      // the salt name.
      const bracketed = text.match(/^(.+?)\s*\(\s*([^()]+?)\s*\)?\s*$/)
      if (bracketed) return { name: bracketed[1].trim(), strength: bracketed[2].trim() }

      // The unit's letters may be spaced apart: "100 M G" means milligrams.
      const match = text.match(
        /^(.*?)[\s-]*(\d+(?:\.\d+)?\s*(?:(?:m\s*c\s*g|m\s*g|g\s*m|m\s*l|i\s*u|u\s*n\s*i\s*t\s*s?|g)\b|%).*)$/i,
      )
      return match
        ? { name: match[1].trim(), strength: match[2].trim() }
        : { name: text, strength: '' }
    })
  }
  try {
    knownSalts.value = (await listSalts({ page_size: 50 })).items
  } catch {
    knownSalts.value = []
  }
})

function addRow() {
  rows.value.push({ name: '', strength: '' })
}

function removeRow(index) {
  rows.value.splice(index, 1)
  if (!rows.value.length) addRow()
}

const filledRows = computed(() => rows.value.filter((r) => r.name.trim()))

// Milligrams per unit, so 1GM correctly outranks 500MG. Only mass units are
// unified: MG/ML is a concentration and % a proportion, and neither converts
// into the other. Mirrors `_MASS_IN_MG` on the server.
const MASS_IN_MG = { MCG: 0.001, MG: 1, GM: 1000, G: 1000 }

/** "500MG" -> ['MASS', 500]; "7.5MG/ML" -> ['MG/ML', 7.5]; null if no number. */
function measure(strength) {
  const match = strength.match(/^(\d+(?:\.\d+)?)\s*(.*)$/)
  if (!match) return null
  const amount = Number(match[1])
  const unit = match[2].trim()
  return unit in MASS_IN_MG ? ['MASS', amount * MASS_IN_MG[unit]] : [unit, amount]
}

/** Uppercased, bracketed, and tidied exactly as `normalise_strength` does. */
function tidy(value) {
  return value
    .trim()
    .replace(/\s+/g, ' ')
    .toUpperCase()
    .replace(/\s*\/\s*/g, '/')
    .replace(/(?<=\d)\s+(?=%)/g, '')
    .replace(/(?<=[0-9A-Z])\s+(?=[0-9A-Z])/g, '')
}

/**
 * Client-side echo of the server's canonical form.
 *
 * Spaces between alphanumerics close up, so "100 M G" previews as "(100MG)",
 * while a space after a sign is kept, so "1 % w/v" stays legible as
 * "(1% W/V)". The strongest ingredient leads; strengths that cannot be
 * compared fall back to alphabetical, the same rule the server applies.
 */
const preview = computed(() => {
  if (!filledRows.value.length) return ''

  const parts = filledRows.value.map((r) => ({
    // A salt name is often more than one word; its spaces are real.
    name: r.name.trim().replace(/\s+/g, ' ').toUpperCase(),
    strength: tidy(r.strength),
  }))

  const measured = parts.map((p) => (p.strength ? measure(p.strength) : null))
  const comparable =
    parts.length > 1 &&
    measured.every(Boolean) &&
    new Set(measured.map((m) => m[0])).size === 1

  const ordered = [...parts].sort((a, b) => {
    if (comparable) {
      const diff = measure(b.strength)[1] - measure(a.strength)[1]
      if (diff !== 0) return diff
    }
    return a.name.localeCompare(b.name)
  })

  return ordered.map((p) => (p.strength ? `${p.name} (${p.strength})` : p.name)).join(' + ')
})

// --- live duplicate check -------------------------------------------------
// The server matches on a space-free, uppercased key, so "paracetamol 500 mg"
// surfaces an existing "PARACETAMOL (500MG)" before a second row can be made.
const exactMatch = ref(null)
const similar = ref([])
const debouncedPreview = useDebounced(preview, 300)

watch(debouncedPreview, async (value) => {
  if (!value || value.length < 2) {
    exactMatch.value = null
    similar.value = []
    return
  }
  try {
    const result = await fetchSimilarCompositions(value)
    exactMatch.value = result.exact
    similar.value = result.similar
  } catch {
    exactMatch.value = null
    similar.value = []
  }
})

function useExisting(composition) {
  emit('use-existing', composition)
}

async function submit() {
  if (!filledRows.value.length) {
    error.value = 'Add at least one salt.'
    return
  }
  saving.value = true
  error.value = null
  try {
    const created = await createComposition({
      components: filledRows.value.map((r) => ({
        name: r.name.trim(),
        strength: r.strength.trim() || null,
      })),
      therapeutic_class: therapeuticClass.value.trim() || null,
    })
    emit('created', created)
  } catch (err) {
    error.value = err.message
  } finally {
    saving.value = false
  }
}
</script>

<template>
  <div class="backdrop" @click.self="emit('close')">
    <div class="dialog card stack" role="dialog" aria-modal="true" aria-label="New composition">
      <div>
        <h2>New composition</h2>
        <p class="muted">
          Add each salt with its strength. Order does not matter — the server stores a canonical
          form so the same formulation is never split in two.
        </p>
      </div>

      <datalist id="known-salts">
        <option v-for="salt in knownSalts" :key="salt.id" :value="salt.name" />
      </datalist>

      <div class="rows">
        <div v-for="(row, index) in rows" :key="index" class="row">
          <label class="field grow">
            <span v-if="index === 0">Salt</span>
            <input
              v-model="row.name"
              list="known-salts"
              placeholder="Amoxicillin"
              @input="saltQuery = row.name"
            />
          </label>
          <label class="field strength">
            <span v-if="index === 0">Strength</span>
            <input v-model="row.strength" placeholder="500mg, 7.5mg/ml, 1% w/v" />
          </label>
          <button
            class="btn btn--sm remove"
            type="button"
            :aria-label="`Remove salt ${index + 1}`"
            @click="removeRow(index)"
          >
            ×
          </button>
        </div>
      </div>

      <div class="form-actions">
        <button class="btn btn--sm" type="button" @click="addRow">+ Add another salt</button>
      </div>

      <label class="field">
        <span>Therapeutic class</span>
        <input v-model="therapeuticClass" placeholder="Antibiotic" />
      </label>

      <p v-if="preview" class="preview">
        Will be saved as <strong>{{ preview }}</strong>
      </p>

      <!-- Duplicate guard: spacing and case are ignored when matching, so a
           formulation already on file shows up before a second one is made. -->
      <div v-if="exactMatch" class="alert alert--warn stack">
        <p>
          <strong>This composition already exists</strong> as
          <code>{{ exactMatch.composition_text }}</code>
          <template v-if="exactMatch.medicine_count">
            — {{ exactMatch.medicine_count }} brand(s) already use it.
          </template>
        </p>
        <div class="form-actions">
          <button class="btn btn--sm btn--primary" type="button" @click="useExisting(exactMatch)">
            Use the existing one
          </button>
        </div>
      </div>

      <div v-else-if="similar.length" class="similar">
        <p class="muted small">Similar formulations already on file — pick one to avoid a duplicate:</p>
        <ul>
          <li v-for="item in similar" :key="item.id">
            <button type="button" @click="useExisting(item)">
              <strong>{{ item.composition_text }}</strong>
              <span class="muted small">
                {{ item.therapeutic_class || 'No class' }} · {{ item.medicine_count ?? 0 }} brand(s)
              </span>
            </button>
          </li>
        </ul>
      </div>

      <p v-if="error" class="alert alert--error">{{ error }}</p>

      <div class="form-actions end">
        <button class="btn" type="button" @click="emit('close')">Cancel</button>
        <button
          class="btn btn--primary"
          type="button"
          :disabled="saving || Boolean(exactMatch)"
          :title="exactMatch ? 'That composition already exists' : ''"
          @click="submit"
        >
          {{ saving ? 'Saving…' : 'Create composition' }}
        </button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.backdrop {
  position: fixed;
  inset: 0;
  background: rgba(16, 24, 40, 0.45);
  display: grid;
  place-items: center;
  padding: 1.5rem;
  z-index: 50;
}

.dialog {
  width: min(560px, 100%);
  max-height: 90vh;
  overflow-y: auto;
}

.rows {
  display: flex;
  flex-direction: column;
  gap: 0.5rem;
}

.row {
  display: flex;
  gap: 0.5rem;
  align-items: flex-end;
}

.grow {
  flex: 1;
}

.strength {
  width: 120px;
}

.remove {
  line-height: 1;
  padding: 0.35rem 0.6rem;
}

.preview {
  margin: 0;
  padding: 0.55rem 0.8rem;
  background: var(--accent-soft);
  border-radius: var(--radius);
  font-size: 0.9rem;
}

.similar ul {
  list-style: none;
  margin: 0.35rem 0 0;
  padding: 0;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  max-height: 180px;
  overflow-y: auto;
}

.similar li + li {
  border-top: 1px solid var(--border);
}

.similar button {
  display: flex;
  flex-direction: column;
  width: 100%;
  text-align: left;
  background: none;
  border: 0;
  font: inherit;
  padding: 0.45rem 0.75rem;
  cursor: pointer;
}

.similar button:hover {
  background: var(--accent-soft);
}

.small {
  font-size: 0.82rem;
}

.alert p {
  margin: 0;
}

.end {
  justify-content: flex-end;
}
</style>
