<script setup>
/**
 * The printed bill.
 *
 * The server sends JSON and this renders it — no HTML ever comes from the
 * API. Four layouts share one payload: the shop's A4 letterhead (the
 * default), 80 mm thermal for a counter roll, A5, and A5_new.
 *
 * Prints are counted, but only when paper actually moves: opening this screen
 * is a read. The count is recorded when the operator clicks Print, so the
 * figure in the register is a count of sheets rather than of glances.
 */
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { fetchInvoice, recordPrint } from '@/api/pos'

const route = useRoute()
const router = useRouter()

const invoice = ref(null)
const loading = ref(true)
const error = ref(null)
const layout = ref('A4')

const rupees = new Intl.NumberFormat('en-IN', {
  style: 'currency',
  currency: 'INR',
  minimumFractionDigits: 2,
})
const money = (v) => (v === null || v === undefined ? '—' : rupees.format(Number(v)))
const trim = (v) => Number(v).toLocaleString('en-IN', { maximumFractionDigits: 3 })

const sale = computed(() => invoice.value?.sale)
const store = computed(() => invoice.value?.store)

const invoiceLabel = computed(() => {
  if (!sale.value) return ''
  const prefix = invoice.value.invoice_prefix || ''
  return `${prefix}${sale.value.invoice_no}`
})

const billedAt = computed(() => {
  if (!sale.value) return ''
  // The DB is UTC; a counter in India reads IST.
  return new Date(sale.value.invoice_datetime).toLocaleString('en-IN', {
    timeZone: 'Asia/Kolkata',
    dateStyle: 'medium',
    timeStyle: 'short',
  })
})

/** dd/mm/yyyy — the form the letterhead asks for. */
const billDate = computed(() => {
  if (!sale.value) return ''
  return new Date(sale.value.invoice_datetime).toLocaleDateString('en-GB', {
    timeZone: 'Asia/Kolkata',
    day: '2-digit',
    month: '2-digit',
    year: 'numeric',
  })
})

/** "G2, 4/44-150, Road no 1, …, Telangana-500089" on one line. */
const addressLine = computed(() => {
  const s = store.value
  if (!s) return ''
  const tail = [s.state, s.pincode].filter(Boolean).join('-')
  return [s.address, s.city, tail].filter(Boolean).join(', ')
})

/**
 * The whole-bill discount as a percentage of the gross.
 *
 * Lines can each carry their own discount, so this is what was actually
 * given rather than a rate anyone typed. The rupees are printed beside it —
 * a percentage on its own is not a figure you can check.
 */
const discountPct = computed(() => {
  const gross = Number(sale.value?.gross_amount || 0)
  const off = Number(sale.value?.discount_amount || 0)
  if (!gross || !off) return null
  return ((off / gross) * 100).toFixed(2)
})

/**
 * Credit notes spent on this bill.
 *
 * A note is a payment, not a discount: the invoice value and the tax on it
 * are unchanged, and what moves is how much the customer hands over. So the
 * bill shows the invoice total, the credit taken off it, and the balance —
 * rather than quietly printing a smaller total nobody can reconcile.
 */
const creditPayments = computed(
  () => sale.value?.payments?.filter((p) => p.mode === 'CREDIT_NOTE') || [],
)

const creditApplied = computed(() =>
  creditPayments.value.reduce((sum, p) => sum + Number(p.amount || 0), 0),
)

const payableAfterCredit = computed(() =>
  Math.max(0, Number(sale.value?.net_amount || 0) - creditApplied.value),
)

/** What is left on the notes after this sale, for the customer's records. */
const creditRemaining = computed(() =>
  creditPayments.value.reduce((sum, p) => sum + Number(p.credit_note_balance || 0), 0),
)

/**
 * "2028-03" for a line that expires, blank for one that does not.
 *
 * A comb has no shelf life; the server stores a sentinel date so that FEFO
 * and the expiry alerts keep working off one column, and says so with
 * `has_expiry`. Printing the sentinel would put the year 9999 on a customer's
 * bill.
 */
const expiryShort = (item) => (item.has_expiry === false ? '' : item.expiry_date?.slice(0, 7))

/**
 * HSN and batch mean something for a medicine — a drug's tax
 * classification, a traceable lot. A bar of soap has neither on the
 * customer's mind, so a general item's bill line leaves both blank
 * regardless of what happens to be stored against it.
 */
const isMedicineLine = (item) => item.item_type !== 'GENERAL'
const hsnShort = (item) => (isMedicineLine(item) ? item.hsn_code || '' : '')
const batchShort = (item) => (isMedicineLine(item) ? item.batch_no : '')

/* Anything whose pack is a countable strip or box, however the label was
   typed. Generated labels read "15 tab strip" / "12 pc pack", but the field
   is free text, so the number is checked as well as the wording. */
const LOOSE_PACK = /\b(tabs?|tablets?|caps?|capsules?|pcs?|pieces?)\b/i
const PACK_CONTAINER = /\s*\b(strip|strips|bottle|pack|box|jar|tube|sachet)\b\s*$/i
const MEASURE = /^(\d+(?:\.\d+)?)\s+(ml|l|g|gm|gms|mg|kg|iu)$/i

/**
 * "100ml", "10g" — the pack, printed only where it tells the customer
 * something.
 *
 * A strip of tablets is sold loose: the Qty column counts tablets, so
 * "15 tab strip" beside the name only raises the question of whether fifteen
 * were bought. Where the pack *is* the sellable unit — `units_per_pack` of 1,
 * a 100 ml bottle or a 10 g tube — the size is the only thing saying which
 * bottle it was, so it stays, shortened to sit inline with the name.
 */
const packShort = (item) => {
  const label = (item.pack_label || '').trim()
  if (!label || Number(item.units_per_pack) > 1 || LOOSE_PACK.test(label)) return ''
  return label.replace(PACK_CONTAINER, '').trim().replace(MEASURE, '$1$2')
}

