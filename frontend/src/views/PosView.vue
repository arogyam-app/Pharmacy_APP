<script setup>
/**
 * POS — the counter screen.
 *
 * Keyboard-first, because a queue does not wait for a mouse:
 *   F2 search · F4 customer · F9 pay · Esc close
 *
 * Nothing on this screen computes a price. Every figure comes from
 * `POST /pos/quote`, which runs the same costing code the invoice will, so
 * what the customer is quoted is what they are charged.
 */
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'

import { searchMedicines } from '@/api/catalog'
import { listBatches } from '@/api/inventory'
import {
  createDoctor,
  createSale,
  findCustomerByMobile,
  listCustomers,
  listDoctors,
} from '@/api/pos'
import { listCreditNotes } from '@/api/returns'
import AlternativesPopover from '@/components/AlternativesPopover.vue'
import { useAlternatives } from '@/composables/useAlternatives'
import { useDebounced } from '@/composables/useDebounced'
import { useCartStore } from '@/stores/cart'
import { useAuthStore } from '@/stores/auth'

const cart = useCartStore()
const auth = useAuthStore()
const router = useRouter()

const rupees = new Intl.NumberFormat('en-IN', {
  style: 'currency',
  currency: 'INR',
  minimumFractionDigits: 2,
})
const money = (v) => (v === null || v === undefined ? '—' : rupees.format(Number(v)))
const trim = (v) => Number(v).toLocaleString('en-IN', { maximumFractionDigits: 3 })

// --- search ---------------------------------------------------------------
const searchBox = ref(null)
const query = ref('')
const debouncedQuery = useDebounced(query, 200)
const results = ref([])
const highlighted = ref(0)
const searching = ref(false)

watch(debouncedQuery, async (value) => {
  const term = value.trim()
  if (term.length < 2) {
    results.value = []
    alt.close()
    return
  }
  searching.value = true
  try {
    results.value = await searchMedicines(term, { limit: 20 })
    highlighted.value = 0
  } catch (err) {
    cart.error = err.message
  } finally {
    searching.value = false
  }
})

// --- same-salt alternatives ----------------------------------------------
//
// It used to be a permanent panel under the search, redrawn on every
// keystroke that moved the highlight. Substitution is a glance — "is there a
// cheaper one?" — so it is a popover that appears after a second of resting
// on a row, and gives that space back the rest of the time.
const alt = useAlternatives()

/** Keyboard users never touch a row, so the highlight opens it too. */
watch(highlighted, () => {
  if (alt.open.value) alt.close()
})

function moveHighlight(delta) {
  if (!results.value.length) return
  const next = highlighted.value + delta
  highlighted.value = Math.max(0, Math.min(results.value.length - 1, next))
}

const inStock = (medicine) => Number(medicine.available_units || 0) > 0

async function addHighlighted() {
  const medicine = results.value[highlighted.value]
  if (medicine) await addToCart(medicine)
}

/**
 * Out-of-stock rows stay in the list — the operator needs to see that the
 * shop stocks the brand at all, and which alternatives to offer instead —
 * but they cannot be sold, so adding one is refused here as well as by the
 * server.
 */
async function addToCart(medicine) {
  if (!inStock(medicine)) {
    cart.notice = `${medicine.brand_name} has no sellable stock. Check the same-composition panel for an alternative.`
    return
  }
  cart.notice = null
  // One pack by default — a strip, a bottle — since that is what is
  // usually asked for. Loose units are a matter of typing the number.
  await cart.add(medicine)
  query.value = ''
  results.value = []
  focusSearch()
}

function focusSearch() {
  nextTick(() => searchBox.value?.focus())
}

// --- cart lines -----------------------------------------------------------
const quotedFor = (index) =>
  (cart.quote?.lines || []).filter((l) => l.medicine_id === cart.lines[index]?.medicine.id)

/** Counter shorthand for the container words the server spells out. */
const PACK_WORD = {
  strip: 'stp',
  strips: 'stp',
  bottle: 'bt',
  bottles: 'bt',
  pack: 'pk',
  packs: 'pk',
  vial: 'vl',
  vials: 'vl',
  tube: 'tb',
  tubes: 'tb',
  sachet: 'sch',
  sachets: 'sch',
  unit: 'u',
  units: 'u',
}

const shortPack = (label) =>
  String(label || '')
    .split(/\s+/)
    .map((word) => PACK_WORD[word.toLowerCase()] || word)
    .join(' ')

/**
 * One short grey line under the quantity: what the typed number comes to in
 * packs, and how a pack is made up.
 *
 *   "1 pc · 100 ml bt · 1u/pc"
 *   "2 pc + 3 ls · 15 tab stp · 15u/pc"
 *
 * Abbreviated because the column is narrow and this is read hundreds of
 * times a day by someone who already knows what it means. The full wording
 * is on the tooltip for anyone who does not.
 */
function packBreakdown(index) {
  const line = cart.lines[index]
  const per = line.medicine.units_per_pack || 1
  const qty = Number(line.qty_units)

  if (per === 1) {
    return { short: `${qty} pc`, long: `${qty} pack${qty === 1 ? '' : 's'}` }
  }

  const whole = Math.floor(qty / per)
  const loose = qty % per
  const short = []
  const long = []
  if (whole) {
    short.push(`${whole} pc`)
    long.push(`${whole} pack${whole === 1 ? '' : 's'}`)
  }
  if (loose) {
    short.push(`${loose} ls`)
    long.push(`${loose} loose`)
  }
  return {
    short: short.join(' + ') || `${qty} u`,
    long: long.join(' + ') || `${qty} unit(s)`,
  }
}

