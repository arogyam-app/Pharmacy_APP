<script setup>
import { onMounted, reactive, ref, watch } from 'vue'

import { createManufacturer, listManufacturers, updateManufacturer } from '@/api/catalog'
import { useDebounced } from '@/composables/useDebounced'
import { useAuthStore } from '@/stores/auth'

const auth = useAuthStore()

const items = ref([])
const loading = ref(true)
const error = ref(null)
const query = ref('')
const debouncedQuery = useDebounced(query, 250)

const form = reactive({ name: '', phone: '', email: '', address: '' })
const saving = ref(false)
const formError = ref(null)

async function load() {
  loading.value = true
  error.value = null
  try {
    const data = await listManufacturers({ q: query.value.trim() || undefined, page_size: 100 })
    items.value = data.items
  } catch (err) {
    error.value = err.message
  } finally {
    loading.value = false
  }
}

watch(debouncedQuery, load)

async function submit() {
  saving.value = true
  formError.value = null
  try {
    await createManufacturer({
      name: form.name.trim(),
      phone: form.phone.trim() || null,
      email: form.email.trim() || null,
      address: form.address.trim() || null,
    })
    Object.assign(form, { name: '', phone: '', email: '', address: '' })
    await load()
  } catch (err) {
    formError.value = err.message
  } finally {
    saving.value = false
  }
}

async function toggleActive(manufacturer) {
  try {
    await updateManufacturer(manufacturer.id, { is_active: !manufacturer.is_active })
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
      <h1>Manufacturers</h1>
      <p class="muted">Renaming one refreshes the search index for all of its medicines.</p>
    </div>

    <div v-if="auth.isAdmin" class="card stack">
      <h2>Add a manufacturer</h2>
      <form class="stack" @submit.prevent="submit">
        <div class="form-grid">
          <label class="field"><span>Name</span><input v-model="form.name" required /></label>
          <label class="field"><span>Phone</span><input v-model="form.phone" maxlength="15" /></label>
          <label class="field"><span>Email</span><input v-model="form.email" type="email" /></label>
          <label class="field"><span>Address</span><input v-model="form.address" /></label>
        </div>
        <p v-if="formError" class="alert alert--error">{{ formError }}</p>
        <div class="form-actions">
          <button class="btn btn--primary" type="submit" :disabled="saving">
            {{ saving ? 'Saving…' : 'Add manufacturer' }}
          </button>
        </div>
      </form>
    </div>

    <div class="card stack">
      <input v-model="query" class="search" type="search" placeholder="Filter manufacturers…" />

      <p v-if="error" class="alert alert--error">{{ error }}</p>
      <p v-else-if="loading" class="muted">Loading…</p>
      <p v-else-if="!items.length" class="muted">No manufacturers match.</p>

      <div v-else class="table-wrap">
        <table class="data">
          <thead>
            <tr>
              <th>Name</th>
              <th>Phone</th>
              <th>Email</th>
              <th>Status</th>
              <th v-if="auth.isAdmin"></th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="item in items" :key="item.id">
              <td>{{ item.name }}</td>
              <td>{{ item.phone || '—' }}</td>
              <td>{{ item.email || '—' }}</td>
              <td>
                <span :class="['badge', item.is_active ? 'badge--ok' : 'badge--danger']">
                  {{ item.is_active ? 'Active' : 'Disabled' }}
                </span>
              </td>
              <td v-if="auth.isAdmin" class="numeric">
                <button class="btn btn--sm" @click="toggleActive(item)">
                  {{ item.is_active ? 'Disable' : 'Enable' }}
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
</style>