/**
 * The Amount column, added up.
 *
 * Summed from the lines rather than taken from `net_amount`, which carries
 * the round-off: a column footer that does not equal its column is a bug on
 * a bill. The round-off is shown on its own line below instead.
 */
const itemsTotal = computed(() =>
  (sale.value?.items || []).reduce((sum, i) => sum + Number(i.net_amount || 0), 0),
)

/**
 * The GST one line carried, in rupees.
 *
 * A basket can mix slabs now that the shop sells general goods beside
 * medicines — 12 % on a strip of tablets, 18 % on a bar of soap. A single
 * tax total at the foot of the bill cannot be checked against either, so
 * every layout prints the amount per line as well as the rate. Intra-state
 * splits into CGST + SGST and inter-state is IGST; summing all three is
 * correct either way, because the ones that do not apply are zero.
 */
const lineTax = (item) =>
  Number(item.cgst_amount || 0) + Number(item.sgst_amount || 0) + Number(item.igst_amount || 0)

const totalTax = computed(() =>
  !sale.value
    ? 0
    : Number(sale.value.cgst_amount || 0) +
      Number(sale.value.sgst_amount || 0) +
      Number(sale.value.igst_amount || 0),
)

/** Only the tax the invoice actually carries: intra-state splits, inter-state does not. */
const taxRows = computed(() => {
  if (!invoice.value?.gst_enabled || !sale.value) return []
  return sale.value.is_intra_state
    ? [
        ['SGST', sale.value.sgst_amount],
        ['CGST', sale.value.cgst_amount],
      ]
    : [['IGST', sale.value.igst_amount]]
})

const printCount = ref(0)
const printing = ref(false)

/**
 * Record the print, then open the dialog. Recording first means a print is
 * never lost to a closed tab; a failure to record is not a reason to stop
 * the counter printing a customer's bill, so it only sets the notice.
 */
async function printNow() {
  printing.value = true
  try {
    const record = await recordPrint(route.params.id)
    printCount.value = record.reprint_count
  } catch (err) {
    error.value = `The print was not recorded (${err.message}).`
  } finally {
    printing.value = false
  }
  window.print()
}

async function load() {
  loading.value = true
  error.value = null
  try {
    invoice.value = await fetchInvoice(route.params.id)
    printCount.value = invoice.value.reprint_count
    layout.value = invoice.value.print_format || 'A4'
  } catch (err) {
    error.value = err.message
  } finally {
    loading.value = false
  }
}

/**
 * The paper the print dialog offers, per layout.
 *
 * `@page { size }` is a document rule: it cannot be scoped to a class and it
 * will not read a CSS variable, so the one rule that matters is rewritten as
 * the layout changes rather than written four times in the stylesheet.
 *
 * Only A5_new names its paper. The other three have always printed on
 * whatever the dialog was set to, and the bills the shop runs today are not
 * the place to find out that assumption was load-bearing.
 *
 * 180 × 210 mm is not a standard size, so it is given as two explicit lengths
 * rather than a paper name. The printer has to be set to a matching custom
 * size, or the driver will fit this to whatever is loaded and the millimetres
 * here stop meaning millimetres on paper.
 */
const PAGE_SIZE = { A5_NEW: '180mm 210mm' }

let pageStyle = null

function applyPageSize(value) {
  if (!pageStyle) {
    pageStyle = document.createElement('style')
    document.head.appendChild(pageStyle)
  }
  pageStyle.textContent = `@page { size: ${PAGE_SIZE[value] || 'auto'}; margin: 0; }`
}

watch(layout, applyPageSize, { immediate: true })

onMounted(load)
onUnmounted(() => {
  // The rule is on `document`, so it would outlive this screen and set the
  // paper for whatever printed next.
  pageStyle?.remove()
  pageStyle = null
})
watch(() => route.params.id, load)
</script>

