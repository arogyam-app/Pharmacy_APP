import { defineStore } from 'pinia'

import { quoteCart } from '@/api/pos'

const STORAGE_KEY = 'pharmacy.cart.v1'

/**
 * Quantities are whole sellable units. Half a tablet or a third of a bottle
 * is not something the counter can hand over, and the server rejects it, so
 * the cart never holds one.
 */
function wholeUnits(value, fallback = 1) {
  const rounded = Math.round(Number(value))
  return Number.isFinite(rounded) && rounded > 0 ? rounded : fallback
}

/**
 * The cart holds only *intent* — what to sell, how much, which batch, what
 * discount was asked for. Every price comes back from the server's quote,
 * so the screen can never show a figure the till would not charge.
 *
 * It survives a reload because a counter that loses a half-built cart when
 * the browser hiccups is a counter that loses a customer.
 */
export const useCartStore = defineStore('cart', {
  state: () => ({
    lines: [],           // {medicine, qty_units, batch_id, discount_pct}
    customer: null,      // {id?, mobile, name?}
    doctorId: null,
    overrideReason: '',
    quote: null,         // the server's pricing of `lines`
    quoting: false,
    error: null,
    notice: null,        // recoverable things the operator should know about
  }),

  getters: {
    isEmpty: (state) => state.lines.length === 0,
    totals: (state) => state.quote?.totals || null,
    warnings: (state) => state.quote?.warnings || [],
    payable: (state) => state.quote?.totals?.net_amount || '0.00',
  },

  actions: {
    restore() {
      try {
        const saved = JSON.parse(localStorage.getItem(STORAGE_KEY) || 'null')
        if (saved?.lines?.length) {
          this.lines = saved.lines
          this.customer = saved.customer || null
          this.doctorId = saved.doctorId || null
        }
      } catch {
        // A corrupt cart is not worth crashing the till over.
        localStorage.removeItem(STORAGE_KEY)
      }
    },

    persist() {
      localStorage.setItem(
        STORAGE_KEY,
        JSON.stringify({
          lines: this.lines,
          customer: this.customer,
          doctorId: this.doctorId,
        }),
      )
    },

    /**
     * Adding the same medicine again bumps the quantity rather than
     * stacking a second identical row.
     *
     * The default quantity is one **pack** — a strip of ten, a bottle —
     * because that is what a customer normally asks for. Selling loose
     * tablets is still a matter of typing the number.
     */
    add(medicine, qtyUnits = null) {
      const step = medicine.units_per_pack || 1
      const wanted = wholeUnits(qtyUnits === null ? step : qtyUnits, step)

      const existing = this.lines.find(
        (l) => l.medicine.id === medicine.id && !l.batch_id,
      )
      if (existing) {
        existing.qty_units = Number(existing.qty_units) + wanted
      } else {
        this.lines.push({
          medicine,
          qty_units: wanted,
          batch_id: null,
          discount_pct: null,
        })
      }
      this.error = null
      return this.refresh()
    },

    remove(index) {
      this.lines.splice(index, 1)
      return this.refresh()
    },

    update(index, changes) {
      if ('qty_units' in changes) {
        changes.qty_units = wholeUnits(changes.qty_units, this.lines[index].qty_units)
      }
      Object.assign(this.lines[index], changes)
      this.error = null
      return this.refresh()
    },

    setCustomer(customer) {
      this.customer = customer
      return this.refresh()
    },

    setDoctor(doctorId) {
      this.doctorId = doctorId
      return this.refresh()
    },

    clear() {
      this.lines = []
      this.customer = null
      this.doctorId = null
      this.overrideReason = ''
      this.quote = null
      this.error = null
      this.notice = null
      localStorage.removeItem(STORAGE_KEY)
    },

    quoteRequest() {
      return {
        items: this.lines.map((l) => ({
          medicine_id: l.medicine.id,
          qty_units: String(wholeUnits(l.qty_units)),
          batch_id: l.batch_id || null,
          discount_pct:
            l.discount_pct === null || l.discount_pct === ''
              ? null
              : String(l.discount_pct),
        })),
        customer: this.customerRef(),
        doctor_id: this.doctorId,
        discount_override_reason: this.overrideReason.trim() || null,
      }
    },

    /**
     * Ask the server to price the cart. The only source of any figure here.
     *
     * A cart that outlives its data — the customer or doctor it referred to
     * has since been removed — used to wedge on "not found" and, because a
     * failed quote disables payment, could not be recovered without wiping
     * localStorage. Now the stale reference is dropped and the quote
     * retried once, with a note saying what was let go.
     */
    async refresh(retrying = false) {
      this.persist()
      if (!this.lines.length) {
        this.quote = null
        this.error = null
        return null
      }

      this.quoting = true
      try {
        this.quote = await quoteCart(this.quoteRequest())
        this.error = null
      } catch (err) {
        const healed = !retrying && this.dropStaleReference(err)
        if (healed) {
          this.quoting = false
          const quote = await this.refresh(true)
          this.notice = healed
          return quote
        }
        // Keep the previous quote on screen; the message says what to fix.
        this.error = err.message
      } finally {
        this.quoting = false
      }
      return this.quote
    },

    /**
     * Let go of a customer or doctor the server no longer knows about.
     * Returns a message to show, or null when the error is nothing to do
     * with a stale reference (an over-limit discount, say, which the
     * operator must actually deal with).
     */
    dropStaleReference(err) {
      if (err.code !== 'NOT_FOUND') return null

      if (err.field === 'customer_id' || /customer/i.test(err.message || '')) {
        this.customer = null
        this.persist()
        return 'That customer record no longer exists, so the sale was set back to walk-in.'
      }
      if (err.field === 'doctor_id' || /doctor/i.test(err.message || '')) {
        this.doctorId = null
        this.persist()
        return 'That doctor is no longer on file, so the prescriber was cleared.'
      }
      if (err.field?.endsWith('medicine_id') || /medicine not found/i.test(err.message || '')) {
        // The catalogue moved on under a cart left open overnight.
        this.lines = []
        this.persist()
        return 'The saved cart referred to medicines that no longer exist, so it was emptied.'
      }
      return null
    },

    customerRef() {
      if (!this.customer) return null
      return {
        customer_id: this.customer.id || null,
        mobile: this.customer.mobile || null,
        name: this.customer.name || null,
      }
    },

    /** Payload for POST /sales. The key is what makes a double-click safe. */
    salePayload(payments, idempotencyKey) {
      return {
        ...this.quoteRequest(),
        payments,
        idempotency_key: idempotencyKey,
      }
    },
  },
})
