import client from './client'

/**
 * Signing in.
 *
 * No password is stored anywhere by any of this. `remember_me` asks the
 * server to give the httpOnly session cookie a lifetime so the browser
 * stays signed in after it is closed; without it the cookie dies with the
 * browser, which is what a shared counter machine wants.
 */

export function login(email, password, rememberMe = false) {
  // Skip the refresh interceptor: a 401 here means bad credentials.
  return client
    .post(
      '/auth/login',
      { email, password, remember_me: rememberMe },
      { _skipAuthRefresh: true },
    )
    .then((r) => r.data)
}

/** The cookie travels on its own; there is no token for us to pass. */
export function refreshSession() {
  return client.post('/auth/refresh', {}, { _skipAuthRefresh: true }).then((r) => r.data)
}

export function logout() {
  return client.post('/auth/logout', {}).then((r) => r.data)
}

export function fetchMe() {
  return client.get('/auth/me').then((r) => r.data)
}

export function changePassword(currentPassword, newPassword) {
  return client
    .post('/auth/change-password', {
      current_password: currentPassword,
      new_password: newPassword,
    })
    .then((r) => r.data)
}