<template>
  <section class="stack">
    <div class="card stack no-print">
      <div class="bar">
        <h1>Invoice {{ invoiceLabel }}</h1>
        <div class="form-actions">
          <label class="inline">
            <span class="muted small">Layout</span>
            <select v-model="layout">
              <option value="A4">A4 letterhead</option>
              <option value="THERMAL_80">80 mm thermal</option>
              <option value="A5">A5</option>
              <option value="A5_NEW">A5_new</option>
            </select>
          </label>
          <button class="btn btn--primary" :disabled="printing || loading" @click="printNow">
            {{ printing ? 'Recording…' : 'Print' }}
          </button>
          <button class="btn" @click="router.push({ name: 'pos' })">New sale</button>
          <button class="btn" @click="router.push({ name: 'sales' })">Register</button>
        </div>
      </div>
      <!-- On screen only (`no-print`): the counter should know how many
           copies exist, without the customer's bill saying so. -->
      <p v-if="printCount" class="muted small">
        Printed {{ printCount }} time(s). Each print is recorded against the invoice.
      </p>
      <p v-else class="muted small">Not printed yet.</p>
    </div>

    <p v-if="error" class="alert alert--error">{{ error }}</p>
    <p v-else-if="loading" class="muted">Loading…</p>

    <!-- ================= A4 letterhead — the shop's own bill ============ -->
    <div v-else-if="invoice && layout === 'A4'" class="sheet">
      <header class="sheet__head">
        <img v-if="store.logo_image" class="sheet__logo" :src="store.logo_image" alt="" />
        <div v-else class="sheet__logo" />

        <div class="sheet__title">
          <h2>{{ store.name }}</h2>
          <p v-if="addressLine">{{ addressLine }}</p>
          <p class="sheet__ids">
            <span v-if="store.drug_licence_no_20b">
              <strong>Licence no:</strong> {{ store.drug_licence_no_20b
              }}<template v-if="store.drug_licence_no_21b">
                / {{ store.drug_licence_no_21b }}</template>
            </span>
            <span v-if="store.gstin && invoice.gst_enabled">
              <strong>GST no. {{ store.gstin }}</strong>
            </span>
          </p>
        </div>

        <img
          v-if="store.partner_logo_image"
          class="sheet__logo"
          :src="store.partner_logo_image"
          alt=""
        />
        <div v-else class="sheet__logo" />
      </header>

      <p class="sheet__kind">INVOICE</p>

      <div class="sheet__meta">
        <div><span>Name:</span> {{ sale.customer_name || '' }}</div>
        <div><span>Bill no:</span> {{ invoiceLabel }}</div>
        <div><span>Doctor:</span> {{ sale.doctor_name || '' }}</div>
        <div><span>Mobile no:</span> {{ sale.customer_mobile || '' }}</div>
        <div><span>Bill Date:</span> {{ billDate }}</div>
        <div>
          <template v-if="sale.customer_gstin">
            <span>GSTIN:</span> {{ sale.customer_gstin }}
          </template>
        </div>
      </div>

      <table class="sheet__items">
        <thead>
          <tr>
            <th class="w-no">#</th>
            <th>Product</th>
            <th class="w-hsn">HSN no.</th>
            <th class="w-batch">Batch</th>
            <th class="w-qty numeric">Qty</th>
            <th class="w-mrp numeric">MRP</th>
            <th class="w-gst numeric">GST %</th>
            <th v-if="invoice.gst_enabled" class="w-gstamt numeric">GST amt</th>
            <th class="w-amt numeric">Amount</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="item in sale.items" :key="item.id">
            <td>{{ item.line_no }}</td>
            <td>
              {{ item.medicine_name }}
              <span v-if="item.pack_label" class="pack">{{ item.pack_label }}</span>
            </td>
            <td>{{ hsnShort(item) }}</td>
            <td>{{ batchShort(item) }}</td>
            <td class="numeric">{{ trim(item.qty_units) }}</td>
            <td class="numeric">{{ money(item.unit_mrp) }}</td>
            <td class="numeric">{{ invoice.gst_enabled ? `${item.gst_rate}%` : '' }}</td>
            <!-- The tax this one product carried. A basket mixing slabs —
                 medicine at 12 %, soap at 18 % — is only checkable line by
                 line; a single total at the foot hides which is which. -->
            <td v-if="invoice.gst_enabled" class="numeric">{{ money(lineTax(item)) }}</td>
            <td class="numeric">{{ money(item.net_amount) }}</td>
          </tr>
          <!-- Keeps the ruled table looking like a form on a short bill. -->
          <tr v-for="n in Math.max(0, 3 - sale.items.length)" :key="`blank-${n}`" class="blank">
            <td>&nbsp;</td><td></td><td></td><td></td><td></td><td></td><td></td>
            <td v-if="invoice.gst_enabled"></td><td></td>
          </tr>
        </tbody>
      </table>

      <!-- Slab-wise summary: what the return is filed from. -->
      <table
        v-if="invoice.gst_enabled && invoice.tax_summary.length"
        class="sheet__items sheet__taxtable"
      >
        <thead>
          <tr>
            <th>GST %</th>
            <th class="numeric">Taxable</th>
            <th v-if="sale.is_intra_state" class="numeric">CGST</th>
            <th v-if="sale.is_intra_state" class="numeric">SGST</th>
            <th v-else class="numeric">IGST</th>
            <th class="numeric">Total tax</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in invoice.tax_summary" :key="row.gst_rate">
            <td>{{ row.gst_rate }}%</td>
            <td class="numeric">{{ money(row.taxable_value) }}</td>
            <td v-if="sale.is_intra_state" class="numeric">{{ money(row.cgst_amount) }}</td>
            <td v-if="sale.is_intra_state" class="numeric">{{ money(row.sgst_amount) }}</td>
            <td v-else class="numeric">{{ money(row.igst_amount) }}</td>
            <td class="numeric">{{ money(row.total_tax) }}</td>
          </tr>
          <tr class="taxtable__total">
            <td>Total</td>
            <td class="numeric">{{ money(sale.taxable_amount) }}</td>
            <td v-if="sale.is_intra_state" class="numeric">{{ money(sale.cgst_amount) }}</td>
            <td v-if="sale.is_intra_state" class="numeric">{{ money(sale.sgst_amount) }}</td>
            <td v-else class="numeric">{{ money(sale.igst_amount) }}</td>
            <td class="numeric">{{ money(totalTax) }}</td>
          </tr>
        </tbody>
      </table>

      <div class="sheet__sums">
        <div class="sheet__tax">
          <span v-for="[label, amount] in taxRows" :key="label">
            {{ label }}: {{ money(amount) }}
          </span>
        </div>
        <dl class="sheet__money">
          <dt>Gross Amount:</dt>
          <dd>{{ money(sale.gross_amount) }}</dd>

          <template v-if="Number(sale.discount_amount)">
            <dt>Discount: {{ discountPct }} %</dt>
            <dd>−{{ money(sale.discount_amount) }}</dd>
          </template>

          <template v-if="Number(sale.round_off)">
            <dt>Round off:</dt>
            <dd>{{ money(sale.round_off) }}</dd>
          </template>

          <!-- With a credit note in play the invoice total and the amount to
               hand over are different numbers, and the bill has to show
               both. -->
          <template v-if="creditApplied">
            <dt>Invoice Amount:</dt>
            <dd>{{ money(sale.net_amount) }}</dd>

            <template v-for="p in creditPayments" :key="p.id">
              <dt>
                Less credit note<template v-if="p.credit_note_no">
                  {{ p.credit_note_no }}</template>:
              </dt>
              <dd>−{{ money(p.amount) }}</dd>
            </template>
          </template>

          <dt class="grand">Net Amount Payable:</dt>
          <dd class="grand">{{ money(payableAfterCredit) }}</dd>
        </dl>
      </div>

      <p class="sheet__words">
        <span>In words: {{ invoice.amount_payable_in_words }}</span>
      </p>

      <p v-if="creditApplied && creditRemaining" class="sheet__credit">
        {{ money(creditRemaining) }} remains on account for a future visit.
      </p>

      <div class="sheet__foot">
        <div class="sheet__notes">
          <p v-if="invoice.footer_text">{{ invoice.footer_text }}</p>
          <p class="sheet__pay">
            <span v-for="p in sale.payments.filter((x) => x.mode !== 'CREDIT_NOTE')" :key="p.id">
              {{ p.mode }} {{ money(p.amount) }}<template v-if="p.reference_no">
                ({{ p.reference_no }})</template>
            </span>
          </p>
          <p class="sheet__by">Billed by {{ sale.sales_user_name }}</p>
        </div>

        <figure v-if="store.order_qr_image" class="sheet__qr">
          <figcaption>{{ store.order_qr_caption || 'Scan the QR to Order' }}</figcaption>
          <img :src="store.order_qr_image" alt="Order QR code" />
        </figure>
      </div>
    </div>

    <!-- ================= thermal and A5 ================================= -->
    <div
      v-else-if="invoice && layout !== 'A5_NEW'"
      :class="['bill', layout === 'A5' ? 'bill--a5' : 'bill--thermal']"
    >
      <header class="bill__head">
        <h2>{{ store.name }}</h2>
        <p v-if="store.legal_name && store.legal_name !== store.name">{{ store.legal_name }}</p>
        <p v-if="store.address">{{ store.address }}</p>
        <p v-if="store.city">{{ store.city }} {{ store.pincode }}</p>
        <p v-if="store.phone">Ph: {{ store.phone }}</p>
        <p v-if="store.gstin && invoice.gst_enabled">GSTIN: {{ store.gstin }}</p>
        <p v-if="store.drug_licence_no_20b">
          DL: {{ store.drug_licence_no_20b }}
          <template v-if="store.drug_licence_no_21b">/ {{ store.drug_licence_no_21b }}</template>
        </p>
      </header>

      <div class="bill__meta">
        <div>
          <strong>Invoice {{ invoiceLabel }}</strong><br />
          {{ billedAt }}
        </div>
        <div>
          <template v-if="sale.customer_mobile || sale.customer_name">
            {{ sale.customer_name || 'Customer' }}<br />
            {{ sale.customer_mobile }}
            <template v-if="sale.customer_gstin"><br />GSTIN: {{ sale.customer_gstin }}</template>
          </template>
          <template v-else>Walk-in customer</template>
          <template v-if="sale.doctor_name"><br />Dr. {{ sale.doctor_name }}</template>
        </div>
      </div>

      <table class="bill__items">
        <thead>
          <tr>
            <th>#</th>
            <th>Item</th>
            <th>Batch</th>
            <th>Exp</th>
            <th class="numeric">Qty</th>
            <th class="numeric">MRP</th>
            <th class="numeric">Disc</th>
            <th v-if="invoice.gst_enabled" class="numeric">GST</th>
            <th class="numeric">Amount</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="item in sale.items" :key="item.id">
            <td>{{ item.line_no }}</td>
            <td>
              {{ item.medicine_name }}
              <span class="muted">{{ item.pack_label }}</span>
              <span v-if="hsnShort(item) && invoice.gst_enabled" class="muted">
                HSN {{ hsnShort(item) }}
              </span>
            </td>
            <td>{{ batchShort(item) }}</td>
            <td>{{ expiryShort(item) }}</td>
            <td class="numeric">{{ trim(item.qty_units) }}</td>
            <td class="numeric">{{ money(item.unit_mrp) }}</td>
            <td class="numeric">{{ item.discount_pct }}%</td>
            <td v-if="invoice.gst_enabled" class="numeric">
              {{ money(lineTax(item)) }}
              <span class="muted">{{ item.gst_rate }}%</span>
            </td>
            <td class="numeric">{{ money(item.net_amount) }}</td>
          </tr>
        </tbody>
      </table>

      <div class="bill__totals">
        <div><span>Gross</span><span>{{ money(sale.gross_amount) }}</span></div>
        <div v-if="Number(sale.discount_amount)">
          <span>Discount</span><span>−{{ money(sale.discount_amount) }}</span>
        </div>
        <template v-if="invoice.gst_enabled">
          <div><span>Taxable</span><span>{{ money(sale.taxable_amount) }}</span></div>
          <div v-if="sale.is_intra_state">
            <span>CGST</span><span>{{ money(sale.cgst_amount) }}</span>
          </div>
          <div v-if="sale.is_intra_state">
            <span>SGST</span><span>{{ money(sale.sgst_amount) }}</span>
          </div>
          <div v-else><span>IGST</span><span>{{ money(sale.igst_amount) }}</span></div>
        </template>
        <div v-if="Number(sale.round_off)">
          <span>Round off</span><span>{{ money(sale.round_off) }}</span>
        </div>
        <div v-if="creditApplied">
          <span>Invoice</span><span>{{ money(sale.net_amount) }}</span>
        </div>
        <div v-for="p in creditPayments" :key="p.id">
          <span>Credit note{{ p.credit_note_no ? ` ${p.credit_note_no}` : '' }}</span>
          <span>−{{ money(p.amount) }}</span>
        </div>
        <div class="grand">
          <span>Payable</span><span>{{ money(payableAfterCredit) }}</span>
        </div>
      </div>

      <p class="bill__words">{{ invoice.amount_payable_in_words }}</p>
      <p v-if="creditApplied && creditRemaining" class="bill__words">
        {{ money(creditRemaining) }} left on account.
      </p>

      <table v-if="invoice.gst_enabled && invoice.tax_summary.length" class="bill__tax">
        <thead>
          <tr>
            <th>GST %</th>
            <th class="numeric">Taxable</th>
            <th v-if="sale.is_intra_state" class="numeric">CGST</th>
            <th v-if="sale.is_intra_state" class="numeric">SGST</th>
            <th v-else class="numeric">IGST</th>
            <th class="numeric">Total tax</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in invoice.tax_summary" :key="row.gst_rate">
            <td>{{ row.gst_rate }}%</td>
            <td class="numeric">{{ money(row.taxable_value) }}</td>
            <td v-if="sale.is_intra_state" class="numeric">{{ money(row.cgst_amount) }}</td>
            <td v-if="sale.is_intra_state" class="numeric">{{ money(row.sgst_amount) }}</td>
            <td v-else class="numeric">{{ money(row.igst_amount) }}</td>
            <td class="numeric">{{ money(row.total_tax) }}</td>
          </tr>
          <tr class="taxtable__total">
            <td>Total</td>
            <td class="numeric">{{ money(sale.taxable_amount) }}</td>
            <td v-if="sale.is_intra_state" class="numeric">{{ money(sale.cgst_amount) }}</td>
            <td v-if="sale.is_intra_state" class="numeric">{{ money(sale.sgst_amount) }}</td>
            <td v-else class="numeric">{{ money(sale.igst_amount) }}</td>
            <td class="numeric">{{ money(totalTax) }}</td>
          </tr>
        </tbody>
      </table>

      <div class="bill__pay">
        <span v-for="p in sale.payments.filter((x) => x.mode !== 'CREDIT_NOTE')" :key="p.id">
          {{ p.mode }} {{ money(p.amount) }}<template v-if="p.reference_no">
            ({{ p.reference_no }})</template>
        </span>
      </div>

      <footer class="bill__foot">
        <p v-if="invoice.footer_text">{{ invoice.footer_text }}</p>
        <p class="muted">Billed by {{ sale.sales_user_name }}</p>
      </footer>
    </div>
    <!-- ================= A5_new ========================================
         The second A5. It began as a copy of the bill above and has since
         been laid out to its own brief: paired header lines, one meta block
         reading invoice/mobile/name then date/prescriber, a per-line GST
         *rate* with the rupees left to the slab table, and column totals
         that sit under the columns they add up.

         Kept as its own markup rather than aliased to A5, which is the whole
         point of a second A5 — the one the shop bills on today is untouched.
         The cost is that a fix to the bill body has to be made in the one you
         mean, not assumed to carry across. -->
    <div v-else-if="invoice && layout === 'A5_NEW'" class="bill bill--a5new">
      <!-- Logo in the left corner, reorder QR in the right, the shop between
           them. Both corners are held open whether or not the images have
           been uploaded, so the store block stays centred either way; the
           dashed outline that shows where they go is screen-only. Set both
           under Admin → Settings. -->
      <header class="bill__head a5n__head">
        <div class="a5n__logo">
          <img v-if="store.logo_image" :src="store.logo_image" alt="" />
          <div v-else class="a5n__ph">Logo</div>
        </div>

        <div>
          <h2>{{ store.name }}</h2>
          <p v-if="store.legal_name && store.legal_name !== store.name">{{ store.legal_name }}</p>
          <p v-if="store.address">{{ store.address }}</p>
          <p v-if="store.city">{{ store.city }} {{ store.pincode }}</p>
          <!-- Phone beside email, and the two registration numbers beside
               each other: four stacked lines cost an A5 sheet more room than
               the header deserves. -->
          <p v-if="store.phone || store.email" class="a5n__pair">
            <span v-if="store.phone">Ph: {{ store.phone }}</span>
            <span v-if="store.email">Email: {{ store.email }}</span>
          </p>
          <p
            v-if="(store.gstin && invoice.gst_enabled) || store.drug_licence_no_20b"
            class="a5n__pair"
          >
            <span v-if="store.gstin && invoice.gst_enabled">GSTIN: {{ store.gstin }}</span>
            <span v-if="store.drug_licence_no_20b">
              DL: {{ store.drug_licence_no_20b
              }}<template v-if="store.drug_licence_no_21b">
                / {{ store.drug_licence_no_21b }}</template>
            </span>
          </p>
        </div>

        <figure class="a5n__qr">
          <!-- The caption only goes on paper when there is a code under it —
               "Scan to reorder" over an empty square is worse than silence. -->
          <template v-if="store.order_qr_image">
            <figcaption>{{ store.order_qr_caption || 'Scan to reorder' }}</figcaption>
            <img :src="store.order_qr_image" alt="Reorder QR code" />
          </template>
          <div v-else class="a5n__ph">Scan to reorder</div>
        </figure>
      </header>

      <div class="a5n__meta">
        <div class="a5n__row">
          <strong>Invoice {{ invoiceLabel }}</strong>
          <span v-if="sale.customer_mobile">Mobile: {{ sale.customer_mobile }}</span>
          <span>{{ sale.customer_name || 'Walk-in customer' }}</span>
          <span v-if="sale.customer_gstin">GSTIN: {{ sale.customer_gstin }}</span>
        </div>
        <div class="a5n__row">
          <span>{{ billedAt }}</span>
          <span v-if="sale.doctor_name">Prescribed by: Dr. {{ sale.doctor_name }}</span>
        </div>
      </div>

      <table class="bill__items a5n__items">
        <thead>
          <tr>
            <th>#</th>
            <th>Item</th>
            <th>HSN</th>
            <th>Batch</th>
            <th>Exp</th>
            <th class="numeric">Qty</th>
            <th class="numeric">MRP</th>
            <th class="numeric">Discount</th>
            <th class="numeric">Amount</th>
            <th v-if="invoice.gst_enabled" class="numeric">GST</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="item in sale.items" :key="item.id">
            <td>{{ item.line_no }}</td>
            <td>
              {{ item.medicine_name }}
              <!-- Blank for anything sold loose — see packShort(). -->
              <span v-if="packShort(item)" class="a5n__pack">{{ packShort(item) }}</span>
            </td>
            <td>{{ hsnShort(item) }}</td>
            <td>{{ batchShort(item) }}</td>
            <!-- Blank for goods with no shelf life — see expiryShort(). -->
            <td>{{ expiryShort(item) }}</td>
            <td class="numeric">{{ trim(item.qty_units) }}</td>
            <td class="numeric">{{ money(item.unit_mrp) }}</td>
            <td class="numeric">{{ money(item.discount_amount) }}</td>
            <td class="numeric">{{ money(item.net_amount) }}</td>
            <!-- The rate only. The rupees per slab are in the tax table at
                 the foot, where the return is filed from. -->
            <td v-if="invoice.gst_enabled" class="numeric">{{ item.gst_rate }}%</td>
          </tr>
        </tbody>
        <!-- Each total sits under the column it adds up, so the customer can
             check it by running a finger down the page. -->
        <tfoot>
          <tr>
            <td colspan="6">Total</td>
            <td class="numeric">{{ money(sale.gross_amount) }}</td>
            <td class="numeric">
              {{ money(sale.discount_amount) }}
              <span v-if="discountPct" class="a5n__fine">{{ discountPct }}%</span>
            </td>
            <td class="numeric">{{ money(itemsTotal) }}</td>
            <td v-if="invoice.gst_enabled"></td>
          </tr>
        </tfoot>
      </table>

      <!-- The slab-wise tax on the left, what is actually handed over on the
           right: one row of two columns, so the foot of the bill does not run
           down half an A5 sheet. -->
      <div class="a5n__foot">
        <table v-if="invoice.gst_enabled && invoice.tax_summary.length" class="bill__tax">
          <thead>
            <tr>
              <th>GST %</th>
              <th class="numeric">Taxable</th>
              <th v-if="sale.is_intra_state" class="numeric">CGST</th>
              <th v-if="sale.is_intra_state" class="numeric">SGST</th>
              <th v-else class="numeric">IGST</th>
              <th class="numeric">Total tax</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="row in invoice.tax_summary" :key="row.gst_rate">
              <td>{{ row.gst_rate }}%</td>
              <td class="numeric">{{ money(row.taxable_value) }}</td>
              <td v-if="sale.is_intra_state" class="numeric">{{ money(row.cgst_amount) }}</td>
              <td v-if="sale.is_intra_state" class="numeric">{{ money(row.sgst_amount) }}</td>
              <td v-else class="numeric">{{ money(row.igst_amount) }}</td>
              <td class="numeric">{{ money(row.total_tax) }}</td>
            </tr>
            <tr class="taxtable__total">
              <td>Total</td>
              <td class="numeric">{{ money(sale.taxable_amount) }}</td>
              <td v-if="sale.is_intra_state" class="numeric">{{ money(sale.cgst_amount) }}</td>
              <td v-if="sale.is_intra_state" class="numeric">{{ money(sale.sgst_amount) }}</td>
              <td v-else class="numeric">{{ money(sale.igst_amount) }}</td>
              <td class="numeric">{{ money(totalTax) }}</td>
            </tr>
          </tbody>
        </table>
        <!-- An empty cell keeps the payable block in the right-hand column
             when there is no tax table to sit beside it. -->
        <div v-else></div>

        <!-- Gross amount → round off → credit note → what is actually handed
             over. A note is a payment, not a discount, so it comes off here
             rather than out of the invoice value or the tax beside it. -->
        <div class="a5n__pay">
          <div v-if="Number(sale.round_off)" class="a5n__payrow">
            <span>Round off</span><span>{{ money(sale.round_off) }}</span>
          </div>
          <div v-for="p in creditPayments" :key="p.id" class="a5n__payrow">
            <span>Less credit note{{ p.credit_note_no ? ` ${p.credit_note_no}` : '' }}</span>
            <span>−{{ money(p.amount) }}</span>
          </div>
          <div class="a5n__payrow a5n__payrow--grand">
            <span>Total payable</span><span>{{ money(payableAfterCredit) }}</span>
          </div>
        </div>
      </div>

      <p class="bill__words">{{ invoice.amount_payable_in_words }}</p>
      <p v-if="creditApplied && creditRemaining" class="bill__words">
        {{ money(creditRemaining) }} left on account.
      </p>

      <div class="bill__pay">
        <span v-for="p in sale.payments.filter((x) => x.mode !== 'CREDIT_NOTE')" :key="p.id">
          {{ p.mode }} {{ money(p.amount) }}<template v-if="p.reference_no">
            ({{ p.reference_no }})</template>
        </span>
      </div>

      <footer class="bill__foot">
        <p v-if="invoice.footer_text">{{ invoice.footer_text }}</p>
        <p>Billed by {{ sale.sales_user_name }}</p>
        <p class="muted">This is a system generated bill sign not required</p>
      </footer>
    </div>
  </section>
