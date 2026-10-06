/**
 * Backend reachability and configuration, shared by the layout and the dashboard.
 */

import { system } from '../api/endpoints.js'
import { useApi } from './useApi.js'

/** Liveness only -- cheap, and the one call that proves the backend is up. */
export function useHealth() {
  return useApi(({ signal }) => system.health({ signal }))
}

/** Integration status and the research modes the backend declares. */
export function useCapabilities() {
  return useApi(({ signal }) => system.capabilities({ signal }))
}

/** Service name, version, environment and the build stage it reports. */
export function useServiceInfo() {
  return useApi(({ signal }) => system.info({ signal }))
}
