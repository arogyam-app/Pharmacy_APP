import client from './client'

/**
 * What needs attention: stock about to go out of date, stock that already
 * has, and what to reorder.
 *
 * Alerts are open to the counter. The expiry register and the order book
 * are admin-only — both are about money leaving the business.
 */

// --- alerts --------------------------------------------------------------

/**
 * Every alert list takes an `itemType`, because medicines and general goods
 * are watched by different people looking at different shelves. Omit it and
 * the answer covers the whole shop — which is what the dashboard wants.
 */
const scope = (itemType) => ({ item_type: itemType || undefined })

/** Counts behind the nav badges. */
export function fetchAlertSummary(itemType) {
  return client.get('/alerts/summary', { params: scope(itemType) }).then((r) => r.data)
}

export function fetchExpired(itemType) {
  return client.get('/alerts/expired', { params: scope(itemType) }).then((r) => r.data)
}

export function fetchExpiring(days, itemType) {
  return client
    .get('/alerts/expiring', { params: { days, ...scope(itemType) } })
    .then((r) => r.data)
}

export function fetchLowStock(itemType) {
  return client.get('/alerts/low-stock', { params: scope(itemType) }).then((r) => r.data)
}

export function fetchOutOfStock(itemType) {
  return client.get('/alerts/out-of-stock', { params: scope(itemType) }).then((r) => r.data)
}

// --- expiry register -----------------------------------------------------

/** Pulls expired batches with stock into the register. Safe to re-run. */
export function scanExpiry() {
  return client.post('/expiry-register/scan').then((r) => r.data)
}

export function listExpiryRegister(params = {}) {
  return client.get('/expiry-register', { params }).then((r) => r.data)
}

/** Books the loss and zeroes the stock through the ledger. */
export function writeOff(id, notes) {
  return client
    .post(`/expiry-register/${id}/write-off`, { notes: notes || null })
    .then((r) => r.data)
}

export function returnExpiredToSupplier(id, payload) {
  return client
    .post(`/expiry-register/${id}/return-to-supplier`, payload)
    .then((r) => r.data)
}

export function recordExpiryCredit(id, amount) {
  return client
    .post(`/expiry-register/${id}/record-credit`, { amount: String(amount) })
    .then((r) => r.data)
}

// --- order book ----------------------------------------------------------

export function listOrderBook(params = {}) {
  return client.get('/order-book', { params }).then((r) => r.data)
}

/** Re-evaluates the decline rule across the whole catalogue. */
export function recomputeOrderBook() {
  return client.post('/order-book/recompute').then((r) => r.data)
}

export function updateOrderBookEntry(id, payload) {
  return client.patch(`/order-book/${id}`, payload).then((r) => r.data)
}

/** CSV grouped by supplier — the list handed to a distributor. */
export function orderBookExportUrl(status = 'OPEN') {
  return `/order-book/export?status=${encodeURIComponent(status)}`
}