</template>

<style scoped>
.bar {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 1rem;
  flex-wrap: wrap;
}

.inline {
  display: inline-flex;
  align-items: center;
  gap: 0.4rem;
}

.small {
  font-size: 0.82rem;
}

/* ===================== A4 letterhead ===============================
   Widths are physical, so what is on screen is what comes out of the
   printer. The sheet is A4 less a 12 mm margin all round. */
.sheet {
  position: relative;
  width: 210mm;
  min-height: 297mm;
  padding: 12mm;
  margin: 0 auto;
  background: #fff;
  color: #000;
  border: 1px solid var(--border);
  font-family: 'Segoe UI', Calibri, Arial, sans-serif;
  font-size: 11pt;
  line-height: 1.35;
}

.sheet__head {
  display: grid;
  grid-template-columns: 26mm 1fr 26mm;
  align-items: center;
  gap: 4mm;
  padding-bottom: 2mm;
}

.sheet__logo {
  width: 26mm;
  max-height: 26mm;
  object-fit: contain;
}

.sheet__title {
  text-align: center;
  min-width: 0;
}

.sheet__title h2 {
  margin: 0;
  font-size: 22pt;
  font-weight: 700;
  letter-spacing: 0.01em;
}

.sheet__title p {
  margin: 0.5mm 0 0;
  font-size: 10pt;
}

