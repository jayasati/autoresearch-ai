/**
 * Single place that knows how to talk to the backend.
 * In development, Vite proxies /api and /health to localhost:8000.
 */

const BASE = import.meta.env.VITE_API_BASE ?? ''

async function request(path, options = {}) {
  const res = await fetch(`${BASE}${path}`, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  })
  if (!res.ok) {
    throw new Error(`${res.status} ${res.statusText}`)
  }
  return res.json()
}

export const getHealth = () => request('/health')
export const getCapabilities = () => request('/api/v1/system/capabilities')

// Stage 2+: createResearchRun, getRun, streamRunEvents, getClaims,
// getBenchmarkComparison.
