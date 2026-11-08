<script setup>
import { computed, ref } from 'vue'
import { useRouter } from 'vue-router'

import { changePassword } from '@/api/auth'
import { useAuthStore } from '@/stores/auth'

const auth = useAuthStore()
const router = useRouter()

const currentPassword = ref('')
const newPassword = ref('')
const confirmPassword = ref('')
const submitting = ref(false)
const error = ref(null)

const forced = computed(() => auth.mustChangePassword)
const mismatch = computed(
  () => confirmPassword.value.length > 0 && newPassword.value !== confirmPassword.value,
)

async function submit() {
  if (mismatch.value) return
  submitting.value = true
  error.value = null
  try {
    await changePassword(currentPassword.value, newPassword.value)
    // The server revokes every session on a password change, so start over.
    auth.clear()
    router.push({ name: 'login', query: { changed: '1' } })
  } catch (err) {
    error.value = err.message
  } finally {
    submitting.value = false
  }
}
</script>

<template>
  <section class="card stack narrow">
    <div>
      <h1>Change password</h1>
      <p v-if="forced" class="muted">
        Your account uses a temporary password. Choose a new one to continue.
      </p>
    </div>

    <form class="stack" @submit.prevent="submit">
      <label class="field">
        <span>Current password</span>
        <input v-model="currentPassword" type="password" autocomplete="current-password" required />
      </label>

      <label class="field">
        <span>New password</span>
        <input v-model="newPassword" type="password" autocomplete="new-password" required />
        <p class="field__hint">At least 8 characters, with a letter and a digit.</p>
      </label>

      <label class="field">
        <span>Confirm new password</span>
        <input v-model="confirmPassword" type="password" autocomplete="new-password" required />
      </label>

      <p v-if="mismatch" class="alert alert--warn">The two passwords do not match.</p>
      <p v-if="error" class="alert alert--error">{{ error }}</p>

      <div class="form-actions">
        <button class="btn btn--primary" type="submit" :disabled="submitting || mismatch">
          {{ submitting ? 'Saving…' : 'Update password' }}
        </button>
        <RouterLink v-if="!forced" class="muted" to="/">Cancel</RouterLink>
      </div>
    </form>
  </section>
</template>

<style scoped>
.narrow {
  max-width: 460px;
}
</style>