function qtyNote(index) {
  const line = cart.lines[index]
  const per = line.medicine.units_per_pack || 1
  return `${packBreakdown(index).short} · ${shortPack(line.medicine.pack_label)} · ${per}u/pc`
}

/** Spelled out, for the tooltip. */
function qtyNoteLong(index) {
  const line = cart.lines[index]
  const per = line.medicine.units_per_pack || 1
  return `${packBreakdown(index).long} · ${line.medicine.pack_label} · ${per} unit(s) per pack`
}

/**
 * Every batch this line draws from, on one line. A cart line that spans two
 * batches is two rows in the invoice, but at the counter it is still one
 * item, and stacking a paragraph per batch pushed the row height around on
 * every keystroke.
 */
function batchSummary(index) {
  return quotedFor(index)
    .map((q) => {
      const qty = Number(q.qty_units).toLocaleString('en-IN', { maximumFractionDigits: 3 })
      const soon = q.is_near_expiry ? ` — expires in ${q.days_to_expiry}d` : ''
      // A general item may have no shelf life at all; the server stores a
      // sentinel date for it, which must never reach the operator.
      const when = q.days_to_expiry === null ? 'no expiry' : `exp ${q.expiry_date}`
      return `${q.batch_no} · ${when} · ${qty}${soon}`
    })
    .join('   |   ')
}

const hasNearExpiry = (index) => quotedFor(index).some((q) => q.is_near_expiry)

/** Units are indivisible, so the box takes whole numbers only. */
async function setQty(index, value) {
  const qty = Math.round(Number(value))
  if (!Number.isFinite(qty) || qty <= 0) {
    // Snap the box back rather than let a blank or a fraction sit there.
    await cart.update(index, { qty_units: cart.lines[index].qty_units })
    return
  }
  await cart.update(index, { qty_units: qty })
}

/** A decimal point never means anything in a quantity of units. */
function blockDecimal(event) {
  if (['.', ',', 'e', 'E', '+', '-'].includes(event.key)) event.preventDefault()
}

/** Step by whole packs; for a bottle or a vial a pack *is* one unit. */
async function stepPacks(index, direction) {
  const line = cart.lines[index]
  const per = line.medicine.units_per_pack || 1
  const next = Number(line.qty_units) + direction * per
  if (next <= 0) return
  await cart.update(index, { qty_units: next })
}

async function setDiscount(index, value) {
  await cart.update(index, { discount_pct: value === '' ? null : Number(value) })
}

// --- batch picker ---------------------------------------------------------
const batchPickerFor = ref(null)
const batchOptions = ref([])

async function openBatchPicker(index) {
  batchPickerFor.value = index
  batchOptions.value = []
  try {
    const data = await listBatches({
      medicine_id: cart.lines[index].medicine.id,
      page_size: 50,
    })
    batchOptions.value = data.items
  } catch (err) {
    cart.error = err.message
  }
}

async function chooseBatch(index, batchId) {
  await cart.update(index, { batch_id: batchId })
  batchPickerFor.value = null
}

// --- customer -------------------------------------------------------------
const mobile = ref('')
const customerName = ref('')
const customerNotice = ref(null)
const customerBox = ref(null)
const customerMatches = ref([])
const searchedFor = ref('')

const digits = (value) => String(value || '').replace(/\D/g, '')
const typedMobile = computed(() => digits(mobile.value))

/**
 * Whether the number in the box is the one attached to the sale.
 *
 * Editing the number breaks the match, which re-arms the action button —
 * so a corrected digit is never silently billed to the wrong customer.
 */
const attached = computed(
  () =>
    Boolean(typedMobile.value) &&
    digits(cart.customer?.mobile) === typedMobile.value,
)

/**
 * Look the number up as it is typed, from three digits on.
 *
 * A regular customer is usually recognised by the first few digits, and
 * finding them mid-keystroke means their name and history are on screen
 * before they finish reciting the number — and it stops the same person
 * being created twice under a mistyped mobile.
 */
const debouncedMobile = useDebounced(typedMobile, 250)

watch(debouncedMobile, async (value) => {
  if (value.length < 3) {
    customerMatches.value = []
    searchedFor.value = ''
    return
  }
  // Selecting a match writes the number into the box, which would other-
  // wise trigger a fresh search and pop the list open again underneath the
  // customer just chosen.
  if (attached.value && value === digits(cart.customer?.mobile)) {
    customerMatches.value = []
    searchedFor.value = value
    return
  }
  try {
    const data = await listCustomers({ q: value, page_size: 8, is_active: true })
    customerMatches.value = data.items
  } catch {
    customerMatches.value = []
  } finally {
    searchedFor.value = value
  }
})

/** Nobody on file for a number long enough to be a real one. Suppressed
 *  once someone is attached — the offer to add is then already answered. */
const noMatch = computed(
  () =>
    !attached.value &&
    searchedFor.value === typedMobile.value &&
    typedMobile.value.length >= 6 &&
    !customerMatches.value.length,
)

const actionLabel = computed(() => {
  if (attached.value) return cart.customer?.id ? 'Selected' : 'Added'
  return noMatch.value ? 'Add new customer' : 'Use'
})

async function selectCustomer(found) {
  mobile.value = found.mobile
  customerName.value = found.name || ''
  customerMatches.value = []
  customerNotice.value = null

  await cart.setCustomer({ id: found.id, mobile: found.mobile, name: found.name })
  focusSearch()

  // History is worth a second request — it tells the operator whether this
  // is a regular. The name and number are already in the boxes above, so
  // the note carries only what those do not say.
  try {
    const detail = await findCustomerByMobile(found.mobile)
    const parts = [`${detail.sale_count || 0} previous purchase(s)`]
    if (Number(detail.credit_balance) > 0) {
      parts.push(`${money(detail.credit_balance)} credit`)
    }
    customerNotice.value = parts.join(' · ')
  } catch {
    // A missing history is not worth a message of its own.
  }
}

