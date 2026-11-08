import client from './client'

// --- users ---------------------------------------------------------------

export function listUsers(params = {}) {
  return client.get('/users', { params }).then((r) => r.data)
}

export function createUser(payload) {
  return client.post('/users', payload).then((r) => r.data)
}

export function updateUser(id, payload) {
  return client.patch(`/users/${id}`, payload).then((r) => r.data)
}

export function resetUserPassword(id) {
  return client.post(`/users/${id}/reset-password`).then((r) => r.data)
}

export function deactivateUser(id) {
  return client.post(`/users/${id}/deactivate`).then((r) => r.data)
}

// --- per-user access -----------------------------------------------------

/** Every capability an admin can tick. Served so the two lists cannot drift. */
export function fetchPermissionCatalogue() {
  return client.get('/users/permissions/catalogue').then((r) => r.data)
}

export function fetchUserPermissions(id) {
  return client.get(`/users/${id}/permissions`).then((r) => r.data)
}

/** The full desired set, not a diff — two admins editing cannot interleave. */
export function setUserPermissions(id, permissions, note = null) {
  return client.put(`/users/${id}/permissions`, { permissions, note }).then((r) => r.data)
}

// --- settings & store ----------------------------------------------------

export function fetchSettings() {
  return client.get('/settings').then((r) => r.data)
}

export function updateSettings(payload) {
  return client.patch('/settings', payload).then((r) => r.data)
}

export function fetchStore() {
  return client.get('/store').then((r) => r.data)
}

export function updateStore(payload) {
  return client.patch('/store', payload).then((r) => r.data)
}
