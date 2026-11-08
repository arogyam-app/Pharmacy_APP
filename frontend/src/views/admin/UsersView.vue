<script setup>
/**
 * Users, and what each of them can reach.
 *
 * Access starts from the role and stays there until an admin says otherwise:
 * the editor below opens on the role defaults, and a person with no
 * exceptions has none stored. Only a deliberate departure is written down,
 * so changing what a role means later still flows through to everybody who
 * was never given an exception.
 *
 * The ticks are not the security boundary — the API refuses a revoked module
 * on its own. Hiding a door that is locked is a courtesy, not the lock.
 */
import { computed, onMounted, reactive, ref } from 'vue'

import {
  createUser,
  deactivateUser,
  fetchPermissionCatalogue,
  fetchUserPermissions,
  listUsers,
  resetUserPassword,
  setUserPermissions,
  updateUser,
} from '@/api/admin'
import { UserRole } from '@/constants'
import { useAuthStore } from '@/stores/auth'

const auth = useAuthStore()

const users = ref([])
const loading = ref(true)
const error = ref(null)
const search = ref('')

const form = reactive({ full_name: '', email: '', phone: '', role: UserRole.SALES_EXEC })
const creating = ref(false)
const formError = ref(null)
// Shown once, right after creation or a reset — it is never retrievable later.
const handover = ref(null)

async function load() {
  loading.value = true
  error.value = null
  try {
    const data = await listUsers({ q: search.value || undefined, page_size: 100 })
    users.value = data.items
  } catch (err) {
    error.value = err.message
  } finally {
    loading.value = false
  }
}

async function submit() {
  creating.value = true
  formError.value = null
  handover.value = null
  try {
    const result = await createUser({
      full_name: form.full_name.trim(),
      email: form.email.trim(),
      phone: form.phone.trim() || null,
      role: form.role,
    })
    handover.value = {
      email: result.user.email,
      password: result.temporary_password,
      label: 'Account created',
    }
    Object.assign(form, { full_name: '', email: '', phone: '', role: UserRole.SALES_EXEC })
    await load()
  } catch (err) {
    formError.value = err.message
  } finally {
    creating.value = false
  }
}

async function resetPassword(user) {
  if (!confirm(`Reset the password for ${user.full_name}? Their current sessions will end.`)) return
  try {
    const result = await resetUserPassword(user.id)
    handover.value = {
      email: user.email,
      password: result.temporary_password,
      label: 'Password reset',
    }
  } catch (err) {
    error.value = err.message
  }
}

async function toggleActive(user) {
  try {
    if (user.is_active) await deactivateUser(user.id)
    else await updateUser(user.id, { is_active: true })
    await load()
  } catch (err) {
    error.value = err.message
  }
}

// --- access editor ---------------------------------------------------------

const catalogue = ref([])
const editing = ref(null)          // the user whose access is open
const access = ref(null)           // the server's view of their access
const chosen = ref(new Set())      // what the ticks currently say
const note = ref('')
const saving = ref(false)
const accessError = ref(null)
const accessNotice = ref(null)

/** The catalogue, grouped the way the sidebar groups the app. */
const permissionGroups = computed(() => {
  const groups = []
  for (const entry of catalogue.value) {
    let group = groups.find((g) => g.key === entry.group)
    if (!group) groups.push((group = { key: entry.group, label: entry.group_label, items: [] }))
    group.items.push(entry)
  }
  return groups
})

const editingIsExec = computed(() => editing.value?.role !== UserRole.ADMIN)

/**
 * Some ticks must not be offered at all.
 *
 * An admin always keeps user administration, or two of them could revoke each
 * other and leave the store with no way back in. And nothing that reveals
 * cost, or that would let a non-admin rewrite the policy governing them, can
 * be handed over — so the box is disabled and says why, rather than saving a
 * tick the server would refuse.
 */
function lockedReason(entry) {
  if (access.value?.locked_permissions.includes(entry.key)) {
    return 'An administrator must keep this.'
  }
  if (access.value?.unavailable_permissions.includes(entry.key)) {
    return editingIsExec.value ? 'Admin role only.' : 'Not available for this role.'
  }
  return null
}

/** True where this tick departs from what the role would give. */
function isException(entry) {
  const byDefault = access.value?.default_permissions.includes(entry.key)
  return chosen.value.has(entry.key) !== Boolean(byDefault)
}

const dirty = computed(() => {
  if (!access.value) return false
  return [...chosen.value].sort().join(',') !== [...access.value.permissions].sort().join(',')
})