/** Use whatever is typed. An unknown number becomes a customer when the
 *  sale is saved, not while the operator is still typing. */
async function attachCustomer() {
  const cleaned = typedMobile.value
  if (!cleaned) return

  const wasUnknown = noMatch.value
  await cart.setCustomer({
    id: digits(cart.customer?.mobile) === cleaned ? cart.customer.id : null,
    mobile: cleaned,
    name: customerName.value.trim() || null,
  })
  customerNotice.value = wasUnknown
    ? 'New — the record is created when the sale is saved.'
    : null
  customerMatches.value = []
  focusSearch()
}

async function clearCustomer() {
  mobile.value = ''
  customerName.value = ''
  customerMatches.value = []
  customerNotice.value = null
  searchedFor.value = ''
  await cart.setCustomer(null)
}

// --- doctor ---------------------------------------------------------------
const doctorQuery = ref('')
const doctorMatches = ref([])
const doctorSearched = ref('')
const doctorNotice = ref(null)
const selectedDoctor = ref(null)
const savingDoctor = ref(false)

const typedDoctor = computed(() => doctorQuery.value.trim())
const doctorAttached = computed(
  () => Boolean(cart.doctorId) && selectedDoctor.value?.name === typedDoctor.value,
)

/**
 * A free-text box rather than a dropdown.
 *
 * A shop accumulates hundreds of prescribers, and the one on the slip in
 * front of the operator is found by typing three letters of the name — not
 * by scrolling a list ordered by nothing in particular.
 */
const debouncedDoctor = useDebounced(typedDoctor, 250)

watch(debouncedDoctor, async (value) => {
  if (value.length < 2) {
    doctorMatches.value = []
    doctorSearched.value = ''
    return
  }
  // Choosing a doctor writes the name into the box; do not search for it
  // again and reopen the list underneath.
  if (doctorAttached.value) {
    doctorMatches.value = []
    doctorSearched.value = value
    return
  }
  try {
    doctorMatches.value = (
      await listDoctors({ q: value, page_size: 8, is_active: true })
    ).items
  } catch {
    doctorMatches.value = []
  } finally {
    doctorSearched.value = value
  }
})

/** Nobody on file under that name — offer to add them. */
const doctorNoMatch = computed(
  () =>
    !doctorAttached.value &&
    doctorSearched.value === typedDoctor.value &&
    typedDoctor.value.length >= 2 &&
    !doctorMatches.value.length,
)

async function selectDoctor(doctor) {
  selectedDoctor.value = doctor
  doctorQuery.value = doctor.name
  doctorMatches.value = []
  doctorNotice.value = doctor.hospital_clinic || doctor.specialization || null
  await cart.setDoctor(doctor.id)
}

/** Add a prescriber with nothing but a name; the rest can be filled in
 *  later under Masters, and the counter cannot wait for it. */
async function addDoctor() {
  const name = typedDoctor.value
  if (name.length < 2) return

  savingDoctor.value = true
  doctorNotice.value = null
  try {
    await selectDoctor(await createDoctor({ name }))
    doctorNotice.value = 'Added to the doctor list.'
  } catch (err) {
    if (err.code === 'DOCTOR_EXISTS') {
      // Someone added them a moment ago, or on another till.
      const again = await listDoctors({ q: name, page_size: 5, is_active: true })
      if (again.items.length) {
        await selectDoctor(again.items[0])
        doctorNotice.value = 'Already on file — selected.'
        return
      }
    }
    doctorNotice.value = err.message
  } finally {
    savingDoctor.value = false
  }
}

async function clearDoctor() {
  doctorQuery.value = ''
  doctorMatches.value = []
  doctorSearched.value = ''
  doctorNotice.value = null
  selectedDoctor.value = null
  await cart.setDoctor(null)
}

const needsDoctor = computed(() => cart.quote?.requires_doctor || false)

// --- payment --------------------------------------------------------------
const payOpen = ref(false)
const payments = ref([{ mode: 'CASH', amount: '', reference_no: '' }])
const saving = ref(false)
const payError = ref(null)
const idempotencyKey = ref(null)

const paid = computed(() =>
  payments.value.reduce((sum, p) => sum + Number(p.amount || 0), 0),
)
const outstanding = computed(() =>
  Math.round((Number(cart.payable) - paid.value + Number.EPSILON) * 100) / 100,
)

/** Nothing to collect: the customer's own credit covered the whole bill. */
const settledByCredit = computed(
  () =>
    payments.value.length > 0 &&
    Math.abs(outstanding.value) <= 0.001 &&
    payments.value.every((p) => p.credit_note_id || Number(p.amount) <= 0),
)

// --- credit notes the customer can spend ----------------------------------
const notesOnFile = ref([])

/**
 * Fetched when a customer is attached, because a note is customer-scoped:
 * a walk-in has none to spend. Offering them at the till is the whole
 * point of issuing them.
 */
async function loadCreditNotes() {
  const id = cart.customer?.id
  if (!id) {
    notesOnFile.value = []
    return
  }
  try {
    notesOnFile.value = (
      await listCreditNotes({ customer_id: id, open_only: true, page_size: 10 })
    ).items
  } catch {
    notesOnFile.value = []
  }
}

watch(() => cart.customer?.id, loadCreditNotes, { immediate: true })

const creditAvailable = computed(() =>
  notesOnFile.value.reduce((sum, n) => sum + Number(n.balance_amount), 0),
)

const round2 = (n) => Math.round((n + Number.EPSILON) * 100) / 100

