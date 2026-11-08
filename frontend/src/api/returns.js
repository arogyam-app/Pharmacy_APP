import client from './client'

/**
 * The ways back out of a sale.
 *
 * A sales executive may take returns and *request* a void; only an admin
 * approves one, approves an out-of-window return, or cancels a credit note.
 * All of that is enforced on the server — these are just the calls.
 */

// --- returns -------------------------------------------------------------

/** Per line: sold, already returned, and what can still come back. */
export function fetchReturnable(saleId) {
  return client.get(`/returns/returnable/${saleId}`).then((r) => r.data)
}

/**
 * Take goods back. Damaged and expired lines are refunded but not
 * restocked; the response says so in `warnings`.
 */
export function createReturn(payload) {
  return client.post('/returns', payload).then((r) => r.data)
}

export function listReturns(params = {}) {
  return client.get('/returns', { params }).then((r) => r.data)
}

export function fetchReturn(id) {
  return client.get(`/returns/${id}`).then((r) => r.data)
}

// --- credit notes --------------------------------------------------------

export function listCreditNotes(params = {}) {
  return client.get('/credit-notes', { params }).then((r) => r.data)
}

export function fetchCreditNote(id) {
  return client.get(`/credit-notes/${id}`).then((r) => r.data)
}

/** Refused once any part of the note has been spent. */
export function cancelCreditNote(id, reason) {
  return client.post(`/credit-notes/${id}/cancel`, { reason }).then((r) => r.data)
}

// --- voids ---------------------------------------------------------------

/** Asks an admin to cancel the invoice; the reason is mandatory. */
export function requestVoid(saleId, reason) {
  return client.post(`/sales/${saleId}/void-request`, { reason }).then((r) => r.data)
}

export function listVoidRequests(params = {}) {
  return client.get('/void-requests', { params }).then((r) => r.data)
}

/** Approving puts every unit back in the batch it came from. */
export function approveVoid(id, reviewNote) {
  return client
    .post(`/void-requests/${id}/approve`, { review_note: reviewNote || null })
    .then((r) => r.data)
}

export function rejectVoid(id, reviewNote) {
  return client
    .post(`/void-requests/${id}/reject`, { review_note: reviewNote })
    .then((r) => r.data)
}
