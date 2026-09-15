const TOKEN_KEY = 'token'
const LAST_ACTIVITY_KEY = 'lastActivityAt'
const ACTIVITY_WRITE_INTERVAL_MS = 1000

export const INACTIVITY_TIMEOUT_MS = 60 * 60 * 1000

export function getToken() {
  return localStorage.getItem(TOKEN_KEY)
}

export function storeToken(token: string) {
  localStorage.setItem(TOKEN_KEY, token)
}

export function markActivity(now = Date.now(), force = false) {
  if (!getToken()) return

  const previous = Number(localStorage.getItem(LAST_ACTIVITY_KEY) || 0)
  if (force || !previous || now - previous >= ACTIVITY_WRITE_INTERVAL_MS) {
    localStorage.setItem(LAST_ACTIVITY_KEY, String(now))
  }
}

export function startSession(token: string) {
  storeToken(token)
  markActivity(Date.now(), true)
}

export function updateSessionToken(token: string) {
  storeToken(token)
  ensureActivityTimestamp()
}

export function clearSession() {
  localStorage.removeItem(TOKEN_KEY)
  localStorage.removeItem(LAST_ACTIVITY_KEY)
}

export function redirectToLogin() {
  if (window.location.pathname !== '/login') {
    window.location.assign('/login')
  }
}

export function clearSessionAndRedirect() {
  clearSession()
  redirectToLogin()
}

export function ensureActivityTimestamp(now = Date.now()) {
  if (!getToken()) return

  const previous = Number(localStorage.getItem(LAST_ACTIVITY_KEY) || 0)
  if (!previous) {
    localStorage.setItem(LAST_ACTIVITY_KEY, String(now))
  }
}

export function isSessionIdleTimedOut(now = Date.now()) {
  if (!getToken()) return false

  const lastActivityAt = Number(localStorage.getItem(LAST_ACTIVITY_KEY) || 0)
  if (!lastActivityAt) return false

  return now - lastActivityAt >= INACTIVITY_TIMEOUT_MS
}

export function hasActiveSession() {
  if (!getToken()) return false

  ensureActivityTimestamp()
  if (isSessionIdleTimedOut()) {
    clearSession()
    return false
  }

  return true
}
