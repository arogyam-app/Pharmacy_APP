<script setup>
import { onMounted, reactive, ref } from 'vue'

import { fetchSettings, fetchStore, updateSettings, updateStore } from '@/api/admin'

const settings = reactive({})
const store = reactive({})
const loading = ref(true)
const savingSettings = ref(false)
const savingStore = ref(false)
const settingsMessage = ref(null)
const storeMessage = ref(null)
const error = ref(null)

async function load() {
  loading.value = true
  try {
    Object.assign(settings, await fetchSettings())
    Object.assign(store, await fetchStore())
  } catch (err) {
    error.value = err.message
  } finally {
    loading.value = false
  }
}

// --- letterhead branding ---------------------------------------------------
const brandingSlots = [
  {
    key: 'logo_image',
    label: 'Shop logo',
    hint: 'Top left of the bill.',
  },
  {
    key: 'partner_logo_image',
    label: 'Partner logo',
    hint: 'Top right. Leave empty if there is none.',
  },
  {
    key: 'order_qr_image',
    label: 'Order QR',
    hint: 'Bottom right on A4, top right on A5_new.',
  },
]

const savingBranding = ref(false)
const brandingMessage = ref(null)

// 400 KB of image ≈ 546 KB once base64-encoded, which is the server's limit.
const MAX_IMAGE_BYTES = 400 * 1024

/** Read the picked file into a data URI — no upload endpoint needed. */
function pickImage(key, event) {
  const file = event.target.files?.[0]
  event.target.value = '' // let the same file be picked again after a Remove
  if (!file) return

  if (file.size > MAX_IMAGE_BYTES) {
    brandingMessage.value = {
      ok: false,
      text: `${file.name} is ${Math.round(file.size / 1024)} KB. Keep it under 400 KB.`,
    }
    return
  }

  const reader = new FileReader()
  reader.onload = () => {
    settings[key] = reader.result
    brandingMessage.value = { ok: true, text: 'Picked — save to keep it.' }
  }
  reader.onerror = () => {
    brandingMessage.value = { ok: false, text: `${file.name} could not be read.` }
  }
  reader.readAsDataURL(file)
}

function clearImage(key) {
  settings[key] = null
  brandingMessage.value = { ok: true, text: 'Cleared — save to keep it.' }
}

async function saveBranding() {
  savingBranding.value = true
  brandingMessage.value = null
  try {
    Object.assign(
      settings,
      await updateSettings({
        // An empty string clears the image server-side; null would mean
        // "leave it alone" on a partial update.
        logo_image: settings.logo_image || '',
        partner_logo_image: settings.partner_logo_image || '',
        order_qr_image: settings.order_qr_image || '',
        order_qr_caption: settings.order_qr_caption || null,
      }),
    )
    brandingMessage.value = { ok: true, text: 'Letterhead saved.' }
  } catch (err) {
    brandingMessage.value = { ok: false, text: err.message }
  } finally {
    savingBranding.value = false
  }
}

async function saveSettings() {
  savingSettings.value = true
  settingsMessage.value = null
  try {
    Object.assign(
      settings,
      await updateSettings({
        default_discount_pct: settings.default_discount_pct,
        max_discount_pct: settings.max_discount_pct,
        near_expiry_days: Number(settings.near_expiry_days),
        low_stock_units: settings.low_stock_units,
        order_book_decline_pct: settings.order_book_decline_pct,
        return_window_days: Number(settings.return_window_days),
        credit_note_validity_days: Number(settings.credit_note_validity_days),
        gst_enabled: settings.gst_enabled,
        gst_registration_type: settings.gst_registration_type,
        price_includes_gst: settings.price_includes_gst,
        print_format: settings.print_format,
        round_off_enabled: settings.round_off_enabled,
        order_qr_caption: settings.order_qr_caption || null,
      }),
    )
    settingsMessage.value = { ok: true, text: 'Settings saved.' }
  } catch (err) {
    settingsMessage.value = { ok: false, text: err.message }
  } finally {
    savingSettings.value = false
  }
}

