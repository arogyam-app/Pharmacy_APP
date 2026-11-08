<script setup>
import { onMounted, ref, watch } from 'vue'

import {
  createComposition,
  deleteComposition,
  downloadCompositionTemplate,
  importCompositions,
  listCompositions,
  updateComposition,
} from '@/api/catalog'
import { useDebounced } from '@/composables/useDebounced'
import { useAuthStore } from '@/stores/auth'

const auth = useAuthStore()

const items = ref([])
const total = ref(0)
const page = ref(1)
const pageSize = 25
const loading = ref(true)
const error = ref(null)

const query = ref('')
const debouncedQuery = useDebounced(query, 250)

const newText = ref('')
const newClass = ref('')
const saving = ref(false)
const formError = ref(null)
const formNotice = ref(null)

async function load() {
  loading.value = true
  error.value = null
  try {
    const data = await listCompositions({
      q: query.value.trim() || undefined,
      page: page.value,
      page_size: pageSize,
    })
    items.value = data.items
    total.value = data.total
  } catch (err) {
    error.value = err.message
  } finally {
    loading.value = false
  }
}

watch(debouncedQuery, () => {
  page.value = 1
  load()
})

function changePage(delta) {
  const next = page.value + delta
  if (next < 1 || (next - 1) * pageSize >= total.value) return
  page.value = next
  load()
}

async function submit() {
  saving.value = true
  formError.value = null
  formNotice.value = null
  try {
    const created = await createComposition({
      composition_text: newText.value.trim(),
      therapeutic_class: newClass.value.trim() || null,
    })
    formNotice.value = `Saved as ${created.composition_text}`
    newText.value = ''
    newClass.value = ''
    await load()
  } catch (err) {
    formError.value = err.message
  } finally {
    saving.value = false
  }
}

// --- edit / delete --------------------------------------------------------
const editing = ref(null) // {id, composition_text, therapeutic_class, notes}
const rowError = ref(null)
const rowNotice = ref(null)
const savingRow = ref(false)

function startEdit(item) {
  rowError.value = null
  rowNotice.value = null
  editing.value = {
    id: item.id,
    composition_text: item.composition_text,
    therapeutic_class: item.therapeutic_class || '',
    notes: item.notes || '',
    medicine_count: item.medicine_count ?? 0,
  }
}

async function saveEdit() {
  savingRow.value = true
  rowError.value = null
  try {
    const saved = await updateComposition(editing.value.id, {
      composition_text: editing.value.composition_text.trim(),
      therapeutic_class: editing.value.therapeutic_class.trim() || null,
      notes: editing.value.notes.trim() || null,
    })
    rowNotice.value = `Saved as ${saved.composition_text}`
    editing.value = null
    await load()
  } catch (err) {
    rowError.value = err.message
  } finally {
    savingRow.value = false
  }
}

async function removeComposition(item) {
  if (item.medicine_count) {
    rowError.value =
      `${item.composition_text} is used by ${item.medicine_count} medicine(s). ` +
      'Move them to another composition first.'
    return
  }
  if (!confirm(`Delete ${item.composition_text}?`)) return
  rowError.value = null
  try {
    await deleteComposition(item.id)
    rowNotice.value = `${item.composition_text} deleted.`
    await load()
  } catch (err) {
    rowError.value = err.message
  }
}

// --- bulk upload ----------------------------------------------------------
const importFile = ref(null)
const importUpdate = ref(false)
const importing = ref(false)
const importResult = ref(null)
const importError = ref(null)
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
    await downloadCompositionTemplate(format)
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
    importResult.value = await importCompositions(importFile.value, {
      updateExisting: importUpdate.value,
    })
    await load()
  } catch (err) {
    importError.value = err.message
  } finally {
    importing.value = false
  }
}

onMounted(load)
</script>