.sheet__ids {
  display: flex;
  justify-content: space-between;
  gap: 6mm;
}

.sheet__kind {
  margin: 0 0 2mm;
  text-align: center;
  font-size: 12pt;
  font-weight: 600;
  border-bottom: 1px solid #000;
  padding-bottom: 2mm;
}

/* Name / Bill no / Doctor above Mobile / Bill date, as on the pad. */
.sheet__meta {
  display: grid;
  grid-template-columns: 1.4fr 1.2fr 1fr;
  gap: 1mm 6mm;
  margin-bottom: 3mm;
}

.sheet__meta span {
  color: #333;
}

.sheet__items {
  width: 100%;
  border-collapse: collapse;
  font-size: 10.5pt;
}

.sheet__items th,
.sheet__items td {
  border: 1px solid #000;
  padding: 1.6mm 2mm;
  text-align: left;
  vertical-align: top;
}

.sheet__items th {
  font-weight: 600;
}

.sheet__items .w-no { width: 8mm; }
.sheet__items .w-hsn { width: 20mm; }
.sheet__items .w-batch { width: 24mm; }
.sheet__items .w-qty { width: 13mm; }
.sheet__items .w-mrp { width: 18mm; }
.sheet__items .w-gst { width: 13mm; }
.sheet__items .w-gstamt { width: 18mm; }
.sheet__items .w-amt { width: 22mm; }

