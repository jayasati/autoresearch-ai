/**
 * Test helpers.
 *
 * `renderApp` mounts the real App inside a MemoryRouter so navigation and the
 * layout are exercised, not stubbed. Every test controls the backend by stubbing
 * `fetch`, which keeps the api client itself under test rather than mocked out.
 */

import { render } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { vi } from 'vitest'

import App from '../App.jsx'

export function renderApp(route = '/') {
  return render(
    <MemoryRouter initialEntries={[route]}>
      <App />
    </MemoryRouter>,
  )
}

/** A successful JSON response. */
export function jsonResponse(body, { status = 200, headers = {} } = {}) {
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: 'OK',
    headers: { get: (name) => headers[name] ?? null },
    json: async () => body,
  }
}

export const HEALTH = { status: 'ok', version: '0.1.0', environment: 'development' }

export const CAPABILITIES = {
  modes: ['model_only', 'hybrid', 'search_grounded'],
  integrations: {
    openai: false,
    tavily: false,
    semantic_scholar: true,
    postgres: true,
    chromadb: true,
  },
  missing_credentials: ['OPENAI_API_KEY', 'TAVILY_API_KEY'],
  implemented: [],
}

/** Backend up: answers health and capabilities, 404s everything else. */
export function stubHealthyBackend(overrides = {}) {
  const routes = {
    '/api/health': HEALTH,
    '/api/v1/system/capabilities': CAPABILITIES,
    ...overrides,
  }
  const fetchMock = vi.fn(async (url) => {
    const path = String(url)
    const match = Object.keys(routes).find((key) => path.endsWith(key))
    if (match) return jsonResponse(routes[match])
    return jsonResponse(
      { error: { code: 'not_found', message: 'Not Found', details: {}, request_id: 'req-test' } },
      { status: 404 },
    )
  })
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

/** Backend down: fetch rejects, as it does when nothing is listening. */
export function stubOfflineBackend() {
  const fetchMock = vi.fn(async () => {
    throw new TypeError('Failed to fetch')
  })
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

/** Backend reachable but erroring, with a full error envelope. */
export function stubFailingBackend(status = 500, code = 'internal_error') {
  const fetchMock = vi.fn(async () =>
    jsonResponse(
      {
        error: {
          code,
          message: 'An unexpected error occurred. The incident has been logged.',
          details: {},
          request_id: 'abc123def456',
        },
      },
      { status, headers: { 'X-Request-ID': 'abc123def456' } },
    ),
  )
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

/** A request that never settles, for asserting the loading state. */
export function stubPendingBackend() {
  const fetchMock = vi.fn(() => new Promise(() => {}))
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}
