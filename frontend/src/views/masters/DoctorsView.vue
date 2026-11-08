<script setup>
/**
 * Doctors — the prescribers named on invoices.
 *
 * Reachable by the counter, not just admins: a scheduled drug cannot be
 * billed without a prescriber, and the customer is standing there.
 */
import { onMounted, reactive, ref, watch } from 'vue'

import { createDoctor, listDoctors } from '@/api/pos'
import client from '@/api/client'
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
  registration_no: '',
  specialization: '',
  hospital_clinic: '',
  phone: '',
  address: '',
})
const form = reactive(blank())
const editingId = ref(null)
const saving = ref(false)
const formError = ref(null)

async function load() {
  loading.value = true
  error.value = null
  try {
    items.value = (await listDoctors({ q: query.value.trim() || undefined, page_size: 100 })).items
  } catch (err) {
    error.value = err.message
  } finally {
    loading.value = false
  }
}

watch(debouncedQuery, load)

function payload() {
  const clean = (v) => (String(v).trim() ? String(v).trim() : null)
  return {
    name: form.name.trim(),
    registration_no: clean(form.registration_no),
    specialization: clean(form.specialization),
    hospital_clinic: clean(form.hospital_clinic),
    phone: clean(form.phone),
    address: clean(form.address),
  }
}

function startEdit(doctor) {
  editingId.value = doctor.id
  Object.assign(form, blank(), {
    name: doctor.name,
    registration_no: doctor.registration_no || '',
    specialization: doctor.specialization || '',
    hospital_clinic: doctor.hospital_clinic || '',
    phone: doctor.phone || '',
    address: doctor.address || '',
  })
  formError.value = null
}

function cancelEdit() {
  editingId.value = null
  Object.assign(form, blank())
  formError.value = null
}

async function submit() {
  saving.value = true
  formError.value = null
  notice.value = null
  try {
    if (editingId.value) {
      await client.patch(`/doctors/${editingId.value}`, payload())
      notice.value = `${form.name} updated.`
      cancelEdit()
    } else {
      const created = await createDoctor(payload())
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

async function toggleActive(doctor) {
  try {
    await client.patch(`/doctors/${doctor.id}`, { is_active: !doctor.is_active })
    await load()
  } catch (err) {
    error.value = err.message
  }
}

async function remove(doctor) {
  if (!confirm(`Delete ${doctor.name}? A doctor named on any invoice is deactivated instead.`)) {
    return
  }
  error.value = null
  try {
    const result = await client.delete(`/doctors/${doctor.id}`)
    notice.value = result.data.message
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
      <h1>Doctors</h1>
      <p class="muted">
        A Schedule H, H1 or X medicine cannot be billed without naming the prescriber.
      </p>
    </div>

    <div class="card stack">
      <h2>{{ editingId ? 'Edit doctor' : 'Add a doctor' }}</h2>
      <form class="stack" @submit.prevent="submit">
        <div class="form-grid">
          <label class="field">
            <span>Name</span>
            <input v-model="form.name" required minlength="2" />
          </label>
          <label class="field">
            <span>Registration no.</span>
            <input v-model="form.registration_no" />
          </label>
          <label class="field">
            <span>Specialization</span>
            <input v-model="form.specialization" />
          </label>
          <label class="field">
            <span>Hospital / clinic</span>
            <input v-model="form.hospital_clinic" />
          </label>
          <label class="field">
            <span>Phone</span>
            <input v-model="form.phone" maxlength="15" />
          </label>
          <label class="field">
            <span>Address</span>
            <input v-model="form.address" />
          </label>
        </div>
        <p v-if="formError" class="alert alert--error">{{ formError }}</p>
        <div class="form-actions">
          <button class="btn btn--primary" type="submit" :disabled="saving">
            {{ saving ? 'Saving…' : editingId ? 'Save changes' : 'Add doctor' }}
          </button>
          <button v-if="editingId" class="btn" type="button" @click="cancelEdit">Cancel</button>
        </div>
      </form>
    </div>

    <div class="card stack">
      <input v-model="query" class="search" type="search" placeholder="Filter doctors…" />

      <p v-if="error" class="alert alert--error">{{ error }}</p>
      <p v-if="notice" class="alert alert--ok">{{ notice }}</p>
      <p v-if="loading" class="muted">Loading…</p>
      <p v-else-if="!items.length" class="muted">No doctors on file.</p>

      <div v-else class="table-wrap">
        <table class="data">
          <thead>
            <tr>
              <th>Name</th>
              <th>Registration</th>
              <th>Specialization</th>
              <th>Clinic</th>
              <th>Status</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="item in items" :key="item.id" :class="{ inactive: !item.is_active }">
              <td>
                <strong>{{ item.name }}</strong>
                <small v-if="item.phone" class="muted"><br />{{ item.phone }}</small>
              </td>
              <td>{{ item.registration_no || '—' }}</td>
              <td>{{ item.specialization || '—' }}</td>
              <td>{{ item.hospital_clinic || '—' }}</td>
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
                <button v-if="auth.isAdmin" class="btn btn--sm btn--danger" @click="remove(item)">
                  Delete
                </button>
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