/* The slab-wise summary under the lines: same ruled look, but it is a
   handful of rows rather than the body of the bill, so it does not stretch. */
.sheet__taxtable {
  margin-top: 3mm;
  width: auto;
  min-width: 90mm;
  margin-left: auto;
}

.taxtable__total td {
  font-weight: 700;
  border-top: 1px solid #000;
}

.sheet__items .pack {
  color: #444;
  font-size: 9pt;
}

.sheet__items .blank td {
  height: 7mm;
}

.sheet__sums {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 10mm;
  margin-top: 5mm;
}

.sheet__tax {
  display: flex;
  gap: 10mm;
  padding-top: 1mm;
}

.sheet__money {
  display: grid;
  grid-template-columns: auto 28mm;
  gap: 1mm 6mm;
  margin: 0;
  min-width: 70mm;
}

.sheet__money dt {
  text-align: right;
}

.sheet__money dd {
  margin: 0;
  text-align: right;
  font-variant-numeric: tabular-nums;
}

.sheet__money .grand {
  font-weight: 700;
  font-size: 12pt;
}

.sheet__words {
  margin: 4mm 0 0;
  display: flex;
  justify-content: space-between;
  gap: 6mm;
}

.sheet__credit {
  margin: 1mm 0 0;
  font-size: 10pt;
  color: #333;
}

