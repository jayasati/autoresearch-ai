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

export const research = {
  /**
   * Run the whole pipeline for one topic and wait for the finished report.
   *
   * Synchronous on the backend: the response arrives after the run completes
   * (roughly 15-90+ seconds depending on depth), so callers should pass an
   * AbortSignal and show a real loading state.
   */
  start: (payload, options) => http.post('/api/v1/research/start', payload, options),

  /** Recent runs, newest first. */
  list: (options) => http.get('/api/v1/research', options),

  /** One run with its configuration, plan and counts. */
  get: (id, options) => http.get(`/api/v1/research/${id}`, options),

  /** The run's report texts: the immutable draft, and the final report if made. */
  report: (id, options) => http.get(`/api/v1/research/${id}/report`, options),

  /** Every source the run retrieved, labelled or not. */
  sources: (id, options) => http.get(`/api/v1/research/${id}/sources`, options),

  /** Stored claims, verdicts, citation outcomes, conflicts and labelled sources. */
  evidence: (id, options) => http.get(`/api/v1/research/${id}/evidence`, options),
}

export const evaluation = {
  /** The committed benchmark artifacts for one set, served verbatim. */
  benchmark: (options) => http.get('/api/v1/evaluation/benchmark', options),
}
