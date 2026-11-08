<script setup>
import { onMounted, ref } from 'vue'

import { fetchSettings } from '@/api/admin'
import { fetchHealth } from '@/api/system'
import { useAuthStore } from '@/stores/auth'

const auth = useAuthStore()

const health = ref(null)
const settings = ref(null)
const state = ref('loading') // loading | ok | error
const error = ref(null)

async function load() {
  state.value = 'loading'
  error.value = null
  try {
    const [healthData, settingsData] = await Promise.all([fetchHealth(), fetchSettings()])
    health.value = healthData
    settings.value = settingsData
    state.value = healthData.status === 'ok' ? 'ok' : 'error'
  } catch (err) {
    error.value = err
    state.value = 'error'
  }
}

onMounted(load)
</script>

<template>
  <section class="stack">
    <div class="card stack">
      <h1>Welcome, {{ auth.user?.full_name }}</h1>
      <p class="muted">
        Signed in to <strong>{{ auth.store?.name }}</strong> as
        {{ auth.isAdmin ? 'an administrator' : 'a sales executive' }}.
      </p>
    </div>

    <p v-if="state === 'loading'" class="muted">Loading…</p>
    <p v-else-if="state === 'error'" class="alert alert--error">
      {{ error?.message || 'The API is unavailable.' }}
    </p>

    <template v-else>
      <div class="card stack">
        <h2>Current policy</h2>
        <dl class="kv">
          <dt>Discount range</dt>
          <dd>{{ settings.default_discount_pct }}% suggested · {{ settings.max_discount_pct }}% maximum</dd>
          <dt>Near-expiry alert</dt>
          <dd>{{ settings.near_expiry_days }} days</dd>
          <dt>Order book trigger</dt>
          <dd>stock decline of {{ settings.order_book_decline_pct }}%</dd>
          <dt>Return window</dt>
          <dd>{{ settings.return_window_days }} days</dd>
          <dt>GST</dt>
          <dd>
            {{ settings.gst_enabled ? 'Recorded' : 'Not recorded' }} ·
            {{ settings.price_includes_gst ? 'MRP includes GST' : 'MRP excludes GST' }}
          </dd>
        </dl>
        <p v-if="auth.isAdmin" class="muted">
          Change these under <RouterLink to="/admin/settings">Settings</RouterLink>.
        </p>
      </div>

      <div class="card stack">
        <h2>Build progress</h2>
        <p><span class="badge badge--ok">Phase 2 complete</span></p>
        <p class="muted">
          Auth, audit trail and the medicine catalogue are live —
          <RouterLink to="/medicine/catalogue">search by brand or generic name</RouterLink> and see
          same-salt alternatives. Next: batch inventory and goods inward, then the POS screen — see
          <code>docs/ROADMAP.md</code>.
        </p>
        <dl class="kv">
          <dt>API version</dt>
          <dd>{{ health.version }}</dd>
          <dt>Environment</dt>
          <dd>{{ health.environment }}</dd>
          <dt>Database</dt>
          <dd>{{ health.db }}</dd>
        </dl>
      </div>
    </template>
  </section>
</template>
