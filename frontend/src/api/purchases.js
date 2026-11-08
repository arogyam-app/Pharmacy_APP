import client, { IMPORT_TIMEOUT_MS } from './client'

/**
 * The buying side. Every route here is admin-only on the server — this is
 * where cost price enters the system — so a sales executive calling any of
 * these gets a 403, not a blank screen.
 */

// --- suppliers -----------------------------------------------------------

export function listSuppliers(params = {}) {
  return client.get('/suppliers', { params }).then((r) => r.data)
}

export function createSupplier(payload) {
  return client.post('/suppliers', payload).then((r) => r.data)
}

export function updateSupplier(id, payload) {
  return client.patch(`/suppliers/${id}`, payload).then((r) => r.data)
}

/** Deletes an unused supplier; deactivates one with purchase history. */
export function deleteSupplier(id) {
  return client.delete(`/suppliers/${id}`).then((r) => r.data)
}

// --- goods inward --------------------------------------------------------

export function listPurchases(params = {}) {
  return client.get('/purchases', { params }).then((r) => r.data)
}

export function fetchPurchase(id) {
  return client.get(`/purchases/${id}`).then((r) => r.data)
}

/**
 * Posts a supplier invoice into stock: creates batches, ledger rows and the
 * inward GST split. The response carries a `warnings` array — a differing
 * MRP on a repeat batch, an already-expired lot — none of them fatal.
 */
export function createPurchase(payload) {
  return client.post('/purchases', payload).then((r) => r.data)
}

/** Payment status and notes only; the figures are the supplier's. */
/**
 * Adds lines to an invoice already received.
 *
 * Only additions — the document keeps its number and every line already on
 * it. This is how a medicine and a general item end up on the same supplier
 * invoice when they are entered from two different screens.
 */
export function addPurchaseItems(id, items) {
  return client.post(`/purchases/${id}/items`, { items }).then((r) => r.data)
}

/**
 * Reads a supplier's item list and says what each line might mean.
 *
 * Receives nothing: the rows come back parsed, each with the products its
 * text might mean, and the operator resolves them before the ordinary
 * goods-inward call moves any stock.
 */
export function previewPurchaseImport(file) {
  const form = new FormData()
  form.append('file', file)
  return client
    .post('/purchases/import-preview', form, {
      timeout: IMPORT_TIMEOUT_MS,
      headers: { 'Content-Type': 'multipart/form-data' },
    })
    .then((r) => r.data)
}

/** The blank item list. Fetched, not linked, because it needs the auth header. */
export async function downloadPurchaseTemplate(format = 'csv') {
  const response = await client.get('/purchases/import-template', {
    params: { format },
    responseType: 'blob',
  })
  const url = URL.createObjectURL(response.data)
  const link = document.createElement('a')
  link.href = url
  link.download = `purchase-items.${format}`
  link.click()
  URL.revokeObjectURL(url)
}

export function updatePurchase(id, payload) {
  return client.patch(`/purchases/${id}`, payload).then((r) => r.data)
}

/** Reverses the stock through the ledger. Refused once anything has sold. */
export function cancelPurchase(id, reason) {
  return client.post(`/purchases/${id}/cancel`, { reason }).then((r) => r.data)
}