.sheet__foot {
  display: flex;
  justify-content: space-between;
  align-items: flex-end;
  gap: 10mm;
  margin-top: 10mm;
}

.sheet__notes p {
  margin: 0 0 1mm;
  font-size: 10pt;
}

.sheet__pay {
  display: flex;
  gap: 5mm;
  flex-wrap: wrap;
}

.sheet__by {
  color: #444;
}

.sheet__qr {
  margin: 0;
  text-align: center;
}

.sheet__qr figcaption {
  margin-bottom: 2mm;
  font-size: 10.5pt;
}

.sheet__qr img {
  width: 42mm;
  height: 42mm;
  object-fit: contain;
  border: 1px solid #000;
}

/* The document. Widths are physical, so what is on screen is what prints. */
.bill {
  position: relative;
  background: #fff;
  color: #000;
  margin: 0 auto;
  padding: 0.6rem;
  border: 1px solid var(--border);
  font-family: ui-monospace, 'Cascadia Mono', Menlo, monospace;
}

.bill--thermal {
  width: 80mm;
  font-size: 10px;
  line-height: 1.35;
}

.bill--a5 {
  width: 148mm;
  font-size: 12px;
  line-height: 1.45;
  padding: 10mm;
}

/* A5_new — its own rules, kept separate on purpose so the two A5 layouts can
   be tuned apart.

   180 × 210 mm, with the 8 mm margin inside it (the page itself is set to no
   margin — see applyPageSize). Everything is border-box, so the content
   column is 164 mm and what is on screen is what comes out of the printer.
   `min-height` rather than `height`: a bill with more lines than one sheet
   holds grows here and breaks across pages when printed, rather than being
   clipped. */
.bill--a5new {
  width: 180mm;
  min-height: 210mm;
  font-size: 12px;
  line-height: 1.4;
  padding: 8mm;
}

/* `.bill__items td` sets text-align on a class+element selector, which
   outranks a bare `.numeric`. Two classes put the money back on the right —
   scoped to this layout, so the older bills print exactly as before. */
.bill--a5new .numeric {
  text-align: right;
}

/* Logo | shop | reorder QR. The corners are fixed widths so the middle
   column is centred on the sheet rather than on whatever is left over. */
.a5n__head {
  display: grid;
  grid-template-columns: 24mm 1fr 24mm;
  align-items: center;
  gap: 0 4mm;
}

.a5n__qr {
  margin: 0;
}

.a5n__logo img,
.a5n__qr img {
  display: block;
  width: 100%;
  max-height: 24mm;
  object-fit: contain;
}

.a5n__qr figcaption {
  font-size: 0.75em;
  line-height: 1.15;
  margin-bottom: 1mm;
}

/* Holds a corner open before its image has been uploaded. The outline says
   where the artwork goes; it is dropped when printing, so a shop that has
   not set one yet hands the customer a clean bill rather than an empty box. */
.a5n__ph {
  display: grid;
  place-items: center;
  height: 20mm;
  padding: 1mm;
  border: 1px dashed #999;
  border-radius: 2px;
  color: #999;
  font-size: 0.72em;
  line-height: 1.15;
}

/* Two facts on one line: phone/email, GSTIN/licence. */
.a5n__pair {
  display: flex;
  justify-content: center;
  gap: 0.9rem;
  flex-wrap: wrap;
}

