import client from './client'

/**
 * Batches carry the commercial terms — MRP, GST rate, discount limits and
 * cost — because they change with every consignment.
 *
 * Cost fields are simply absent from the response for a sales executive; the
 * server returns a different model rather than blanking them.
 */

export function listStock(params = {}) {
  return client.get('/inventory/stock', { params }).then((r) => r.data)
}

export function listBatches(params = {}) {
  return client.get('/inventory/batches', { params }).then((r) => r.data)
}

export function createBatch(payload) {
  return client.post('/inventory/batches', payload).then((r) => r.data)
}

export function updateBatch(id, payload) {
  return client.patch(`/inventory/batches/${id}`, payload).then((r) => r.data)
}

/** Quantities move through the ledger, never by direct edit. */
export function adjustBatch(id, qtyUnitsDelta, reason) {
  return client
    .post(`/inventory/batches/${id}/adjust`, {
      qty_units_delta: String(qtyUnitsDelta),
      reason,
    })
    .then((r) => r.data)
}

/** Only possible while nothing has been sold or returned against the batch. */
export function deleteBatch(id) {
  return client.delete(`/inventory/batches/${id}`).then((r) => r.data)
}

export function listLedger(params = {}) {
  return client.get('/inventory/ledger', { params }).then((r) => r.data)
}
