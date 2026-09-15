/**
 * Format UTC timestamps from the server in America/Los_Angeles.
 */

const APP_TIME_ZONE = 'America/Los_Angeles'

const DATE_FMT = new Intl.DateTimeFormat(undefined, {
  year: 'numeric',
  month: 'short',
  day: 'numeric',
  hour: 'numeric',
  minute: '2-digit',
  hour12: true,
  timeZone: APP_TIME_ZONE,
  timeZoneName: 'short',
})

const SHORT_FMT = new Intl.DateTimeFormat(undefined, {
  month: 'short',
  day: 'numeric',
  hour: 'numeric',
  minute: '2-digit',
  hour12: true,
  timeZone: APP_TIME_ZONE,
  timeZoneName: 'short',
})

function parseServerDate(value: string | Date): Date {
  if (value instanceof Date) return value
  const normalized = /(?:Z|[+-]\d{2}:?\d{2})$/.test(value) ? value : `${value}Z`
  return new Date(normalized)
}

/** Full date + time in America/Los_Angeles, e.g. "Aug 24, 2026, 8:18 AM PDT" */
export function formatDateTime(value: string | Date | null | undefined): string {
  if (!value) return '—'
  const d = parseServerDate(value)
  if (isNaN(d.getTime())) return '—'
  return DATE_FMT.format(d)
}

/** Short date + time in America/Los_Angeles, e.g. "Aug 24, 8:18 AM PDT" */
export function formatShort(value: string | Date | null | undefined): string {
  if (!value) return '—'
  const d = parseServerDate(value)
  if (isNaN(d.getTime())) return '—'
  return SHORT_FMT.format(d)
}

/** Relative time, e.g. "3 minutes ago", "just now" */
export function formatRelative(value: string | Date | null | undefined): string {
  if (!value) return '—'
  const d = parseServerDate(value)
  if (isNaN(d.getTime())) return '—'
  const diffMs = Date.now() - d.getTime()
  const diffSec = Math.floor(diffMs / 1000)
  if (diffSec < 5)   return 'just now'
  if (diffSec < 60)  return `${diffSec}s ago`
  const diffMin = Math.floor(diffSec / 60)
  if (diffMin < 60)  return `${diffMin}m ago`
  const diffHr = Math.floor(diffMin / 60)
  if (diffHr < 24)   return `${diffHr}h ago`
  const diffDay = Math.floor(diffHr / 24)
  return `${diffDay}d ago`
}

/** Returns the app display timezone name. */
export function localTimezone(): string {
  return APP_TIME_ZONE
}

/** Convert a datetime-local input value into an explicit UTC ISO timestamp. */
export function datetimeLocalToUtcIso(value: string): string | null {
  if (!value) return null
  const date = new Date(value)
  if (isNaN(date.getTime())) return null
  return date.toISOString()
}
