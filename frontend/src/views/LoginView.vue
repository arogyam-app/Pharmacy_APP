<script setup>
import { onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { useAuthStore } from '@/stores/auth'

const auth = useAuthStore()
const router = useRouter()
const route = useRoute()

// The email is remembered between visits; the password never is. Browsers
// have a password manager for that, and it is the only place a stored
// credential belongs — behind the operating system's own protection, not in
// this app's storage where any script on the page could read it.
const email = ref(auth.rememberedEmail)
const password = ref('')
const rememberMe = ref(auth.rememberMe)
const submitting = ref(false)
const error = ref(null)
const notice = ref(null)

onMounted(() => {
  if (route.query.expired) notice.value = 'Your session expired. Please sign in again.'
})

async function submit() {
  submitting.value = true
  error.value = null
  notice.value = null
  try {
    await auth.login(email.value.trim(), password.value, rememberMe.value)
    router.push(auth.mustChangePassword ? { name: 'change-password' } : route.query.next || '/')
  } catch (err) {
    error.value = err.message
  } finally {
    submitting.value = false
  }
}
</script>

<template>
  <div class="login">
    <form class="card login__card stack" @submit.prevent="submit">
      <div>
        <h1>Pharmacy POS</h1>
        <p class="muted">Sign in to continue</p>
      </div>

      <p v-if="notice" class="badge badge--warn">{{ notice }}</p>

      <label class="field">
        <span>Email</span>
        <input
          v-model="email"
          type="email"
          autocomplete="username"
          required
          autofocus
          placeholder="you@pharmacy.in"
        />
      </label>

      <label class="field">
        <span>Password</span>
        <input
          v-model="password"
          type="password"
          autocomplete="current-password"
          required
          :autofocus="Boolean(email)"
        />
      </label>

      <label class="login__remember">
        <input v-model="rememberMe" type="checkbox" />
        <span>
          Keep me signed in on this device
          <small class="muted">
            Stays signed in for 30 days. Leave this off on a shared counter machine —
            the session then ends when the browser closes.
          </small>
        </span>
      </label>

      <p v-if="error" class="alert alert--error">{{ error }}</p>

      <button class="btn btn--primary" type="submit" :disabled="submitting">
        {{ submitting ? 'Signing in…' : 'Sign in' }}
      </button>
    </form>
  </div>
</template>

<style scoped>
.login {
  display: grid;
  place-items: center;
  min-height: 100vh;
  padding: 1.5rem;
}

.login__card {
  width: min(400px, 100%);
}

.login__remember {
  display: flex;
  gap: 0.55rem;
  align-items: flex-start;
  cursor: pointer;
  line-height: 1.35;
}

.login__remember input {
  margin-top: 0.2rem;
}

.login__remember small {
  display: block;
  font-size: 0.78rem;
}
</style>