<template>
  <section class="stack">
    <div class="card stack">
      <h1>Compositions</h1>
      <p class="muted">
        A composition is what makes two brands substitutes for each other. Ingredients are stored in
        a canonical order, so <em>A + B</em> and <em>B + A</em> are the same row.
      </p>
    </div>

    <div v-if="auth.isAdmin" class="card stack">
      <h2>Add a composition</h2>
      <form class="stack" @submit.prevent="submit">
        <div class="form-grid">
          <label class="field">
            <span>Formulation</span>
            <input
              v-model="newText"
              required
              placeholder="Amoxicillin 500mg + Clavulanic Acid 125mg"
            />
          </label>
          <label class="field">
            <span>Therapeutic class</span>
            <input v-model="newClass" placeholder="Antibiotic" />
          </label>
        </div>
        <p v-if="formError" class="alert alert--error">{{ formError }}</p>
        <p v-if="formNotice" class="alert alert--ok">{{ formNotice }}</p>
        <div class="form-actions">
          <button class="btn btn--primary" type="submit" :disabled="saving">
            {{ saving ? 'Saving…' : 'Add composition' }}
          </button>
        </div>
      </form>
    </div>

    <div v-if="auth.isAdmin" class="card stack">
      <h2>Bulk upload</h2>
      <p class="muted">
        Start from the template — it is generated from the columns the importer reads, so a
        file built on it cannot have the wrong headers. Only <code>composition</code> has to
        be filled in; <code>therapeutic_class</code> and <code>notes</code> can be left blank
        and added later.
      </p>

      <div class="form-actions">
        <button class="btn" :disabled="templateBusy" @click="getTemplate('csv')">
          Download CSV template
        </button>
        <button class="btn" :disabled="templateBusy" @click="getTemplate('xlsx')">
          Download Excel template
        </button>
      </div>

      <p class="muted small">
        One formulation per row, ingredients separated by <code>+</code>, for example
        <code>Amoxicillin 500mg + Clavulanic Acid 125mg</code>. Order, spacing, case and
        brackets do not matter — every row is re-canonicalised to
        <code>AMOXICILLIN (500MG) + CLAVULANIC ACID (125MG)</code>, so <em>B + A</em> lands
        on the same row as <em>A + B</em> and a formulation already on file is reported as
        skipped rather than duplicated. Salts are created from the ingredients
        automatically.
      </p>

      <div class="form-actions">
        <input
          type="file"
          accept=".csv,.xlsx,text/csv,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
          @change="onFilePicked"
        />
        <label class="inline">
          <input v-model="importUpdate" type="checkbox" />
          Fill in the class and notes on formulations already on file
        </label>
        <button class="btn btn--primary" :disabled="!importFile || importing" @click="runImport">
          {{ importing ? 'Uploading…' : 'Upload' }}
        </button>
      </div>

      <p v-if="importError" class="alert alert--error">{{ importError }}</p>

      <template v-if="importResult">
        <p :class="['alert', importResult.errors.length ? 'alert--warn' : 'alert--ok']">
          {{ importResult.total_rows }} row(s) read · {{ importResult.created }} added ·
          {{ importResult.updated }} filled in · {{ importResult.skipped }} already on file ·
          {{ importResult.errors.length }} rejected
        </p>
        <div v-if="importResult.errors.length" class="table-wrap">
          <table class="data">
            <thead>
              <tr>
                <th>Row</th>
                <th>Column</th>
                <th>Problem</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="err in importResult.errors" :key="`${err.row}-${err.field}`">
                <td>{{ err.row }}</td>
                <td>{{ err.field || '—' }}</td>
                <td class="wrap">{{ err.message }}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </template>
    </div>

    <div class="card stack">
      <input v-model="query" class="search" type="search" placeholder="Filter compositions…" />

      <p v-if="error" class="alert alert--error">{{ error }}</p>
      <p v-else-if="loading" class="muted">Loading…</p>
      <p v-else-if="!items.length" class="muted">No compositions match.</p>

      <p v-if="rowError" class="alert alert--error">{{ rowError }}</p>
      <p v-if="rowNotice" class="alert alert--ok">{{ rowNotice }}</p>

      <div v-if="!loading && !error && items.length" class="table-wrap">
        <table class="data">
          <thead>
            <tr>
              <th>Composition</th>
              <th>Salts</th>
              <th>Class</th>
              <th class="numeric">Brands</th>
              <th v-if="auth.isAdmin"></th>
            </tr>
          </thead>
          <tbody>
            <template v-for="item in items" :key="item.id">
              <tr v-if="editing?.id !== item.id">
                <td>{{ item.composition_text }}</td>
                <td>
                  <span v-for="c in item.components" :key="c.salt_id" class="salt">
                    {{ c.name }}<template v-if="c.strength"> {{ c.strength }}</template>
                  </span>
                </td>
                <td>{{ item.therapeutic_class || '—' }}</td>
                <td class="numeric">{{ item.medicine_count ?? '—' }}</td>
                <td v-if="auth.isAdmin" class="numeric actions">
                  <button class="btn btn--sm" @click="startEdit(item)">Edit</button>
                  <button
                    class="btn btn--sm btn--danger"
                    :disabled="Boolean(item.medicine_count)"
                    :title="
                      item.medicine_count
                        ? 'In use by ' + item.medicine_count + ' medicine(s)'
                        : 'Delete'
                    "
                    @click="removeComposition(item)"
                  >
                    Delete
                  </button>
                </td>
              </tr>

              <!-- Editing a formulation re-groups which brands are substitutes. -->
              <tr v-else class="editing">
                <td colspan="5">
                  <div class="stack">
                    <div class="form-grid">
                      <label class="field">
                        <span>Formulation</span>
                        <input v-model="editing.composition_text" />
                        <p class="field__hint">
                          Order and spacing do not matter — it is re-canonicalised on save.
                        </p>
                      </label>
                      <label class="field">
                        <span>Therapeutic class</span>
                        <input v-model="editing.therapeutic_class" />
                      </label>
                      <label class="field">
                        <span>Notes</span>
                        <input v-model="editing.notes" />
                      </label>
                    </div>
                    <p v-if="editing.medicine_count" class="alert alert--warn">
                      {{ editing.medicine_count }} medicine(s) use this composition — changing
                      the salts changes which brands are offered as substitutes for each other.
                    </p>
                    <div class="form-actions">
                      <button class="btn btn--primary btn--sm" :disabled="savingRow" @click="saveEdit">
                        {{ savingRow ? 'Saving…' : 'Save' }}
                      </button>
                      <button class="btn btn--sm" @click="editing = null">Cancel</button>
                    </div>
                  </div>
                </td>
              </tr>
            </template>
          </tbody>
        </table>
      </div>

      <div v-if="total > pageSize" class="form-actions">
        <button class="btn btn--sm" :disabled="page === 1" @click="changePage(-1)">Previous</button>
        <span class="muted">
          Page {{ page }} of {{ Math.ceil(total / pageSize) }} · {{ total }} total
        </span>
        <button
          class="btn btn--sm"
          :disabled="page * pageSize >= total"
          @click="changePage(1)"
        >
          Next
        </button>
      </div>
    </div>
  </section>
</template>

<style scoped>
.search {
  width: 100%;
}

.actions {
  white-space: nowrap;
}

.actions button + button {
  margin-left: 0.35rem;
}

.editing td {
  background: #fafbfc;
  white-space: normal;
}

.inline {
  display: flex;
  align-items: center;
  gap: var(--sp-1);
  font-size: 0.85rem;
  color: var(--text-muted);
}

.inline input {
  width: auto;
}

.wrap {
  white-space: normal;
}

.salt {
  display: inline-block;
  font-size: 0.82rem;
  background: var(--accent-soft);
  color: var(--accent);
  border-radius: 999px;
  padding: 0.1rem 0.55rem;
  margin: 0 0.3rem 0.2rem 0;
}
</style>