/** What the customer's credit will actually cover on this invoice. */
const creditApplied = computed(() =>
  round2(Math.min(creditAvailable.value, Number(cart.payable || 0))),
)

/** What is left for them to hand over after the credit is used. */
const dueAfterCredit = computed(() =>
  round2(Math.max(0, Number(cart.payable || 0) - creditApplied.value)),
)

/**
 * Spend the customer's notes against this invoice, nearest expiry first so
 * the ones about to lapse are used before they do.
 *
 * A note that does not cover the whole bill leaves the rest to pay; a note
 * bigger than the bill is only drawn down by what is owed, and the balance
 * stays on the customer's account for next time.
 */
function creditLines(payable) {
  const lines = []
  let remaining = round2(payable)
  for (const note of notesOnFile.value) {
    if (remaining <= 0) break
    const use = round2(Math.min(Number(note.balance_amount), remaining))
    if (use <= 0) continue
    lines.push({
      mode: 'CREDIT_NOTE',
      amount: use.toFixed(2),
      reference_no: '',
      credit_note_id: note.id,
      credit_note_no: note.credit_note_no,
    })
    remaining = round2(remaining - use)
  }
  return { lines, remaining }
}

/** Put a note back after the operator has removed its line. */
function applyCreditNote(note) {
  const usable = round2(Math.min(Number(note.balance_amount), Math.max(0, outstanding.value)))
  if (usable <= 0) return

  const existing = payments.value.find((p) => p.credit_note_id === note.id)
  if (existing) {
    existing.amount = round2(Number(existing.amount || 0) + usable).toFixed(2)
  } else {
    payments.value.push({
      mode: 'CREDIT_NOTE',
      amount: usable.toFixed(2),
      reference_no: '',
      credit_note_id: note.id,
      credit_note_no: note.credit_note_no,
    })
  }
  const cash = payments.value.find((p) => p.mode === 'CASH')
  if (cash) {
    cash.amount = Math.max(0, round2(Number(cash.amount || 0) - usable)).toFixed(2)
  }
}

function removePayment(index) {
  payments.value.splice(index, 1)
}

function openPayment() {
  // F9 can reach this even when the button is disabled, so say why rather
  // than appear to do nothing.
  if (cart.isEmpty) {
    cart.notice = 'The cart is empty.'
    return
  }
  if (cart.error) {
    cart.notice = 'Fix the problem above before taking payment.'
    return
  }
  if (!cart.quote) {
    cart.notice = 'Still pricing the cart — try again in a moment.'
    return
  }
  payError.value = null
  // One key per attempt at this cart: retrying the same payment is safe,
  // starting a fresh sale is not confused with it.
  idempotencyKey.value = `${auth.user?.id || 0}-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`

  // Credit on the customer's account comes off the bill automatically —
  // that is the point of having issued it. Whatever is left goes on cash,
  // and if the credit covers everything there is nothing to collect.
  const { lines, remaining } = creditLines(Number(cart.payable))
  if (remaining > 0 || !lines.length) {
    lines.push({ mode: 'CASH', amount: remaining.toFixed(2), reference_no: '' })
  }
  payments.value = lines
  payOpen.value = true
}

function addPaymentRow() {
  payments.value.push({
    mode: 'UPI',
    amount: outstanding.value > 0 ? outstanding.value.toFixed(2) : '',
    reference_no: '',
  })
}

async function confirmSale() {
  payError.value = null
  if (Math.abs(outstanding.value) > 0.001) {
    payError.value = `Payments must add up to ${money(cart.payable)}.`
    return
  }

  saving.value = true
  try {
    const sale = await createSale(
      cart.salePayload(
        payments.value
          .filter((p) => Number(p.amount) > 0)
          .map((p) => ({
            mode: p.mode,
            amount: String(p.amount),
            reference_no: p.reference_no || null,
            credit_note_id: p.credit_note_id || null,
          })),
        idempotencyKey.value,
      ),
    )
    cart.clear()
    notesOnFile.value = []
    payOpen.value = false
    router.push({ name: 'invoice', params: { id: sale.id } })
  } catch (err) {
    payError.value = err.message
  } finally {
    saving.value = false
  }
}

// --- keyboard -------------------------------------------------------------
function onKey(event) {
  if (event.key === 'F2') {
    event.preventDefault()
    focusSearch()
  } else if (event.key === 'F4') {
    event.preventDefault()
    nextTick(() => customerBox.value?.focus())
  } else if (event.key === 'F9') {
    event.preventDefault()
    openPayment()
  } else if (event.key === 'Escape') {
    if (payOpen.value) payOpen.value = false
    else if (customerMatches.value.length) customerMatches.value = []
    else if (batchPickerFor.value !== null) batchPickerFor.value = null
    else {
      query.value = ''
      results.value = []
    }
  }
}

onMounted(() => {
  cart.restore()
  cart.refresh()
  window.addEventListener('keydown', onKey)
  focusSearch()
})

onUnmounted(() => window.removeEventListener('keydown', onKey))
</script>

