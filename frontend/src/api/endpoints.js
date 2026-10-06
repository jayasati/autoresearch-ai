/**
 * Named backend operations, grouped by domain.
 *
 * Components call these, never `fetch` and never `http` directly. When a path
 * changes, it changes here and nowhere else.
 *
 * Only operations the backend actually serves are exported. The commented groups
 * below are the real stage boundaries -- adding a function that calls a route
 * that does not exist would produce a confusing 404 at the UI layer instead of an
 * honest "not built yet".
 */

import { http } from './client.js'

export const system = {
  /** Service name, version, environment and build stage. */
  info: (options) => http.get('/', options),

  /** Liveness. Unversioned on purpose: it must not move when the API does. */
  health: (options) => http.get('/api/health', options),

  /** Which integrations are configured, and which credentials are missing. */
  capabilities: (options) => http.get('/api/v1/system/capabilities', options),
}

// --- Not available yet -------------------------------------------------------
//
// export const research = {
//   create: (payload) => http.post('/api/v1/research', payload),   // stage 3
//   get: (id) => http.get(`/api/v1/research/${id}`),               // stage 3
//   list: () => http.get('/api/v1/research'),                      // stage 3
// }
//
// export const sources = {
//   listForRun: (runId) => http.get(`/api/v1/research/${runId}/sources`),   // stage 4
// }
//
// export const evidence = {
//   claims: (runId) => http.get(`/api/v1/research/${runId}/claims`),        // stage 5
//   conflicts: (runId) => http.get(`/api/v1/research/${runId}/conflicts`),  // stage 5
// }
//
// export const evaluation = {
//   metrics: (runId) => http.get(`/api/v1/research/${runId}/metrics`),      // stage 6
//   benchmark: () => http.get('/api/v1/evaluation/benchmark'),              // stage 6
// }
