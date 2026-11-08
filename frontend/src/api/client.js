import axios from 'axios'

/**
 * Single axios instance for the whole app.
 *
 * Every backend error arrives as {detail: {code, message, field}} (see
 * docs/API.md). The response interceptor flattens that into a predictable
 * shape so views never have to dig through err.response.data.detail.
 */
/**
 * How long a bulk upload may take before the browser gives up.
 *
 * The default 20 s is right for everything a counter does — a sale that hangs
 * for twenty seconds is broken and should say so. A master-data import is the
 * opposite: 20,000 formulations is around two and a half minutes of honest
 * work, and cutting it off mid-way leaves the operator staring at a network
 * error while the server quietly finishes the job. Kept per-request rather
 * than raised globally, so slow imports do not make a slow till acceptable.
 *
 * Sized against `MAX_IMPORT_ROWS` in backend/app/utils/spreadsheet.py: 50,000
 * rows at ~130/s is about six minutes, so fifteen leaves real headroom.
 */
export const IMPORT_TIMEOUT_MS = 15 * 60 * 1000

const client = axios.create({
  baseURL: import.meta.env.VITE_API_BASE_URL || '/api/v1',
  timeout: 20000,
  headers: { 'Content-Type': 'application/json' },
  // The session lives in an httpOnly cookie the browser attaches itself.
  // Script can neither read it nor forge it, which is the point.
  withCredentials: true,
})

// The access token is kept in memory only — never localStorage, where any
// injected script could read it. The refresh token is not held here at
// all any more: it lives in an httpOnly cookie set by /auth/login, so a
// compromised script cannot lift a week-long session off this machine.
let accessToken = null
let tokenRefresher = null
let onSessionLost = null

export function setAccessToken(token) {
  accessToken = token
}

/** Registered by the auth store; avoids a circular import. */
export function setTokenRefresher(fn) {
  tokenRefresher = fn
}

export function setSessionLostHandler(fn) {
  onSessionLost = fn
}

client.interceptors.request.use((config) => {
  if (accessToken) {
    config.headers.Authorization = `Bearer ${accessToken}`
  }
  return config
})

export class ApiError extends Error {
  constructor({ code, message, field, status, errors }) {
    super(message)
    this.name = 'ApiError'
    this.code = code
    this.field = field
    this.status = status
    this.errors = errors || []
  }
}

client.interceptors.response.use(
  (response) => response,
  async (error) => {
    const original = error.config || {}

    if (error.response) {
      const detail = error.response.data?.detail
      const payload =
        detail && typeof detail === 'object'
          ? detail
          : { code: 'HTTP_ERROR', message: String(detail || error.message) }

      // A 404 with no field and FastAPI's bare "Not Found" body is a missing
      // *route*, not a missing row — almost always an API process still
      // running code older than the SPA. Say that, instead of letting a
      // screen report "Not found" about data that is perfectly present.
      if (
        error.response.status === 404 &&
        !payload.field &&
        /^not found$/i.test(payload.message || '')
      ) {
        payload.code = 'ENDPOINT_MISSING'
        payload.message =
          `The API has no ${original.method?.toUpperCase() || 'GET'} ${original.url} endpoint. ` +
          'The server is probably running an older build — restart uvicorn.'
      }

      // One silent retry: swap an expired access token for a fresh one.
      const canRetry =
        error.response.status === 401 &&
        payload.code === 'TOKEN_EXPIRED' &&
        tokenRefresher &&
        !original._retried &&
        !original._skipAuthRefresh

      if (canRetry) {
        original._retried = true
        try {
          await tokenRefresher()
          return client(original)
        } catch {
          onSessionLost?.()
        }
      } else if (error.response.status === 401 && !original._skipAuthRefresh) {
        onSessionLost?.()
      }

      return Promise.reject(new ApiError({ ...payload, status: error.response.status }))
    }

    if (error.code === 'ECONNABORTED') {
      return Promise.reject(
        new ApiError({ code: 'TIMEOUT', message: 'The server took too long to respond.' }),
      )
    }
    return Promise.reject(
      new ApiError({
        code: 'NETWORK_ERROR',
        message: 'Cannot reach the server. Is the API running on port 8000?',
      }),
    )
  },
)

export default client
