import client from './client'

/**
 * The counter. Everything priced here comes from the server — the client
 * sends what to sell, never what it costs.
 */

/**
 * Price a cart without writing anything. Called on every cart change, so
 * the figures on screen are the ones that will be charged.
 */
export function quoteCart(payload) {
  return client.post('/pos/quote', payload).then((r) => r.data)
}

/**
 * Confirm the sale. `idempotency_key` makes a retried submit return the
 * invoice already issued instead of billing the customer twice.
 */
export function createSale(payload) {
  return client.post('/sales', payload).then((r) => r.data)
}

export function listSales(params = {}) {
  return client.get('/sales', { params }).then((r) => r.data)
}

export function fetchSale(id) {
  return client.get(`/sales/${id}`).then((r) => r.data)
}

export function fetchSaleByInvoice(invoiceNo) {
  return client.get(`/sales/by-invoice/${invoiceNo}`).then((r) => r.data)
}

/** The printable payload. A read — looking at a bill is not printing it. */
export function fetchInvoice(id) {
  return client.get(`/sales/${id}/print`).then((r) => r.data)
}

/** Records one print, at the moment the operator commits to it. */
export function recordPrint(id) {
  return client.post(`/sales/${id}/printed`).then((r) => r.data)
}

// --- parties -------------------------------------------------------------

export function findCustomerByMobile(mobile) {
  return client.get(`/customers/by-mobile/${mobile}`).then((r) => r.data)
}

export function listCustomers(params = {}) {
  return client.get('/customers', { params }).then((r) => r.data)
}

/** One customer's card: details, purchase history and credit notes. */
export function fetchCustomer(id) {
  return client.get(`/customers/${id}`).then((r) => r.data)
}

/**
 * Removes a customer entered in error. One who has bought is deactivated
 * instead — the invoices have to keep pointing somewhere.
 */
export function deleteCustomer(id) {
  return client.delete(`/customers/${id}`).then((r) => r.data)
}

export function createCustomer(payload) {
  return client.post('/customers', payload).then((r) => r.data)
}

export function updateCustomer(id, payload) {
  return client.patch(`/customers/${id}`, payload).then((r) => r.data)
}

export function listDoctors(params = {}) {
  return client.get('/doctors', { params }).then((r) => r.data)
}

export function createDoctor(payload) {
  return client.post('/doctors', payload).then((r) => r.data)
}