<template>
  <section class="pos stack">
    <!-- search, alternatives and cart run the full width -->
    <div class="card stack">
      <div class="search-row">
        <input
          ref="searchBox"
          v-model="query"
          class="search"
          type="search"
          placeholder="Search a brand or a salt…  (F2)"
          @keydown.down.prevent="moveHighlight(1)"
          @keydown.up.prevent="moveHighlight(-1)"
          @keydown.enter.prevent="addHighlighted"
        />
        <span class="muted small">{{ searching ? 'Searching…' : 'F2 search · F4 customer · F9 pay · Esc close' }}</span>
      </div>

      <p v-if="results.length" class="muted small">
        In stock first, then a same-salt equivalent that is in stock, then what has run out.
      </p>

      <div v-if="results.length" class="table-wrap results">
        <table class="data">
          <thead>
            <tr>
              <th>Brand</th>
              <th>Pack</th>
              <th class="numeric">MRP</th>
              <th class="numeric">Available</th>
              <th>Expiry</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            <tr
              v-for="(m, index) in results"
              :key="m.id"
              :class="{
                'row--selected': index === highlighted,
                'row--nostock': !inStock(m),
              }"
              @mouseenter="highlighted = index; alt.hover(m, $event)"
              @mousemove="alt.hover(m, $event)"
              @mouseleave="alt.leave()"
              @click="addToCart(m)"
            >
              <td>
                <strong>{{ m.brand_name }}</strong>
                <!-- On the list because it shares a salt with what was
                     typed, not because its own name matched. -->
                <span v-if="m.is_alternative" class="badge badge--ok">same salt</span>
                <p class="composition">
                  <template v-if="m.composition_text">{{ m.composition_text }} · </template>
                  <span v-else class="badge">General</span>
                  {{ m.manufacturer_name }}
                </p>
              </td>
              <td>{{ m.pack_label }}</td>
              <td class="numeric">{{ money(m.mrp_per_pack) }}</td>
              <td class="numeric">
                <template v-if="inStock(m)">{{ trim(m.available_units) }}</template>
                <span v-else class="badge badge--danger">No stock</span>
              </td>
              <td>
                {{ m.nearest_expiry || '—' }}
                <span v-if="m.has_near_expiry" class="badge badge--warn">Soon</span>
              </td>
              <td class="numeric">
                <button
                  class="btn btn--sm"
                  :disabled="!inStock(m)"
                  :title="inStock(m) ? 'Add one pack' : 'Nothing sellable in stock'"
                >
                  Add
                </button>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>


    <!-- cart -->
    <div class="card stack">
      <h2>Cart</h2>
      <p v-if="cart.error" class="alert alert--error">{{ cart.error }}</p>
      <p v-if="cart.notice" class="alert alert--warn">{{ cart.notice }}</p>
      <p v-for="(w, i) in cart.warnings" :key="i" class="alert alert--warn">{{ w }}</p>
      <p v-if="cart.isEmpty" class="muted">Nothing in the cart. Search above to begin.</p>

      <!-- No horizontal scroller here: the cart is the one table an
           operator reads while a customer waits, so it wraps to fit
           rather than hiding columns off to the side. -->
      <div v-else>
        <table class="data cart-table">
          <thead>
            <tr>
              <th>Item</th>
              <th class="numeric">Qty (units)</th>
              <th class="numeric">MRP / pack</th>
              <th class="numeric">MRP / unit</th>
              <th class="numeric">Disc %</th>
              <th class="numeric">Amount</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            <template v-for="(line, index) in cart.lines" :key="index">
              <tr>
                <td class="cell--item">
                  <strong>{{ line.medicine.brand_name }}</strong>
                  <!-- every batch this line draws from, on one line -->
                  <p v-if="batchSummary(index)" class="batches">
                    {{ batchSummary(index) }}
                    <span v-if="hasNearExpiry(index)" class="badge badge--warn">Near expiry</span>
                  </p>
                </td>
                <td class="numeric">
                  <div class="qty-box">
                    <button
                      class="btn btn--sm"
                      :title="`One pack fewer (${line.medicine.units_per_pack} unit(s))`"
                      :disabled="Number(line.qty_units) <= line.medicine.units_per_pack"
                      @click="stepPacks(index, -1)"
                    >
                      −
                    </button>
                    <input
                      class="qty"
                      type="number"
                      min="1"
                      step="1"
                      inputmode="numeric"
                      :value="line.qty_units"
                      @keydown="blockDecimal"
                      @change="setQty(index, $event.target.value)"
                    />
                    <button
                      class="btn btn--sm"
                      :title="`One pack more (${line.medicine.units_per_pack} unit(s))`"
                      @click="stepPacks(index, 1)"
                    >
                      +
                    </button>
                  </div>
                  <!-- how the pack is made up, where the quantity is -->
                  <p class="pack-note" :title="qtyNoteLong(index)">{{ qtyNote(index) }}</p>
                </td>
                <td class="numeric">{{ money(quotedFor(index)[0]?.mrp_per_pack) }}</td>
                <td class="numeric">{{ money(quotedFor(index)[0]?.unit_mrp) }}</td>
                <td class="numeric">
                  <input
                    class="disc"
                    type="number"
                    min="0"
                    max="100"
                    step="0.01"
                    :value="line.discount_pct ?? quotedFor(index)[0]?.discount_pct ?? ''"
                    @change="setDiscount(index, $event.target.value)"
                  />
                  <p v-if="quotedFor(index)[0]" class="composition">
                    max {{ quotedFor(index)[0].max_discount_pct }}%
                  </p>
                </td>
                <td class="numeric">
                  {{ money(quotedFor(index).reduce((s, q) => s + Number(q.net_amount), 0)) }}
                </td>
                <td class="numeric actions">
                  <button class="btn btn--sm" @click="openBatchPicker(index)">Batch</button>
                  <button class="btn btn--sm btn--danger" @click="cart.remove(index)">×</button>
                </td>
              </tr>

              <tr v-if="batchPickerFor === index">
                <td colspan="7">
                  <p class="muted small">
                    Leave unpicked to sell first-expiry-first. Expired and blocked batches
                    are never offered.
                  </p>
                  <div class="batch-options">
                    <button class="btn btn--sm" @click="chooseBatch(index, null)">
                      FEFO (automatic)
                    </button>
                    <button
                      v-for="b in batchOptions"
                      :key="b.id"
                      class="btn btn--sm"
                      @click="chooseBatch(index, b.id)"
                    >
                      {{ b.batch_no }} ·
                      {{ b.has_expiry === false ? 'no expiry' : `exp ${b.expiry_date}` }} ·
                      {{ trim(b.qty_available_units) }} left · {{ money(b.mrp_per_pack) }}
                    </button>
                  </div>
                </td>
              </tr>
            </template>
          </tbody>
        </table>
      </div>
    </div>

    <!-- Customer with the doctor beneath it, and the total alongside both:
         the payable stays level with the customer it belongs to. -->
    <div class="pos__foot">
      <div class="stack">
        <div class="card stack">
          <div class="card__head">
            <h3>Customer</h3>
            <!-- A status, not the values: the number and name are in the
                 boxes below and do not need saying twice. -->
            <span v-if="attached" class="badge badge--ok">
              {{ cart.customer.id ? 'On file' : 'New' }}
            </span>
            <span v-else class="muted small">Walk-in</span>
          </div>

          <!-- Mobile and name on one line: they are entered together, and
               the number is looked up as it is typed. -->
          <div class="cust-row">
            <label class="field">
              <span>Mobile (F4)</span>
              <input
                ref="customerBox"
                v-model="mobile"
                inputmode="numeric"
                maxlength="15"
                placeholder="Start typing…"
                @keydown.enter.prevent="attachCustomer"
              />
            </label>
            <label class="field">
              <span>Name</span>
              <input
                v-model="customerName"
                placeholder="Optional"
                @keydown.enter.prevent="attachCustomer"
              />
            </label>
          </div>

          <!-- Anyone already on file whose number starts this way. -->
          <ul v-if="customerMatches.length" class="matches matches--compact">
            <li v-for="c in customerMatches" :key="c.id">
              <button type="button" @click="selectCustomer(c)">
                <strong>{{ c.mobile }}</strong>
                <span class="muted small">
                  {{ c.name || 'No name on file' }}
                  <template v-if="Number(c.credit_balance) > 0">
                    · {{ money(c.credit_balance) }} credit
                  </template>
                </span>
              </button>
            </li>
          </ul>

          <p v-else-if="noMatch" class="muted small">
            Nobody on file with that number.
          </p>

          <p v-if="customerNotice" class="muted small">{{ customerNotice }}</p>

          <!-- Once someone is attached the action is spent: it greys out and
               Clear is the way back to a walk-in sale. -->
          <div class="form-actions">
            <button
              class="btn btn--sm btn--primary"
              :disabled="attached || !typedMobile"
              @click="attachCustomer"
            >
              {{ actionLabel }}
            </button>
            <button v-if="attached || typedMobile" class="btn btn--sm" @click="clearCustomer">
              Clear
            </button>
          </div>
        </div>

        <div class="card stack">
          <div class="card__head">
            <h3>Doctor</h3>
            <span v-if="doctorAttached" class="badge badge--ok">Recorded</span>
            <span v-else class="muted small">Self</span>
          </div>

          <p v-if="needsDoctor && !cart.doctorId" class="alert alert--warn">
            A Schedule H / H1 / X medicine is in the cart, so the prescriber has to be named
            before it can be billed.
          </p>

          <!-- Typed, not picked from a list: a shop accumulates hundreds of
               prescribers and the one on the slip is found by name. -->
          <label class="field">
            <span>Prescribed by</span>
            <input
              v-model="doctorQuery"
              placeholder="Type a name — leave blank for self"
              @keydown.enter.prevent="doctorNoMatch ? addDoctor() : null"
            />
          </label>

          <ul v-if="doctorMatches.length" class="matches matches--compact">
            <li v-for="d in doctorMatches" :key="d.id">
              <button type="button" @click="selectDoctor(d)">
                <strong>{{ d.name }}</strong>
                <span v-if="d.hospital_clinic || d.specialization" class="muted small">
                  {{ [d.specialization, d.hospital_clinic].filter(Boolean).join(' · ') }}
                </span>
              </button>
            </li>
          </ul>

          <p v-else-if="doctorNoMatch" class="muted small">
            Not on file — adding needs only the name.
          </p>

          <p v-if="doctorNotice" class="muted small">{{ doctorNotice }}</p>

          <div class="form-actions">
            <button
              class="btn btn--sm btn--primary"
              :disabled="doctorAttached || !doctorNoMatch || savingDoctor"
              @click="addDoctor"
            >
              {{ doctorAttached ? 'Recorded' : savingDoctor ? 'Adding…' : 'Add doctor' }}
            </button>
            <button v-if="doctorAttached || typedDoctor" class="btn btn--sm" @click="clearDoctor">
              Clear
            </button>
          </div>
        </div>
      </div>

      <div class="card stack totals-card">
        <h3>Total</h3>
        <div v-if="cart.totals" class="totals">
          <div><span>Gross</span><strong>{{ money(cart.totals.gross_amount) }}</strong></div>
          <div><span>Discount</span><strong>−{{ money(cart.totals.discount_amount) }}</strong></div>
          <div><span>Taxable</span><strong>{{ money(cart.totals.taxable_amount) }}</strong></div>
          <div v-if="cart.quote?.is_intra_state">
            <span>CGST</span><strong>{{ money(cart.totals.cgst_amount) }}</strong>
          </div>
          <div v-if="cart.quote?.is_intra_state">
            <span>SGST</span><strong>{{ money(cart.totals.sgst_amount) }}</strong>
          </div>
          <div v-else><span>IGST</span><strong>{{ money(cart.totals.igst_amount) }}</strong></div>
          <div><span>Round off</span><strong>{{ money(cart.totals.round_off) }}</strong></div>
          <div class="totals__grand">
            <!-- With credit in play, "payable" belongs to the figure below:
                 only one number on screen should answer "how much now?" -->
            <span>{{ creditApplied ? 'Invoice total' : 'Payable' }}</span>
            <strong>{{ money(cart.totals.net_amount) }}</strong>
          </div>

          <!-- Credit on account is applied at payment; showing it here means
               the operator can tell the customer before they reach for a
               wallet. -->
          <template v-if="creditAvailable > 0">
            <div class="totals__credit">
              <span>Credit on account</span>
              <strong>−{{ money(creditApplied) }}</strong>
            </div>
            <div class="totals__due">
              <span>{{ dueAfterCredit > 0 ? 'To collect' : 'Nothing to collect' }}</span>
              <strong>{{ money(dueAfterCredit) }}</strong>
            </div>
            <p class="muted small credit-note-line">
              {{ money(creditAvailable) }} on account
              <template v-if="creditAvailable > creditApplied">
                · {{ money(creditAvailable - creditApplied) }} stays for next time
              </template>
            </p>
          </template>
        </div>
        <p v-else class="muted">Add an item to see the total.</p>

        <p v-if="cart.quote" class="muted small">{{ cart.quote.amount_in_words }}</p>

        <label v-if="auth.isAdmin" class="field">
          <span>Discount override reason</span>
          <input
            v-model="cart.overrideReason"
            placeholder="Needed only above the batch limit"
            @change="cart.refresh()"
          />
        </label>

        <div class="pay-actions">
          <button
            class="btn btn--primary btn--lg pay-btn"
            :disabled="cart.isEmpty || Boolean(cart.error) || cart.quoting"
            @click="openPayment"
          >
            Take payment · F9
          </button>
          <button class="btn" :disabled="cart.isEmpty" @click="cart.clear()">Clear</button>
        </div>
      </div>
    </div>

    <!-- payment dialog -->
    <div v-if="payOpen" class="overlay" @click.self="payOpen = false">
      <div class="card stack dialog">
        <h2>Payment — {{ money(cart.payable) }}</h2>

        <div v-for="(p, index) in payments" :key="index" class="pay-row">
          <label class="field">
            <span>Mode</span>
            <select v-if="!p.credit_note_id" v-model="p.mode">
              <option value="CASH">Cash</option>
              <option value="CARD">Card</option>
              <option value="UPI">UPI</option>
              <option value="NET_BANKING">Net banking</option>
              <option value="OTHER">Other</option>
            </select>
            <input v-else :value="`Credit note ${p.credit_note_no}`" disabled />
          </label>
          <label class="field">
            <span>Amount</span>
            <input v-model="p.amount" type="number" step="0.01" min="0" />
          </label>
          <label class="field">
            <span>Reference</span>
            <input
              v-model="p.reference_no"
              :placeholder="p.credit_note_id ? 'From the customer’s account' : 'UTR, last 4 digits…'"
              :disabled="Boolean(p.credit_note_id)"
            />
          </label>
          <button
            class="btn btn--sm btn--danger pay-row__drop"
            :disabled="payments.length === 1"
            title="Remove this payment line"
            @click="removePayment(index)"
          >
            ×
          </button>
        </div>

        <!-- Anything the customer already has on account. -->
        <div v-if="notesOnFile.length" class="notes-panel stack">
          <p class="muted small">
            {{ money(creditAvailable) }} on account in {{ notesOnFile.length }} credit note(s) —
            applied automatically, nearest expiry first.
          </p>
          <div class="form-actions">
            <button
              v-for="note in notesOnFile"
              :key="note.id"
              class="btn btn--sm"
              :disabled="payments.some((p) => p.credit_note_id === note.id) || outstanding <= 0"
              :title="`Expires ${note.expires_on}`"
              @click="applyCreditNote(note)"
            >
              {{ note.credit_note_no }} · {{ money(note.balance_amount) }}
            </button>
          </div>
        </div>

        <div class="form-actions">
          <button class="btn btn--sm" @click="addPaymentRow">+ Split payment</button>
          <span :class="['muted', Math.abs(outstanding) > 0.001 ? 'unbalanced' : '']">
            {{ outstanding > 0 ? `${money(outstanding)} still due`
               : outstanding < 0 ? `${money(-outstanding)} over` : 'Balanced' }}
          </span>
        </div>

        <p v-if="settledByCredit" class="alert alert--ok">
          Settled in full from the customer's credit — nothing to collect.
        </p>
        <p v-if="payError" class="alert alert--error">{{ payError }}</p>

        <div class="form-actions">
          <button class="btn btn--primary" :disabled="saving" @click="confirmSale">
            {{ saving ? 'Saving…' : 'Confirm and print' }}
          </button>
          <button class="btn" @click="payOpen = false">Cancel</button>
        </div>
      </div>
    </div>
    <!-- Same-salt alternatives, after a second of resting on a row.
         Clicking one adds it instead — which is the substitution. -->
    <AlternativesPopover
      v-if="alt.open.value"
      :medicine="alt.medicine.value"
      :rows="alt.rows.value"
      :loading="alt.loading.value"
      :anchor="alt.anchor.value"
      action-label="Click a brand to add it to the cart instead."
      @hold="alt.hold()"
      @leave="alt.leave()"
      @pick="(m) => { alt.close(); addToCart(m) }"
    />
  </section>