async function saveStore() {
  savingStore.value = true
  storeMessage.value = null
  try {
    Object.assign(
      store,
      await updateStore({
        name: store.name,
        legal_name: store.legal_name || null,
        address_line1: store.address_line1 || null,
        city: store.city || null,
        state: store.state || null,
        state_code: store.state_code || null,
        pincode: store.pincode || null,
        gstin: store.gstin || null,
        drug_licence_no_20b: store.drug_licence_no_20b || null,
        drug_licence_no_21b: store.drug_licence_no_21b || null,
        phone: store.phone || null,
        // Null, not "": the server validates this as an address, and an
        // empty string is not one.
        email: store.email || null,
      }),
    )
    storeMessage.value = { ok: true, text: 'Store profile saved.' }
  } catch (err) {
    storeMessage.value = { ok: false, text: err.message }
  } finally {
    savingStore.value = false
  }
}

onMounted(load)
</script>

<template>
  <section class="stack">
    <div class="card stack">
      <h1>Settings</h1>
      <p class="muted">
        These values drive pricing, alerts and returns across the whole app. The discount limits are
        enforced by the server on every sale.
      </p>
    </div>

    <p v-if="error" class="alert alert--error">{{ error }}</p>
    <p v-if="loading" class="muted">Loading…</p>

    <template v-else>
      <form class="card stack" @submit.prevent="saveSettings">
        <h2>Discount policy</h2>
        <div class="form-grid">
          <label class="field">
            <span>Default discount %</span>
            <input v-model="settings.default_discount_pct" type="number" step="0.01" min="0" max="100" />
            <p class="field__hint">Suggested on each line at the counter.</p>
          </label>
          <label class="field">
            <span>Maximum discount %</span>
            <input v-model="settings.max_discount_pct" type="number" step="0.01" min="0" max="100" />
            <p class="field__hint">A sales executive can never exceed this.</p>
          </label>
        </div>

        <h2>Alerts &amp; reordering</h2>
        <div class="form-grid">
          <label class="field">
            <span>Near-expiry window (days)</span>
            <input v-model="settings.near_expiry_days" type="number" min="1" max="730" />
          </label>
          <label class="field">
            <span>Low stock threshold (units)</span>
            <input v-model="settings.low_stock_units" type="number" step="0.001" min="0" />
          </label>
          <label class="field">
            <span>Order book trigger (% decline)</span>
            <input v-model="settings.order_book_decline_pct" type="number" step="0.01" min="1" max="99" />
          </label>
        </div>

        <h2>Returns</h2>
        <div class="form-grid">
          <label class="field">
            <span>Return window (days)</span>
            <input v-model="settings.return_window_days" type="number" min="0" max="365" />
          </label>
          <label class="field">
            <span>Credit note validity (days)</span>
            <input v-model="settings.credit_note_validity_days" type="number" min="1" max="3650" />
          </label>
        </div>

        <h2>GST &amp; printing</h2>
        <div class="form-grid">
          <label class="field">
            <span>GST recording</span>
            <select v-model="settings.gst_enabled">
              <option :value="true">Enabled</option>
              <option :value="false">Disabled</option>
            </select>
          </label>
          <label class="field">
            <span>Registration type</span>
            <select v-model="settings.gst_registration_type">
              <option value="REGULAR">Regular</option>
              <option value="COMPOSITION">Composition</option>
              <option value="UNREGISTERED">Unregistered</option>
            </select>
          </label>
          <label class="field">
            <span>MRP includes GST</span>
            <select v-model="settings.price_includes_gst">
              <option :value="true">Yes</option>
              <option :value="false">No</option>
            </select>
          </label>
          <label class="field">
            <span>Invoice format</span>
            <select v-model="settings.print_format">
              <option value="A4">A4 letterhead</option>
              <option value="THERMAL_80">Thermal 80 mm</option>
              <option value="A5">A5</option>
              <option value="A5_NEW">A5_new</option>
            </select>
          </label>
          <label class="field">
            <span>Round off totals</span>
            <select v-model="settings.round_off_enabled">
              <option :value="true">Yes</option>
              <option :value="false">No</option>
            </select>
          </label>
        </div>

        <p v-if="settingsMessage" :class="['alert', settingsMessage.ok ? 'alert--ok' : 'alert--error']">
          {{ settingsMessage.text }}
        </p>

        <div class="form-actions">
          <button class="btn btn--primary" type="submit" :disabled="savingSettings">
            {{ savingSettings ? 'Saving…' : 'Save settings' }}
          </button>
        </div>
      </form>

      <!-- Letterhead branding: what the A4 bill prints around the figures. -->
      <div class="card stack">
        <h2>Letterhead</h2>
        <p class="muted">
          Printed by the <strong>A4 letterhead</strong> invoice. Images are stored with the
          settings, so a bill needs nothing else to render. PNG or JPEG, under 400 KB —
          a logo around 300 px wide is plenty.
        </p>

        <div class="grid-3">
          <div v-for="slot in brandingSlots" :key="slot.key" class="brand">
            <span class="brand__label">{{ slot.label }}</span>
            <div class="brand__frame">
              <img v-if="settings[slot.key]" :src="settings[slot.key]" :alt="slot.label" />
              <span v-else class="muted small">Not set</span>
            </div>
            <p class="muted small">{{ slot.hint }}</p>
            <div class="form-actions">
              <input
                :id="`pick-${slot.key}`"
                class="brand__file"
                type="file"
                accept="image/png,image/jpeg,image/svg+xml,image/webp"
                @change="pickImage(slot.key, $event)"
              />
              <label class="btn btn--sm" :for="`pick-${slot.key}`">Choose…</label>
              <button
                v-if="settings[slot.key]"
                class="btn btn--sm btn--danger"
                type="button"
                @click="clearImage(slot.key)"
              >
                Remove
              </button>
            </div>
          </div>
        </div>

        <label class="field">
          <span>QR caption</span>
          <input
            v-model="settings.order_qr_caption"
            maxlength="80"
            placeholder="Scan the QR to Order"
          />
        </label>

        <p v-if="brandingMessage" :class="['alert', brandingMessage.ok ? 'alert--ok' : 'alert--error']">
          {{ brandingMessage.text }}
        </p>

        <div class="form-actions">
          <button class="btn btn--primary" :disabled="savingBranding" @click="saveBranding">
            {{ savingBranding ? 'Saving…' : 'Save letterhead' }}
          </button>
        </div>
      </div>

      <form class="card stack" @submit.prevent="saveStore">
        <h2>Store profile</h2>
        <p class="muted">Printed on every invoice.</p>
        <div class="form-grid">
          <label class="field"><span>Store name</span><input v-model="store.name" required /></label>
          <label class="field"><span>Legal name</span><input v-model="store.legal_name" /></label>
          <label class="field"><span>Address</span><input v-model="store.address_line1" /></label>
          <label class="field"><span>City</span><input v-model="store.city" /></label>
          <label class="field"><span>State</span><input v-model="store.state" /></label>
          <label class="field">
            <span>State code</span>
            <input v-model="store.state_code" maxlength="2" placeholder="18" />
            <p class="field__hint">Two digits — decides CGST+SGST vs IGST.</p>
          </label>
          <label class="field"><span>PIN code</span><input v-model="store.pincode" /></label>
          <label class="field"><span>GSTIN</span><input v-model="store.gstin" maxlength="15" /></label>
          <label class="field">
            <span>Drug licence 20B</span><input v-model="store.drug_licence_no_20b" />
          </label>
          <label class="field">
            <span>Drug licence 21B</span><input v-model="store.drug_licence_no_21b" />
          </label>
          <label class="field"><span>Phone</span><input v-model="store.phone" /></label>
          <label class="field">
            <span>Email</span>
            <input v-model="store.email" type="email" placeholder="shop@example.com" />
            <p class="field__hint">Printed beside the phone number on the bill.</p>
          </label>
        </div>

        <p v-if="storeMessage" :class="['alert', storeMessage.ok ? 'alert--ok' : 'alert--error']">
          {{ storeMessage.text }}
        </p>

        <div class="form-actions">
          <button class="btn btn--primary" type="submit" :disabled="savingStore">
            {{ savingStore ? 'Saving…' : 'Save store profile' }}
          </button>
        </div>
      </form>
    </template>
  </section>
</template>

<style scoped>
.brand {
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
}

.brand__label {
  font-size: 0.8rem;
  font-weight: 600;
  color: var(--text-muted);
}

/* A white frame, because a logo is drawn for white paper. */
.brand__frame {
  display: flex;
  align-items: center;
  justify-content: center;
  height: 6.5rem;
  padding: var(--sp-2);
  background: #fff;
  border: 1px dashed var(--border-strong);
  border-radius: var(--radius-sm);
}

.brand__frame img {
  max-width: 100%;
  max-height: 100%;
  object-fit: contain;
}

/* The real input is hidden behind its label, which is styled as a button. */
.brand__file {
  position: absolute;
  width: 1px;
  height: 1px;
  opacity: 0;
  pointer-events: none;
}

.brand__file:focus-visible + label {
  outline: 2px solid var(--accent);
  outline-offset: 2px;
}
</style>
