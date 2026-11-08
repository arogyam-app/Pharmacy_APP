<script setup>
import { computed, onMounted, reactive, ref, watch } from 'vue'

import {
  createSupplier,
  deleteSupplier,
  listSuppliers,
  updateSupplier,
} from '@/api/purchases'
import { useDebounced } from '@/composables/useDebounced'
import { useAuthStore } from '@/stores/auth'

const auth = useAuthStore()

const items = ref([])
const loading = ref(true)
const error = ref(null)
const notice = ref(null)

const query = ref('')
const debouncedQuery = useDebounced(query, 250)

const blank = () => ({
  name: '',
  gstin: '',
  state_code: '',
  contact_person: '',
  phone: '',
  email: '',
  address: '',
  drug_licence_no: '',
  payment_terms_days: '',
})

const form = reactive(blank())
const editingId = ref(null)
const saving = ref(false)
const formError = ref(null)

const storeStateCode = computed(() => auth.store?.state_code || null)

/** Shown live, because it decides the tax split on every invoice received. */
const taxHint = computed(() => {
  const code = form.state_code.trim()
  if (!code || !storeStateCode.value) return null
  return code === storeStateCode.value
    ? 'Same state as the shop — invoices will be split into CGST + SGST.'
    : `State ${code} differs from the shop's ${storeStateCode.value} — invoices will carry IGST.`
})

async function load() {
  loading.value = true
  error.value = null
  try {
    const data = await listSuppliers({ q: query.value.trim() || undefined, page_size: 100 })
    items.value = data.items
  } catch (err) {
    error.value = err.message
  } finally {
    loading.value = false
  }
}

watch(debouncedQuery, load)

function startEdit(supplier) {
  editingId.value = supplier.id
  Object.assign(form, blank(), {
    ...supplier,
    gstin: supplier.gstin || '',
    state_code: supplier.state_code || '',
    contact_person: supplier.contact_person || '',
    phone: supplier.phone || '',
    email: supplier.email || '',
    address: supplier.address || '',
    drug_licence_no: supplier.drug_licence_no || '',
    payment_terms_days: supplier.payment_terms_days ?? '',
  })
  formError.value = null
  window.scrollTo({ top: 0, behavior: 'smooth' })
}

function cancelEdit() {
  editingId.value = null
  Object.assign(form, blank())
  formError.value = null
}

function payload() {
  const trimmed = (value) => (String(value).trim() ? String(value).trim() : null)
  return {
    name: form.name.trim(),
    gstin: trimmed(form.gstin),
    state_code: trimmed(form.state_code),
    contact_person: trimmed(form.contact_person),
    phone: trimmed(form.phone),
    email: trimmed(form.email),
    address: trimmed(form.address),
    drug_licence_no: trimmed(form.drug_licence_no),
    payment_terms_days: form.payment_terms_days === '' ? null : Number(form.payment_terms_days),
  }
}

async function submit() {
  saving.value = true
  formError.value = null
  notice.value = null
  try {
    if (editingId.value) {
      const saved = await updateSupplier(editingId.value, payload())
      notice.value = `${saved.name} updated.`
      cancelEdit()
    } else {
      const created = await createSupplier(payload())
      notice.value = `${created.name} added.`
      Object.assign(form, blank())
    }
    await load()
  } catch (err) {
    formError.value = err.message
  } finally {
    saving.value = false
  }
}

async function toggleActive(supplier) {
  error.value = null
  try {
    await updateSupplier(supplier.id, { is_active: !supplier.is_active })
    await load()
  } catch (err) {
    error.value = err.message
  }
}

async function remove(supplier) {
  const warning = supplier.purchase_count
    ? `${supplier.name} has ${supplier.purchase_count} purchase invoice(s), so it will be deactivated rather than deleted. Continue?`
    : `Delete ${supplier.name}?`
  if (!confirm(warning)) return

  error.value = null
  notice.value = null
  try {
    const result = await deleteSupplier(supplier.id)
    notice.value = result.message
    if (editingId.value === supplier.id) cancelEdit()
    await load()
  } catch (err) {
    error.value = err.message
  }
}

onMounted(load)
</script>