</template>

<style scoped>
/* Under the full-width cart: customer with the doctor stacked beneath it on
   the left, the total beside them on the right. */
.pos__foot {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 22rem;
  gap: var(--sp-3);
  align-items: start;
}

/* Floating: pinned just under the top bar, so the payable and the pay
   button stay where the eye expects however far the page is scrolled. */
.totals-card {
  position: sticky;
  top: calc(var(--topbar-h) + var(--sp-3));
  border-color: var(--accent);
}

@media (max-width: 900px) {
  .pos__foot {
    grid-template-columns: 1fr;
  }

  /* Stacked, a pinned card would cover what it belongs to. */
  .totals-card {
    position: static;
  }
}

.search-row {
  display: flex;
  align-items: center;
  gap: 0.75rem;
}

.search {
  flex: 1;
  font-size: 1rem;
  padding: 0.4rem 0.7rem;
}

.results {
  max-height: 20rem;
  overflow-y: auto;
}

.composition {
  margin: 0.1rem 0 0;
  font-size: 0.8rem;
  color: var(--text-muted);
  white-space: normal;
}

/* The cart wraps rather than scrolling sideways. Fixed layout stops a long
   brand name from squeezing the numbers into a scroller. */
.cart-table {
  table-layout: fixed;
  width: 100%;
}