.a5n__meta {
  padding: 0.4rem 0;
  border-bottom: 1px dashed #000;
}

/* Justified: invoice number at the left edge, name at the right, mobile
   between them — and the date/prescriber line below on the same two edges. */
.a5n__row {
  display: flex;
  justify-content: space-between;
  gap: 0.9rem;
  flex-wrap: wrap;
}

/* Inline with the item name — "100ml", not a second line. */
.a5n__pack {
  font-size: 0.85em;
  color: #444;
}

/* The average discount, under the discount total it belongs to. */
.a5n__fine {
  display: block;
  font-weight: 400;
  font-size: 0.8em;
  color: #444;
}

.a5n__items tfoot td {
  border-top: 1px solid #000;
  font-weight: 700;
}

/* Tax table beside the payable block. `auto` on the right so the tax table
   takes the room it needs and the money column stays as narrow as its
   longest figure. */
.a5n__foot {
  display: grid;
  grid-template-columns: 1fr auto;
  gap: 0 1.2rem;
  align-items: start;
  margin-top: 0.5rem;
}

.a5n__foot .bill__tax {
  margin-top: 0;
}

.a5n__pay {
  min-width: 52mm;
  border-top: 1px dashed #000;
  padding-top: 0.35rem;
}

.a5n__payrow {
  display: flex;
  justify-content: space-between;
}

.a5n__payrow--grand {
  border-top: 1px solid #000;
  margin-top: 0.25rem;
  padding-top: 0.25rem;
  font-weight: 700;
  font-size: 1.15em;
}

.bill__head {
  text-align: center;
  border-bottom: 1px dashed #000;
  padding-bottom: 0.4rem;
}

.bill__head h2 {
  margin: 0;
  font-size: 1.15em;
}

.bill__head p {
  margin: 0;
}

.bill__meta {
  display: flex;
  justify-content: space-between;
  gap: 0.75rem;
  padding: 0.4rem 0;
  border-bottom: 1px dashed #000;
}

.bill__items,
.bill__tax {
  width: 100%;
  border-collapse: collapse;
  margin-top: 0.4rem;
}

.bill__items th,
.bill__items td,
.bill__tax th,
.bill__tax td {
  padding: 0.15rem 0.2rem;
  text-align: left;
  vertical-align: top;
}

.bill__items thead th,
.bill__tax thead th {
  border-bottom: 1px solid #000;
}

.bill__items .muted {
  display: block;
  font-size: 0.85em;
  color: #444;
}

.numeric {
  text-align: right;
}

.bill__totals {
  margin-top: 0.5rem;
  border-top: 1px dashed #000;
  padding-top: 0.35rem;
}

.bill__totals > div {
  display: flex;
  justify-content: space-between;
}

.bill__totals .grand {
  border-top: 1px solid #000;
  margin-top: 0.25rem;
  padding-top: 0.25rem;
  font-weight: 700;
  font-size: 1.15em;
}

.bill__words {
  margin: 0.4rem 0;
  font-style: italic;
}

.bill__pay {
  display: flex;
  gap: 0.75rem;
  flex-wrap: wrap;
  border-top: 1px dashed #000;
  padding-top: 0.35rem;
  margin-top: 0.4rem;
}

.bill__foot {
  text-align: center;
  border-top: 1px dashed #000;
  margin-top: 0.4rem;
  padding-top: 0.35rem;
}

.bill__foot p {
  margin: 0;
}

/* Only the document reaches the printer. */
@media print {
  :global(.shell__top),
  :global(.shell__side),
  .no-print {
    display: none !important;
  }

  /* Collapse the shell's grid so the bill is not printed inside a column. */
  :global(.shell) {
    display: block !important;
  }

  :global(body),
  :global(.shell__main) {
    background: #fff;
    padding: 0;
    margin: 0;
    max-width: none;
  }

  /* A roll has no margin to give, so the bill keeps a hair of its own. */
  .bill {
    border: 0;
    padding: 2mm;
    width: auto;
  }

  /* A5_new asks the dialog for a 180 × 210 mm page (see applyPageSize), so it
     keeps its own width and margin rather than spreading to whatever it is
     given.

     The screen's 210 mm floor comes off: the page is already exactly that
     tall, and a box asked to fill it can round a hair over and put out a
     blank second sheet. A bill longer than one page flows onto the next. */
  .bill--a5new {
    width: 180mm;
    min-height: 0;
    padding: 8mm;
  }

  /* The corner keeps its space so the header stays centred, but nothing is
     drawn — an empty dashed box has no business on a customer's bill. */
  .a5n__ph {
    border-color: transparent;
    color: transparent;
  }

  /* The sheet carries its own 12 mm margin (see `.sheet` above), which is
     why the page is set to none — otherwise the two would stack. */
  .sheet {
    border: 0;
    margin: 0;
    width: auto;
    min-height: 0;
  }

  /* An itemised bill must not split a row across two pages. */
  .sheet__items tr,
  .a5n__items tr {
    break-inside: avoid;
  }

  .sheet__items thead,
  .a5n__items thead {
    display: table-header-group;
  }

  /* A `tfoot` repeats on every page by default — right for a running total,
     wrong for this one, which is the total of the whole bill. Demoted to an
     ordinary row group so it prints once, after the last line. */
  .a5n__items tfoot {
    display: table-row-group;
    break-inside: avoid;
  }

  /* Tax table, payable, words and the sign-off each stay whole. A page break
     through the middle of the payable block would put "Total payable" on a
     sheet of its own with nothing to show what it was arrived at from. */
  .a5n__foot,
  .bill--a5new .bill__words,
  .bill--a5new .bill__pay,
  .bill--a5new .bill__foot {
    break-inside: avoid;
  }

  .sheet__foot,
  .sheet__sums {
    break-inside: avoid;
  }
}

/* Each layout supplies its own margin — an A4 sheet 12 mm, a thermal roll
   almost none — so the page itself must not add a second one. */
@page {
  margin: 0;
}
</style>