const exceptionCount = computed(
  () => catalogue.value.filter((entry) => isException(entry)).length,
)

async function openAccess(user) {
  accessError.value = null
  accessNotice.value = null
  note.value = ''
  editing.value = user
  access.value = null
  try {
    if (!catalogue.value.length) catalogue.value = await fetchPermissionCatalogue()
    access.value = await fetchUserPermissions(user.id)
    chosen.value = new Set(access.value.permissions)
  } catch (err) {
    accessError.value = err.message
  }
}

function toggle(entry) {
  if (lockedReason(entry)) return
  const next = new Set(chosen.value)
  if (next.has(entry.key)) next.delete(entry.key)
  else next.add(entry.key)
  chosen.value = next
}

/** Tick or clear a whole group at once — the counter tab has nine of them. */
function toggleGroup(group, on) {
  const next = new Set(chosen.value)
  for (const entry of group.items) {
    if (lockedReason(entry)) continue
    if (on) next.add(entry.key)
    else next.delete(entry.key)
  }
  chosen.value = next
}

function groupState(group) {
  const open = group.items.filter((e) => !lockedReason(e))
  const on = open.filter((e) => chosen.value.has(e.key)).length
  if (!open.length) return 'locked'
  if (on === 0) return 'none'
  return on === open.length ? 'all' : 'some'
}

function restoreDefaults() {
  if (access.value) chosen.value = new Set(access.value.default_permissions)
}

async function saveAccess() {
  saving.value = true
  accessError.value = null
  try {
    access.value = await setUserPermissions(editing.value.id, [...chosen.value], note.value || null)
    chosen.value = new Set(access.value.permissions)
    accessNotice.value = access.value.overrides.length
      ? `${access.value.overrides.length} exception(s) saved.`
      : 'Back to the role defaults — no exceptions stored.'
    note.value = ''
  } catch (err) {
    accessError.value = err.message
  } finally {
    saving.value = false
  }
}

function closeAccess() {
  editing.value = null
  access.value = null
}

onMounted(load)
</script>

