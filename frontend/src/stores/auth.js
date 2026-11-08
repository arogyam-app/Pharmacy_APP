import { defineStore } from 'pinia'

import * as authApi from '@/api/auth'
import { setAccessToken, setSessionLostHandler, setTokenRefresher } from '@/api/client'
import { UserRole } from '@/constants'

// The session token is NOT kept here any more — it lives in an httpOnly
// cookie the browser sends by itself, out of reach of any script. What is
// remembered locally is the email, which is an identifier and not a
// secret, so the counter does not retype it every morning. The password
// is never stored, in any form, anywhere.
const EMAIL_KEY = 'pharmacy.last_email'
const REMEMBER_KEY = 'pharmacy.remember_me'

// The refresh token is single-use -- the server revokes the presented one
// the instant it issues a new one (app/api/v1/auth.py:refresh). A page that
// fires several requests at once all hits this the same moment the access
// token expires, and without this guard each 401 would call renew()
// independently: the first refresh succeeds and rotates the cookie, the
// rest arrive holding the now-revoked one and get rejected -- and a
// rejection's cookie-clearing header can land *after* the good one and wipe
// it out, logging in someone who was actively using the app. One in-flight
// refresh, shared by every concurrent caller, is what a "many times mid
// session" logout turned out to be.
let refreshInFlight = null

export const useAuthStore = defineStore('auth', {
  state: () => ({
    user: null,
    store: null,
    permissions: [],
    // True once a session has been established in this tab. The cookie
    // itself is invisible to script, so this is the only signal we have.
    signedIn: false,
    rememberedEmail: localStorage.getItem(EMAIL_KEY) || '',
    rememberMe: localStorage.getItem(REMEMBER_KEY) === '1',
    // False until the initial session restore has finished, so guards do not
    // bounce a signed-in user to /login on a page refresh.
    ready: false,
  }),

  getters: {
    isAuthenticated: (state) => Boolean(state.user),
    isAdmin: (state) => state.user?.role === UserRole.ADMIN,
    mustChangePassword: (state) => Boolean(state.user?.must_change_password),
    // An admin can switch any of these off for one person, and the server
    // enforces the same list — so this only decides what is worth showing.
    // `canAny` is for a screen reachable by more than one route.
    can: (state) => (permission) => state.permissions.includes(permission),
    canAny: (state) => (...permissions) =>
      permissions.some((p) => state.permissions.includes(p)),
  },

  actions: {
    /** Remember who signs in here — the email only, never the password. */
    _rememberEmail(email, remember) {
      this.rememberMe = remember
      localStorage.setItem(REMEMBER_KEY, remember ? '1' : '0')
      if (remember) {
        this.rememberedEmail = email
        localStorage.setItem(EMAIL_KEY, email)
      } else {
        this.rememberedEmail = ''
        localStorage.removeItem(EMAIL_KEY)
      }
    },

    _applySession({ access_token }) {
      setAccessToken(access_token)
      this.signedIn = true
    },

    async login(email, password, rememberMe = false) {
      const data = await authApi.login(email, password, rememberMe)
      this._rememberEmail(email, rememberMe)
      this._applySession(data)
      this.user = data.user
      await this.loadProfile()
      return data.user
    },

    async loadProfile() {
      const me = await authApi.fetchMe()
      this.user = me.user
      this.store = me.store
      this.permissions = me.permissions
      return me
    },

    /** Called by the axios interceptor when an access token expires. */
    async renew() {
      // Nothing to check first: the cookie either travels with the request
      // or it does not, and only the server can tell.
      //
      // One request in flight at a time -- see the note on refreshInFlight
      // above. Every concurrent caller awaits the same promise and applies
      // the same result, rather than each rotating the cookie out from
      // under the others.
      if (!refreshInFlight) {
        refreshInFlight = authApi.refreshSession().finally(() => {
          refreshInFlight = null
        })
      }
      this._applySession(await refreshInFlight)
    },

    /** Restore a session from the httpOnly cookie on app start. */
    async restore() {
      try {
        await this.renew()
        await this.loadProfile()
        return true
      } catch {
        this.clear()
        return false
      } finally {
        this.ready = true
      }
    },

    async logout() {
      try {
        await authApi.logout()
      } catch {
        // Signing out locally matters more than the server acknowledging it.
      } finally {
        this.clear()
      }
    },

    clear() {
      this.user = null
      this.store = null
      this.permissions = []
      this.signedIn = false
      setAccessToken(null)
    },
  },
})

/** Wires the store into the axios interceptors. Called once from main.js. */
export function installAuthInterceptors(router) {
  const auth = useAuthStore()
  setTokenRefresher(() => auth.renew())
  setSessionLostHandler(() => {
    auth.clear()
    if (router.currentRoute.value.meta.requiresAuth !== false) {
      router.push({ name: 'login', query: { expired: '1' } })
    }
  })
  return auth
}
