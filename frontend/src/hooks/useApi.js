/**
 * The one place async request state is modelled.
 *
 * Every data-loading screen needs the same four things -- is it loading, did it
 * fail, what came back, and how do I retry -- so they are implemented once here
 * rather than re-derived per page with slightly different bugs.
 */

import { useCallback, useEffect, useRef, useState } from 'react'

/**
 * Run an async operation and track its state.
 *
 * @param {(opts: {signal: AbortSignal}) => Promise<any>} operation
 * @param {{enabled?: boolean, deps?: unknown[]}} [config]
 *   `enabled: false` holds the request back -- used when a parameter is not ready.
 *   `deps` re-runs the operation when it changes; its members must be JSON
 *   serialisable, which route params and ids are.
 * @returns {{data: any, error: Error|null, loading: boolean, reload: () => void}}
 */
export function useApi(operation, { enabled = true, deps = [] } = {}) {
  const [attempt, setAttempt] = useState(0)

  /**
   * Identifies the request the caller currently wants. Retrying bumps `attempt`;
   * changing `deps` changes the key directly. Serialising it keeps the effect's
   * dependency array a fixed length, which spreading `deps` would not.
   */
  const requestKey = JSON.stringify([attempt, deps])

  /**
   * The settled result, tagged with the key that produced it.
   *
   * `loading` is *derived* from whether the stored key still matches the wanted
   * one, rather than stored as a third piece of state. That removes the
   * set-loading-then-fetch dance from the effect body -- which causes cascading
   * renders -- and makes a stale result impossible to show: if the key does not
   * match, the data is not this request's.
   */
  const [result, setResult] = useState({ key: null, data: null, error: null })

  // Held in a ref so an inline arrow passed by the caller does not re-trigger the
  // effect on every render. Synced in an effect, never during render.
  const operationRef = useRef(operation)
  useEffect(() => {
    operationRef.current = operation
  })

  useEffect(() => {
    if (!enabled) return undefined

    const controller = new AbortController()
    let active = true

    operationRef
      .current({ signal: controller.signal })
      .then((data) => {
        if (active) setResult({ key: requestKey, data, error: null })
      })
      .catch((error) => {
        // An abort is a cancelled request, not a failure to report.
        if (!active || error?.name === 'AbortError') return
        setResult({ key: requestKey, data: null, error })
      })

    return () => {
      active = false
      controller.abort()
    }
  }, [enabled, requestKey])

  const reload = useCallback(() => setAttempt((n) => n + 1), [])

  if (!enabled) {
    return { data: null, error: null, loading: false, reload }
  }

  const settled = result.key === requestKey
  return {
    data: settled ? result.data : null,
    error: settled ? result.error : null,
    loading: !settled,
    reload,
  }
}
