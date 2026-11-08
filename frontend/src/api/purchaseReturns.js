import client from './client'

/**
 * Debit notes — goods back to a distributor, and the credit that creates.
 *
 * The credit is a settlement, not a discount: applying it to an invoice
 * changes what is left to pay and never touches the invoice's taxable value
 * or the input tax claimed on it.
 */

export function createPurchaseReturn(payload) {
  return client.post('/purchase-returns', payload).then((r) => r.data)
}

export function listPurchaseReturns(params = {}) {
  return client.get('/purchase-returns', { params }).then((r) => r.data)
}

export function fetchPurchaseReturn(id) {
  return client.get(`/purchase-returns/${id}`).then((r) => r.data)
}

/** Refused once any of the credit has been set against an invoice. */
export function cancelPurchaseReturn(id, reason) {
  return client.post(`/purchase-returns/${id}/cancel`, { reason }).then((r) => r.data)
}

/** What this distributor still owes, oldest first. */
export function supplierCredit(supplierId) {
  return client
    .get('/purchase-returns/credit', { params: { supplier_id: supplierId } })
    .then((r) => r.data)
}

export function applyCreditToInvoice(purchaseInvoiceId, purchaseReturnId, amount) {
  return client
    .post(`/purchase-returns/apply-to-invoice/${purchaseInvoiceId}`, {
      purchase_return_id: purchaseReturnId,
      amount: String(amount),
    })
    .then((r) => r.data)
}

export function creditOnInvoice(purchaseInvoiceId) {
  return client.get(`/purchase-returns/applied/${purchaseInvoiceId}`).then((r) => r.data)
}