.cart-table th,
.cart-table td {
  white-space: normal;
  padding: 0.5rem 0.5rem;
  vertical-align: top;
}

/* The item carries the brand name and its batch line, both of which are
   read on every row, so it takes the largest share. */
.cart-table th:first-child,
.cart-table td:first-child {
  width: 32%;
}

/* Wide enough for the stepper and the abbreviated pack note beneath it. */
.cart-table th:nth-child(2),
.cart-table td:nth-child(2) {
  width: 20%;
}

/* The two MRPs, the discount and the amount are left to size themselves:
   they hold short right-aligned figures and share whatever the item and
   quantity columns do not take. */

.cart-table th:last-child,
.cart-table td:last-child {
  width: 6.5rem;
}

.cell--item strong {
  display: block;
  line-height: 1.25;
}

/* Every batch behind the line, on one line. */
.batches {
  margin: 0.15rem 0 0;
  font-size: 0.78rem;
  color: var(--text-muted);
  white-space: normal;
}

/* Grey, directly under the quantity box — the pack make-up belongs where
   the number is being typed, not next to the brand name. */
.pack-note {
  margin: 0.15rem 0 0;
  font-size: 0.78rem;
  color: var(--text-muted);
  white-space: normal;
}

.qty,
.disc {
  width: 100%;
  max-width: 4rem;
  font: inherit;
  text-align: right;
  padding: 0.25rem 0.35rem;
  min-height: 1.7rem;
}

