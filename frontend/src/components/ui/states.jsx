/**
 * The three states every data view needs, plus the honest placeholder.
 *
 * Kept in one file because they are one idea: what to show when there is no
 * content to show. Scattering them invites four slightly different spinners.
 */

import { AlertTriangle, Inbox, Loader2, RefreshCw, Wrench } from 'lucide-react'

/** Spinner. `label` is for screen readers, not decoration. */
export function Spinner({ label = 'Loading' }) {
  return <Loader2 className="spinner" size={20} aria-label={label} role="status" />
}

/**
 * Loading state.
 *
 * `aria-busy` and `role="status"` matter: without them a screen reader user gets
 * silence while the page waits.
 */
export function LoadingState({ message = 'Loading…' }) {
  return (
    <div className="state state--loading" role="status" aria-busy="true">
      <Spinner label={message} />
      <p className="state__message">{message}</p>
    </div>
  )
}

/**
 * Error state.
 *
 * Shows the backend's stable `code` and the correlation id when present, because
 * "something went wrong" is not a diagnosis. The id appears in the server log for
 * the same request, which is what makes a bug report actionable.
 */
export function ErrorState({ error, onRetry, title = 'Something went wrong' }) {
  const code = error?.code
  const requestId = error?.requestId
  const unreachable = error?.isUnreachable

  return (
    <div className="state state--error" role="alert">
      <AlertTriangle className="state__icon" size={22} aria-hidden="true" />
      <div className="state__body">
        <p className="state__title">{unreachable ? 'Backend not reachable' : title}</p>
        <p className="state__message">{error?.message ?? 'Unknown error.'}</p>

        {unreachable && (
          <p className="state__hint">
            Start it with <code>uvicorn app.main:app --reload --port 8000</code> from the{' '}
            <code>backend/</code> directory.
          </p>
        )}

        {(code || requestId) && (
          <dl className="state__meta">
            {code && (
              <>
                <dt>code</dt>
                <dd>
                  <code>{code}</code>
                </dd>
              </>
            )}
            {requestId && (
              <>
                <dt>request id</dt>
                <dd>
                  <code>{requestId}</code>
                </dd>
              </>
            )}
          </dl>
        )}

        {onRetry && (
          <button type="button" className="btn btn--ghost" onClick={onRetry}>
            <RefreshCw size={14} aria-hidden="true" />
            Try again
          </button>
        )}
      </div>
    </div>
  )
}

/**
 * Empty state.
 *
 * Distinct from the placeholder below: empty means the feature works but there is
 * nothing in it yet.
 */
export function EmptyState({ title, message, action }) {
  return (
    <div className="state state--empty">
      <Inbox className="state__icon" size={22} aria-hidden="true" />
      <div className="state__body">
        <p className="state__title">{title}</p>
        {message && <p className="state__message">{message}</p>}
        {action}
      </div>
    </div>
  )
}

/**
 * Not-implemented placeholder.
 *
 * Deliberately not an empty state. Empty says "nothing here yet"; this says "this
 * feature does not exist yet", names the stage it arrives in, and never shows
 * sample output that could be mistaken for a real result.
 */
export function NotImplemented({ feature, stage, children }) {
  return (
    <div className="state state--todo" role="note">
      <Wrench className="state__icon" size={22} aria-hidden="true" />
      <div className="state__body">
        <p className="state__title">{feature} is not implemented yet</p>
        <p className="state__message">
          Arrives in <strong>{stage}</strong>. Nothing on this page is generated output, and no
          sample results are shown.
        </p>
        {children}
      </div>
    </div>
  )
}

/**
 * Renders the right state for an async result.
 *
 * `loading` and `error` are mutually exclusive by construction in `useApi`, which
 * clears the error when a retry begins. Empty is decided by the caller via
 * `isEmpty`, because only the caller knows what empty means for its own data.
 */
export function AsyncBoundary({
  loading,
  error,
  onRetry,
  isEmpty = false,
  empty = null,
  loadingMessage,
  children,
}) {
  if (error) return <ErrorState error={error} onRetry={onRetry} />
  if (loading) return <LoadingState message={loadingMessage} />
  if (isEmpty) return empty
  return children
}
