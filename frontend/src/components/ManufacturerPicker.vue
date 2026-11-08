<script setup>
/**
 * Search-and-select for the maker of a product.
 *
 * The box used to be plain text, and the server created a manufacturer for
 * whatever came through. That is fine until somebody types `Cipla Ltd`,
 * `CIPLA`, `Cipla.` and `Ciplaa` over four weeks: four rows, four sets of
 * medicines split between them, and the maker filter on the catalogue
 * quietly showing a quarter of what it should.
 *
 * So typing searches what already exists, and creating one is a deliberate
 * second step against a name that genuinely matched nothing.
 *
 * The bound value is `{id, name}`. A **null id with a name** means "create
 * this one" — both `POST /medicines` and the general-item form accept either
 * `manufacturer_id` or `manufacturer_name`, so the same shape works whether
 * the maker existed or not.
 */
import { computed, ref, watch } from 'vue'

import { listManufacturers } from '@/api/catalog'
import { useDebounced } from '@/composables/useDebounced'

const model = defineModel({ type: Object, default: null }) // {id, name}

const props = defineProps({
  label: { type: String, default: 'Manufacturer' },
  placeholder: { type: String, default: 'Search makers, e.g. Cipla' },
  /** Editing an existing product: the maker is changed under Manufacturers. */
  disabled: { type: Boolean, default: false },
})

const query = ref('')
const debouncedQuery = useDebounced(query, 250)
const matches = ref([])
const searching = ref(false)
const searched = ref(false)

watch(debouncedQuery, async (value) => {
  const term = value.trim()
  if (term.length < 2) {
    matches.value = []
    searched.value = false
    return
  }
  searching.value = true
  try {
    matches.value = (await listManufacturers({ q: term, page_size: 12 })).items
    searched.value = true
  } catch {
    matches.value = []
  } finally {
    searching.value = false
  }
})

/** An exact name already on file — creating it again would be a duplicate. */
const exact = computed(() =>
  matches.value.find(
    (m) => m.name.trim().toLowerCase() === query.value.trim().toLowerCase(),
  ),
)

function choose(manufacturer) {
  model.value = { id: manufacturer.id, name: manufacturer.name }
  query.value = ''
  matches.value = []
  searched.value = false
}

/**
 * Use the typed name for a maker that is not on file.
 *
 * No id: the server creates it when the product is saved, so a maker is
 * never left behind by a form the operator then abandoned.
 */
function useTyped() {
  const name = query.value.trim()
  if (!name) return
  if (exact.value) return choose(exact.value)
  model.value = { id: null, name }
  query.value = ''
  matches.value = []
  searched.value = false
}

function clear() {
  model.value = null
  query.value = ''
  matches.value = []
  searched.value = false
}
</script>

<template>
  <div class="picker">
    <span class="label">{{ label }}</span>

    <!-- Chosen: show what is bound, and whether it is new. -->
    <div v-if="model" class="chosen">
      <div>
        <strong>{{ model.name }}</strong>
        <span v-if="model.id === null" class="badge badge--warn">will be created</span>
      </div>
      <button v-if="!disabled" class="btn btn--sm" type="button" @click="clear">Change</button>
    </div>

    <template v-else>
      <input
        v-model="query"
        class="input"
        type="search"
        :placeholder="placeholder"
        :disabled="disabled"
        @keydown.enter.prevent="useTyped"
      />

      <p v-if="searching" class="muted small">Searching…</p>

      <ul v-else-if="matches.length" class="results">
        <li v-for="item in matches" :key="item.id">
          <button type="button" @click="choose(item)">
            <strong>{{ item.name }}</strong>
            <span v-if="item.address" class="muted small">{{ item.address }}</span>
          </button>
        </li>
      </ul>

      <!-- Nothing matched. Creating one is offered, never assumed. -->
      <div v-if="query.trim().length >= 2 && !searching && !exact" class="form-actions">
        <button class="btn btn--sm" type="button" @click="useTyped">
          Use “{{ query.trim() }}” as a new maker
        </button>
        <span v-if="searched && !matches.length" class="muted small">
          Nothing on file matches that.
        </span>
      </div>
    </template>
  </div>
</template>

<style scoped>
.picker {
  display: block;
  min-width: 0;
}

.label {
  display: block;
  font-size: 0.78rem;
  font-weight: 600;
  color: var(--text-muted);
  margin-bottom: 0.15rem;
}

.input {
  width: 100%;
}

.chosen {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 0.75rem;
  padding: 0.4rem 0.7rem;
  border: 1px solid var(--accent);
  background: var(--accent-soft);
  border-radius: var(--radius);
}

.chosen strong {
  margin-right: 0.4rem;
}

.results {
  list-style: none;
  margin: 0.35rem 0 0;
  padding: 0;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  max-height: 200px;
  overflow-y: auto;
}

.results li + li {
  border-top: 1px solid var(--border);
}

.results button {
  display: flex;
  flex-direction: column;
  width: 100%;
  text-align: left;
  background: none;
  border: 0;
  font: inherit;
  padding: 0.4rem 0.7rem;
  cursor: pointer;
}

.results button:hover {
  background: var(--accent-soft);
}

.small {
  font-size: 0.82rem;
}
</style>
