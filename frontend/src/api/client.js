/**
 * Single place that knows how to talk to the backend.
 * In development, Vite proxies /api and /health to localhost:8000.
 */

const BASE = import.meta.env.VITE_API_BASE ?? ''

/**
 * The backend wraps every failure as {error: {code, message, details, request_id}}.
 * Switch on `code`, never on `message` -- wording changes, codes do not.
 */
export class ApiError extends Error {
  constructor({ status, code, message, details, requestId }) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.code = code
    this.details = details ?? {}
    this.requestId = requestId
  }
}

async function request(path, options = {}) {
  const res = await fetch(`${BASE}${path}`, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  })

  if (!res.ok) {
    // Parse the error envelope when there is one; fall back to the status line
    // for failures that never reached the application (proxy down, CORS block).
    let envelope = null
    try {
      envelope = await res.json()
    } catch {
      // not JSON -- leave it null
    }
    const err = envelope?.error
    throw new ApiError({
      status: res.status,
      code: err?.code ?? 'unreachable',
      message: err?.message ?? `${res.status} ${res.statusText}`,
      details: err?.details,
      requestId: err?.request_id ?? res.headers.get('X-Request-ID'),
    })
  }

  return res.json()
}

export const getServiceInfo = () => request('/')
export const getHealth = () => request('/api/health')
export const getCapabilities = () => request('/api/v1/system/capabilities')

// Stage 2+: createResearchRun, getRun, streamRunEvents, getClaims,
// getBenchmarkComparison.
