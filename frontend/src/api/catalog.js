import client, { IMPORT_TIMEOUT_MS } from './client'

// --- medicines -----------------------------------------------------------

export function listMedicines(params = {}) {
  return client.get('/medicines', { params }).then((r) => r.data)
}

/**
 * POS-style lookup: matches a brand name OR a generic/salt name.
 *
 * `itemType` scopes the search to medicines or to general goods. The counter
 * omits it — a basket mixes both — while each maintenance screen passes its
 * own kind so one never lists the other's rows.
 */
export function searchMedicines(q, { itemType, ...params } = {}) {
  return client
    .get('/medicines/search', { params: { q, item_type: itemType, ...params } })
    .then((r) => r.data)
}

/**
 * Which product a line of a supplier's invoice might mean.
 *
 * Matching is on tokens rather than the whole string, so `PARACETAMOL TAB`
 * finds `Paracetamol 500` and a trailing form word stops mattering. It
 * returns `{query, terms, pack_hint, matches, has_pack_match}` and **never
 * chooses** — the caller shows the list and the operator picks, or adds the
 * product. A `pack_hint` with `has_pack_match: false` means the brand is on
 * the master but that size is not, and two sizes are two products.
 */
export function matchMedicines(q, { itemType, limit = 12 } = {}) {
  return client
    .get('/medicines/match', { params: { q, item_type: itemType, limit } })
    .then((r) => r.data)
}

/** Other brands sharing the exact salt composition. */
export function fetchAlternatives(medicineId) {
  return client.get(`/medicines/${medicineId}/alternatives`).then((r) => r.data)
}

export function createMedicine(payload) {
  return client.post('/medicines', payload).then((r) => r.data)
}

export function updateMedicine(id, payload) {
  return client.patch(`/medicines/${id}`, payload).then((r) => r.data)
}

/**
 * Deletes an unstocked medicine outright; one with batches is deactivated
 * instead, so stock history survives. Check `deleted` on the result.
 */
export function deleteMedicine(id) {
  return client.delete(`/medicines/${id}`).then((r) => r.data)
}

/**
 * Bulk-load the medicine master. Two layouts are accepted — the shop's own
 * template and the price-list shape a manufacturer or portal exports.
 * `layout: 'auto'` reads it from the header row.
 */
export function importMedicines(file, { updateExisting = false, layout = 'auto' } = {}) {
  const form = new FormData()
  form.append('file', file)
  return client
    .post('/medicines/import', form, {
      params: { update_existing: updateExisting, layout },
      headers: { 'Content-Type': 'multipart/form-data' },
      timeout: IMPORT_TIMEOUT_MS,
    })
    .then((r) => r.data)
}

/** The blank template for either layout; fetched because it needs the auth header. */
export async function downloadMedicineTemplate(format = 'csv', layout = 'standard') {
  const response = await client.get('/medicines/import-template', {
    params: { format, layout },
    responseType: 'blob',
  })
  const url = URL.createObjectURL(response.data)
  const link = document.createElement('a')
  const name = layout === 'price_list' ? 'medicines-price-list-template' : 'medicines-template'
  link.href = url
  link.download = `${name}.${format}`
  link.click()
  URL.revokeObjectURL(url)
}

// --- compositions, salts, manufacturers ----------------------------------

export function listCompositions(params = {}) {
  return client.get('/compositions', { params }).then((r) => r.data)
}

/**
 * Formulations resembling what is being typed. Matching ignores spacing and
 * case, so "paracetamol 500 mg" finds an existing "PARACETAMOL 500MG"
 * instead of letting a duplicate be created.
 */
export function fetchSimilarCompositions(text, limit = 8) {
  return client.get('/compositions/similar', { params: { text, limit } }).then((r) => r.data)
}

export function createComposition(payload) {
  return client.post('/compositions', payload).then((r) => r.data)
}

export function updateComposition(id, payload) {
  return client.patch(`/compositions/${id}`, payload).then((r) => r.data)
}

/** Refused while any medicine still uses the composition. */
export function deleteComposition(id) {
  return client.delete(`/compositions/${id}`).then((r) => r.data)
}

/**
 * Bulk-load formulations from the downloaded template. CSV or XLSX; only
 * the `composition` column has to be filled in.
 */
export function importCompositions(file, { updateExisting = false } = {}) {
  const form = new FormData()
  form.append('file', file)
  return client
    .post('/compositions/import', form, {
      params: { update_existing: updateExisting },
      headers: { 'Content-Type': 'multipart/form-data' },
      timeout: IMPORT_TIMEOUT_MS,
    })
    .then((r) => r.data)
}

/**
 * The blank template. Fetched rather than linked because the endpoint needs
 * the auth header; the browser is then handed a blob to save.
 */
export async function downloadCompositionTemplate(format = 'csv') {
  const response = await client.get('/compositions/import-template', {
    params: { format },
    responseType: 'blob',
  })
  const url = URL.createObjectURL(response.data)
  const link = document.createElement('a')
  link.href = url
  link.download = `compositions-template.${format}`
  link.click()
  URL.revokeObjectURL(url)
}

export function listSalts(params = {}) {
  return client.get('/salts', { params }).then((r) => r.data)
}

export function listManufacturers(params = {}) {
  return client.get('/manufacturers', { params }).then((r) => r.data)
}

export function createManufacturer(payload) {
  return client.post('/manufacturers', payload).then((r) => r.data)
}

export function updateManufacturer(id, payload) {
  return client.patch(`/manufacturers/${id}`, payload).then((r) => r.data)
}