<template>
  <section class="stack">
    <div class="card stack">
      <h1>Suppliers</h1>
      <p class="muted">
        The distributors goods are bought from. A supplier's state code decides the tax split
        on every invoice received from them.
      </p>
    </div>

    <div class="card stack">
      <h2>{{ editingId ? 'Edit supplier' : 'Add a supplier' }}</h2>
      <form class="stack" @submit.prevent="submit">
        <div class="form-grid">
          <label class="field">
            <span>Name</span>
            <input v-model="form.name" required minlength="2" />
          </label>
          <label class="field">
            <span>GSTIN</span>
            <input v-model="form.gstin" maxlength="15" placeholder="15 characters" />
          </label>
          <label class="field">
            <span>State code</span>
            <input v-model="form.state_code" maxlength="2" placeholder="18" />
            <p v-if="taxHint" class="field__hint">{{ taxHint }}</p>
          </label>
          <label class="field">
            <span>Contact person</span>
            <input v-model="form.contact_person" />
          </label>
          <label class="field">
            <span>Phone</span>
            <input v-model="form.phone" maxlength="15" />
          </label>
          <label class="field">
            <span>Email</span>
            <input v-model="form.email" type="email" />
          </label>
          <label class="field">
            <span>Drug licence no.</span>
            <input v-model="form.drug_licence_no" />
          </label>
          <label class="field">
            <span>Payment terms (days)</span>
            <input v-model="form.payment_terms_days" type="number" min="0" max="365" step="1" />
          </label>
          <label class="field field--wide">
            <span>Address</span>
            <input v-model="form.address" />
          </label>
        </div>

        <p v-if="formError" class="alert alert--error">{{ formError }}</p>

        <div class="form-actions">
          <button class="btn btn--primary" type="submit" :disabled="saving">
            {{ saving ? 'Saving…' : editingId ? 'Save changes' : 'Add supplier' }}
          </button>
          <button v-if="editingId" class="btn" type="button" @click="cancelEdit">Cancel</button>
        </div>
      </form>
    </div>

    <div class="card stack">
      <input v-model="query" class="search" type="search" placeholder="Filter by name or GSTIN…" />

      <p v-if="error" class="alert alert--error">{{ error }}</p>
      <p v-if="notice" class="alert alert--ok">{{ notice }}</p>
      <p v-if="loading" class="muted">Loading…</p>
      <p v-else-if="!items.length" class="muted">No suppliers match.</p>

      <div v-else class="table-wrap">
        <table class="data">
          <thead>
            <tr>
              <th>Name</th>
              <th>GSTIN</th>
              <th>State</th>
              <th>Contact</th>
              <th class="numeric">Terms</th>
              <th class="numeric">Invoices</th>
              <th>Status</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="item in items" :key="item.id" :class="{ inactive: !item.is_active }">
              <td>{{ item.name }}</td>
              <td>{{ item.gstin || '—' }}</td>
              <td>{{ item.state_code || '—' }}</td>
              <td>
                {{ item.contact_person || '—' }}
                <small v-if="item.phone" class="muted"><br />{{ item.phone }}</small>
              </td>
              <td class="numeric">{{ item.payment_terms_days ?? '—' }}</td>
              <td class="numeric">{{ item.purchase_count ?? '—' }}</td>
              <td>
                <span :class="['badge', item.is_active ? 'badge--ok' : 'badge--danger']">
                  {{ item.is_active ? 'Active' : 'Disabled' }}
                </span>
              </td>
              <td class="numeric actions">
                <button class="btn btn--sm" @click="startEdit(item)">Edit</button>
                <button class="btn btn--sm" @click="toggleActive(item)">
                  {{ item.is_active ? 'Disable' : 'Enable' }}
                </button>
                <button class="btn btn--sm btn--danger" @click="remove(item)">Delete</button>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>
  </section>
</template>

<style scoped>
.search {
  width: 100%;
}

.field--wide {
  grid-column: 1 / -1;
}

.actions {
  white-space: nowrap;
}

.actions button + button {
  margin-left: 0.35rem;
}

.inactive td {
  opacity: 0.6;
}
</style>
