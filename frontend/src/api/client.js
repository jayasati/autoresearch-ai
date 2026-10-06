/**
 * HTTP transport.
 *
 * The only module that knows how to reach the backend. Everything above it calls
 * named operations from `endpoints.js` instead of constructing URLs, so the base
 * URL, the error contract and the correlation header are each handled in exactly
 * one place.
 */

/**
 * Where the backend lives.
 *
 * Empty in development: `vite.config.js` proxies `/api` to localhost:8000, which
 * keeps the dev build free of hard-coded hosts and avoids CORS entirely. In a
 * deployed build, set `VITE_API_BASE` to the backend's origin.
 *
 * Only `VITE_*` variables are exposed to the browser by Vite, which is why the
 * name is prefixed -- it is a deliberate signal that this value is public.
 */
export const API_BASE = import.meta.env?.VITE_API_BASE ?? ''

/**
 * A failed request.
 *
 * The backend wraps every failure as
 * `{error: {code, message, details, request_id}}`. Switch on `code`, never on
 * `message`: codes are stable, wording is not.
 *
 * `requestId` is the correlation id. It appears in every backend log line for
 * that request, so showing it to a user turns "it broke" into something
 * diagnosable.
 */
export class ApiError extends Error {
  constructor({ status, code, message, details, requestId }) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.code = code
    this.details = details ?? {}
    this.requestId = requestId ?? null
  }

  /** True when the request never reached the application at all. */
  get isUnreachable() {
    return this.code === 'unreachable'
  }

  /** True when the endpoint does not exist yet -- expected during this build. */
  get isNotImplemented() {
    return this.status === 404 && this.code === 'not_found'
  }
}

async function request(path, { method = 'GET', body, signal, headers } = {}) {
  let response
  try {
    response = await fetch(`${API_BASE}${path}`, {
      method,
      signal,
      headers: { 'Content-Type': 'application/json', ...headers },
      body: body === undefined ? undefined : JSON.stringify(body),
    })
  } catch (cause) {
    // fetch rejects only for network-level failures: the server is down, DNS
    // failed, the request was aborted. There is no response and no envelope.
    if (cause?.name === 'AbortError') throw cause
    throw new ApiError({
      status: 0,
      code: 'unreachable',
      message: 'Could not reach the backend. Is it running on port 8000?',
      details: { cause: String(cause?.message ?? cause) },
    })
  }

  if (!response.ok) {
    // Parse the error envelope when there is one; fall back to the status line
    // for failures that never reached the application (a proxy error page).
    let envelope = null
    try {
      envelope = await response.json()
    } catch {
      envelope = null
    }
    const error = envelope?.error
    throw new ApiError({
      status: response.status,
      code: error?.code ?? 'http_error',
      message: error?.message ?? `${response.status} ${response.statusText}`,
      details: error?.details,
      requestId: error?.request_id ?? response.headers.get('X-Request-ID'),
    })
  }

  if (response.status === 204) return null
  return response.json()
}

export const http = {
  get: (path, options) => request(path, { ...options, method: 'GET' }),
  post: (path, body, options) => request(path, { ...options, method: 'POST', body }),
  patch: (path, body, options) => request(path, { ...options, method: 'PATCH', body }),
  delete: (path, options) => request(path, { ...options, method: 'DELETE' }),
}
