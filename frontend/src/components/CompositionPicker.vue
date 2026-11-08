<script setup>
/**
 * Search-and-select for compositions.
 *
 * Typing filters the existing formulations; picking one binds its id, which
 * is what makes the new medicine an alternative of the brands already on that
 * composition. If nothing matches, the same box offers to create one.
 */
import { ref, watch } from 'vue'

import { listCompositions } from '@/api/catalog'
import { useDebounced } from '@/composables/useDebounced'
import NewCompositionDialog from './NewCompositionDialog.vue'

const model = defineModel({ type: Object, default: null }) // {id, composition_text}

const query = ref('')
const debouncedQuery = useDebounced(query, 250)
const matches = ref([])
const open = ref(false)
const searching = ref(false)
const showDialog = ref(false)

watch(debouncedQuery, async (value) => {
  const term = value.trim()
  if (term.length < 2) {
    matches.value = []
    return
  }
  searching.value = true
  try {
    matches.value = (await listCompositions({ q: term, page_size: 15 })).items
  } catch {
    matches.value = []
  } finally {
    searching.value = false
  }
})

function choose(composition) {
  model.value = composition
  query.value = ''
  matches.value = []
  open.value = false
}

function clear() {
  model.value = null
  query.value = ''
  open.value = true
}

function onCreated(composition) {
  showDialog.value = false
  choose(composition)
}
</script>

<template>
  <div class="picker">
    <span class="label">Composition</span>

    <!-- Chosen state: show what is bound, with a way back to searching. -->
    <div v-if="model" class="chosen">
      <div>
        <strong>{{ model.composition_text }}</strong>
        <p v-if="model.therapeutic_class" class="muted small">{{ model.therapeutic_class }}</p>
        <p v-if="model.medicine_count" class="muted small">
          {{ model.medicine_count }} brand(s) already on this composition
        </p>
      </div>
      <button class="btn btn--sm" type="button" @click="clear">Change</button>
    </div>

    <template v-else>
      <input
        v-model="query"
        class="input"
        type="search"
        placeholder="Search existing compositions, e.g. paracetamol"
        @focus="open = true"
      />

      <p v-if="searching" class="muted small">Searching…</p>

      <ul v-if="matches.length" class="results">
        <li v-for="item in matches" :key="item.id">
          <button type="button" @click="choose(item)">
            <strong>{{ item.composition_text }}</strong>
            <span class="muted small">
              {{ item.therapeutic_class || 'No class' }} · {{ item.medicine_count ?? 0 }} brand(s)
            </span>
          </button>
        </li>
      </ul>

      <p v-else-if="query.trim().length >= 2 && !searching" class="muted small">
        No composition matches “{{ query }}”.
      </p>

      <div class="form-actions">
        <button class="btn btn--sm" type="button" @click="showDialog = true">
          + New composition
        </button>
        <span class="muted small">Add a formulation the shop has not stocked before.</span>
      </div>
    </template>

    <NewCompositionDialog
      v-if="showDialog"
      :initial-text="query"
      @close="showDialog = false"
      @created="onCreated"
      @use-existing="onCreated"
    />
  </div>
</template>

<style scoped>
.picker {
  display: block;
}

.label {
  display: block;
  font-size: 0.85rem;
  color: var(--text-muted);
  margin-bottom: 0.25rem;
}

.input {
  width: 100%;
  font: inherit;
  padding: 0.5rem 0.7rem;
  border: 1px solid var(--border);
  border-radius: var(--radius);
}

.input:focus {
  outline: 2px solid var(--accent);
  outline-offset: -1px;
}

.chosen {
  display: flex;
  gap: 0.75rem;
  align-items: flex-start;
  justify-content: space-between;
  padding: 0.55rem 0.8rem;
  border: 1px solid var(--accent);
  background: var(--accent-soft);
  border-radius: var(--radius);
}

.chosen p {
  margin: 0.15rem 0 0;
}

.results {
  list-style: none;
  margin: 0.4rem 0 0;
  padding: 0;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  max-height: 220px;
  overflow-y: auto;
  background: var(--surface);
}

.results li + li {
  border-top: 1px solid var(--border);
}

.results button {
  display: flex;
  flex-direction: column;
  gap: 0.1rem;
  width: 100%;
  text-align: left;
  background: none;
  border: 0;
  font: inherit;
  padding: 0.5rem 0.8rem;
  cursor: pointer;
}

.results button:hover {
  background: var(--accent-soft);
}

.small {
  font-size: 0.82rem;
}
</style>