.qty-box {
  display: inline-flex;
  align-items: center;
  gap: 0.2rem;
}

/* Still listed — the operator needs to know the shop carries the brand —
   but plainly not sellable. */
.row--nostock td {
  opacity: 0.55;
}

.row--nostock {
  cursor: not-allowed;
}

.batch-options {
  display: flex;
  flex-wrap: wrap;
  gap: 0.4rem;
}

.chosen {
  margin: 0;
}

.card__head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--sp-2);
  flex-wrap: wrap;
}

.card__head h3 {
  margin: 0;
}

/* Mobile and name are entered together, so they sit together. The mobile
   gets the wider half — it is the field that identifies the customer. */
.cust-row {
  display: grid;
  grid-template-columns: 1.1fr 1fr;
  gap: var(--sp-2);
}

.matches--compact {
  max-height: 11rem;
}

.matches--compact button {
  padding: 0.3rem 0.55rem;
  gap: 0.1rem;
}

.totals > div {
  display: flex;
  justify-content: space-between;
  gap: var(--sp-3);
  padding: 0.08rem 0;
  font-size: 0.85rem;
  color: var(--text-muted);
}

.totals > div strong {
  color: var(--text);
  font-variant-numeric: tabular-nums;
}

/* The payable is the one number the customer is told out loud, so it is
   the one thing on the screen allowed to shout. */
.totals__grand {
  border-top: 1px solid var(--border-strong);
  margin-top: var(--sp-2);
  padding-top: var(--sp-2) !important;
  font-size: 1rem !important;
  color: var(--text) !important;
  font-weight: 600;
}

.totals__grand strong {
  font-size: 1.5rem;
  line-height: 1.1;
  color: var(--accent);
}

.pay-actions {
  display: flex;
  gap: var(--sp-2);
  align-items: stretch;
}

.pay-btn {
  flex: 1;
}

.actions {
  white-space: nowrap;
}

.actions button + button {
  margin-left: 0.3rem;
}

tbody tr {
  cursor: pointer;
}

.row--selected {
  background: var(--accent-soft) !important;
}

.overlay {
  position: fixed;
  inset: 0;
  background: rgba(15, 23, 42, 0.45);
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 1rem;
  z-index: 50;
}

.dialog {
  width: min(38rem, 100%);
  max-height: 90vh;
  overflow-y: auto;
}

/* A credit line reads differently from a cash one, so it looks different. */
.pay-row {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)) 2rem;
  gap: var(--sp-2);
  align-items: end;
}

.pay-row__drop {
  min-width: 2rem;
}

.totals__credit strong {
  color: var(--ok);
}

.totals__due {
  border-top: 1px dashed var(--border-strong);
  margin-top: var(--sp-1);
  padding-top: var(--sp-1) !important;
  font-size: 0.95rem !important;
  color: var(--text) !important;
  font-weight: 600;
}

.totals__due strong {
  font-size: 1.2rem;
  color: var(--accent);
}

.credit-note-line {
  margin: var(--sp-1) 0 0;
}

.notes-panel {
  border: 1px solid var(--accent);
  background: var(--accent-soft);
  border-radius: var(--radius-sm);
  padding: var(--sp-2);
}

.unbalanced {
  color: var(--danger, #b91c1c);
  font-weight: 600;
}

.small {
  font-size: 0.82rem;
}
</style>