<template>
  <section class="stack">
    <div class="card stack">
      <h1>Users</h1>
      <p class="muted">Sales executives sign in with the email and temporary password you issue.</p>
    </div>

    <div class="card stack">
      <h2>Add a user</h2>
      <form class="stack" @submit.prevent="submit">
        <div class="form-grid">
          <label class="field">
            <span>Full name</span>
            <input v-model="form.full_name" required minlength="2" />
          </label>
          <label class="field">
            <span>Email</span>
            <input v-model="form.email" type="email" required placeholder="staff@pharmacy.in" />
          </label>
          <label class="field">
            <span>Phone</span>
            <input v-model="form.phone" maxlength="15" />
          </label>
          <label class="field">
            <span>Role</span>
            <select v-model="form.role">
              <option :value="UserRole.SALES_EXEC">Sales executive</option>
              <option :value="UserRole.ADMIN">Admin</option>
            </select>
          </label>
        </div>

        <p v-if="formError" class="alert alert--error">{{ formError }}</p>

        <div class="form-actions">
          <button class="btn btn--primary" type="submit" :disabled="creating">
            {{ creating ? 'Creating…' : 'Create user' }}
          </button>
        </div>
      </form>

      <div v-if="handover" class="alert alert--ok">
        <strong>{{ handover.label }}.</strong>
        Give <code>{{ handover.email }}</code> this temporary password —
        <code>{{ handover.password }}</code
        >. It is shown once and must be changed at first sign-in.
      </div>
    </div>

    <div class="card stack">
      <div class="form-actions">
        <input
          v-model="search"
          class="grow"
          placeholder="Search name or email"
          @keyup.enter="load"
        />
        <button class="btn" @click="load">Search</button>
      </div>

      <p v-if="error" class="alert alert--error">{{ error }}</p>
      <p v-if="loading" class="muted">Loading…</p>
      <p v-else-if="!users.length" class="muted">No users match that search.</p>

      <div v-else class="table-wrap">
        <table class="data">
          <thead>
            <tr>
              <th>Name</th>
              <th>Email</th>
              <th>Role</th>
              <th>Status</th>
              <th>Last sign-in</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="user in users" :key="user.id">
              <td>{{ user.full_name }}</td>
              <td>{{ user.email }}</td>
              <td>{{ user.role === UserRole.ADMIN ? 'Admin' : 'Sales executive' }}</td>
              <td>
                <span v-if="!user.is_active" class="badge badge--danger">Disabled</span>
                <span v-else-if="user.must_change_password" class="badge badge--warn">
                  Temp password
                </span>
                <span v-else class="badge badge--ok">Active</span>
              </td>
              <td>{{ user.last_login_at ? new Date(user.last_login_at).toLocaleString() : '—' }}</td>
              <td class="numeric">
                <button
                  class="btn btn--sm"
                  :disabled="user.id === auth.user?.id"
                  :title="
                    user.id === auth.user?.id
                      ? 'You cannot change your own access.'
                      : 'Grant or revoke areas of the app'
                  "
                  @click="openAccess(user)"
                >
                  Access
                </button>
                <button class="btn btn--sm" @click="resetPassword(user)">Reset password</button>
                <button class="btn btn--sm btn--danger" @click="toggleActive(user)">
                  {{ user.is_active ? 'Disable' : 'Enable' }}
                </button>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>

    <div v-if="editing" class="card stack">
      <div class="card__head">
        <h2>Access — {{ editing.full_name }}</h2>
        <button class="btn btn--sm" @click="closeAccess">Close</button>
      </div>
      <p class="muted small">
        Starts from the {{ editingIsExec ? 'sales executive' : 'admin' }} defaults. Only the
        boxes you change are stored, so anything left alone follows the role — including
        later changes to it.
        <span v-if="exceptionCount">
          <strong>{{ exceptionCount }}</strong> box(es) currently differ from the defaults.
        </span>
      </p>

      <p v-if="accessError" class="alert alert--error">{{ accessError }}</p>
      <p v-if="accessNotice" class="alert alert--ok">{{ accessNotice }}</p>
      <p v-if="!access" class="muted">Loading…</p>

      <template v-else>
        <div class="access">
          <div v-for="group in permissionGroups" :key="group.key" class="access__group">
            <p class="access__heading">
              {{ group.label }}
              <span v-if="groupState(group) !== 'locked'" class="access__bulk">
                <button type="button" @click="toggleGroup(group, true)">all</button>
                <button type="button" @click="toggleGroup(group, false)">none</button>
              </span>
            </p>
            <label
              v-for="entry in group.items"
              :key="entry.key"
              class="access__item"
              :class="{ 'access__item--locked': lockedReason(entry) }"
              :title="lockedReason(entry) || entry.key"
            >
              <input
                type="checkbox"
                :checked="chosen.has(entry.key)"
                :disabled="Boolean(lockedReason(entry))"
                @change="toggle(entry)"
              />
              <span>{{ entry.label }}</span>
              <span
                v-if="!access.default_permissions.includes(entry.key) && chosen.has(entry.key)"
                class="badge badge--ok"
              >
                granted
              </span>
              <span
                v-else-if="
                  access.default_permissions.includes(entry.key) && !chosen.has(entry.key)
                "
                class="badge badge--danger"
              >
                switched off
              </span>
              <span v-if="lockedReason(entry)" class="muted small">{{ lockedReason(entry) }}</span>
            </label>
          </div>
        </div>

        <label class="field">
          <span>Why (optional)</span>
          <input v-model="note" maxlength="200" placeholder="Covers the counter on Sundays" />
          <span class="field__hint">Recorded in the audit trail with the change.</span>
        </label>

        <div class="form-actions">
          <button class="btn btn--primary" :disabled="!dirty || saving" @click="saveAccess">
            {{ saving ? 'Saving…' : 'Save access' }}
          </button>
          <button class="btn" :disabled="saving" @click="restoreDefaults">
            Restore role defaults
          </button>
        </div>
      </template>
    </div>
  </section>
</template>

<style scoped>
.grow {
  flex: 1;
  min-width: 10rem;
}

td button + button {
  margin-left: 0.4rem;
}

.access {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(13rem, 1fr));
  gap: 1rem;
}

.access__heading {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 0.5rem;
  margin: 0 0 0.35rem;
  font-size: 0.72rem;
  font-weight: 700;
  letter-spacing: 0.06em;
  text-transform: uppercase;
  opacity: 0.6;
}

.access__bulk button {
  border: 0;
  background: none;
  padding: 0 0.15rem;
  font: inherit;
  font-size: 0.68rem;
  text-transform: none;
  letter-spacing: 0;
  text-decoration: underline;
  cursor: pointer;
  color: inherit;
}

.access__item {
  display: flex;
  align-items: center;
  gap: 0.45rem;
  padding: 0.2rem 0;
  cursor: pointer;
}

.access__item--locked {
  cursor: not-allowed;
  opacity: 0.6;
}
</style>
