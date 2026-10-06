/**
 * The api client: base URL, error envelope parsing, correlation ids.
 */

import { beforeEach, describe, expect, it, vi } from 'vitest'

import { ApiError, API_BASE, http } from '../api/client.js'
import { system } from '../api/endpoints.js'
import { jsonResponse } from './utils.jsx'

describe('base URL', () => {
  it('is empty in development so Vite can proxy and CORS never applies', () => {
    expect(API_BASE).toBe('')
  })
})

describe('endpoint paths', () => {
  it('targets the unversioned health path', async () => {
    const fetchMock = vi.fn(async () => jsonResponse({ status: 'ok' }))
    vi.stubGlobal('fetch', fetchMock)
    await system.health()
    expect(fetchMock.mock.calls[0][0]).toBe('/api/health')
  })

  it('targets the versioned capabilities path', async () => {
    const fetchMock = vi.fn(async () => jsonResponse({ modes: [] }))
    vi.stubGlobal('fetch', fetchMock)
    await system.capabilities()
    expect(fetchMock.mock.calls[0][0]).toBe('/api/v1/system/capabilities')
  })

  it('does not export research operations, because the routes do not exist', async () => {
    const endpoints = await import('../api/endpoints.js')
    expect(endpoints.research).toBeUndefined()
    expect(endpoints.evidence).toBeUndefined()
    expect(endpoints.evaluation).toBeUndefined()
  })
})

describe('error envelope', () => {
  beforeEach(() => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        jsonResponse(
          {
            error: {
              code: 'not_found',
              message: 'No research run with id 42.',
              details: { run_id: 42 },
              request_id: 'ff00aa11',
            },
          },
          { status: 404 },
        ),
      ),
    )
  })

  it('becomes an ApiError carrying code, details and request id', async () => {
    const error = await http.get('/api/v1/research/42').catch((e) => e)
    expect(error).toBeInstanceOf(ApiError)
    expect(error.status).toBe(404)
    expect(error.code).toBe('not_found')
    expect(error.message).toBe('No research run with id 42.')
    expect(error.details).toEqual({ run_id: 42 })
    expect(error.requestId).toBe('ff00aa11')
  })

  it('exposes isNotImplemented for routes that do not exist yet', async () => {
    const error = await http.get('/api/v1/research').catch((e) => e)
    expect(error.isNotImplemented).toBe(true)
    expect(error.isUnreachable).toBe(false)
  })
})

describe('responses without an envelope', () => {
  it('falls back to the status line and reads the header for the request id', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => ({
        ok: false,
        status: 502,
        statusText: 'Bad Gateway',
        headers: { get: (name) => (name === 'X-Request-ID' ? 'from-header' : null) },
        json: async () => {
          throw new Error('not JSON')
        },
      })),
    )
    const error = await http.get('/api/health').catch((e) => e)
    expect(error.code).toBe('http_error')
    expect(error.message).toBe('502 Bad Gateway')
    expect(error.requestId).toBe('from-header')
  })
})

describe('network failure', () => {
  beforeEach(() => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => {
        throw new TypeError('Failed to fetch')
      }),
    )
  })

  it('is reported as unreachable, not as a server error', async () => {
    const error = await http.get('/api/health').catch((e) => e)
    expect(error.code).toBe('unreachable')
    expect(error.isUnreachable).toBe(true)
    expect(error.status).toBe(0)
  })

  it('says how to fix the usual cause', async () => {
    const error = await http.get('/api/health').catch((e) => e)
    expect(error.message).toMatch(/is it running on port 8000/i)
  })
})

describe('aborts', () => {
  it('propagate rather than being reported as failures', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => {
        const abort = new Error('aborted')
        abort.name = 'AbortError'
        throw abort
      }),
    )
    const error = await http.get('/api/health').catch((e) => e)
    expect(error.name).toBe('AbortError')
    expect(error).not.toBeInstanceOf(ApiError)
  })
})

describe('request bodies', () => {
  it('are JSON encoded with the right method and header', async () => {
    const fetchMock = vi.fn(async () => jsonResponse({ ok: true }))
    vi.stubGlobal('fetch', fetchMock)
    await http.post('/api/v1/research', { topic: 'RAG', mode: 'hybrid' })

    const [, init] = fetchMock.mock.calls[0]
    expect(init.method).toBe('POST')
    expect(init.headers['Content-Type']).toBe('application/json')
    expect(JSON.parse(init.body)).toEqual({ topic: 'RAG', mode: 'hybrid' })
  })

  it('are omitted entirely for GET', async () => {
    const fetchMock = vi.fn(async () => jsonResponse({}))
    vi.stubGlobal('fetch', fetchMock)
    await http.get('/api/health')
    expect(fetchMock.mock.calls[0][1].body).toBeUndefined()
  })
})

describe('204 responses', () => {
  it('resolve to null instead of failing to parse an empty body', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => ({
        ok: true,
        status: 204,
        statusText: 'No Content',
        headers: { get: () => null },
        json: async () => {
          throw new Error('no body')
        },
      })),
    )
    await expect(http.delete('/api/v1/research/1')).resolves.toBeNull()
  })
})
