/**
 * The run selector shared by every page that reads one stored run.
 *
 * Owns the list request so Sources, Evidence Audit and Research Results do not
 * each re-implement it. Selection is lifted to the parent, which fetches whatever
 * view of the chosen run it needs.
 */

import { useEffect } from 'react'

import { research } from '../api/endpoints.js'
import { AsyncBoundary, EmptyState } from './ui/states.jsx'
import { useApi } from '../hooks/useApi.js'

function describeRun(run) {
  const when = run.created_at ? new Date(run.created_at).toLocaleString() : ''
  const topic = run.topic.length > 70 ? `${run.topic.slice(0, 70)}…` : run.topic
  return `${topic} — ${run.mode}, ${run.status}${when ? `, ${when}` : ''}`
}

export default function RunPicker({ value, onChange, emptyMessage }) {
  const { data: runs, error, loading, reload } = useApi(({ signal }) => research.list({ signal }))

  // Pre-select the newest run once the list arrives, so each page shows real
  // data immediately instead of an unexplained blank choice.
  useEffect(() => {
    if (!value && runs?.length) onChange(runs[0].id)
  }, [value, runs, onChange])

  return (
    <AsyncBoundary
      loading={loading}
      error={error}
      onRetry={reload}
      loadingMessage="Loading runs…"
      isEmpty={runs?.length === 0}
      empty={
        <EmptyState
          title="No research runs yet"
          message={emptyMessage ?? 'Start one from the New Research page and it will appear here.'}
        />
      }
    >
      <div className="field">
        <label className="field__label" htmlFor="run-picker">
          Research run
        </label>
        <select
          id="run-picker"
          className="field__input"
          value={value ?? ''}
          onChange={(event) => onChange(event.target.value)}
        >
          {(runs ?? []).map((run) => (
            <option key={run.id} value={run.id}>
              {describeRun(run)}
            </option>
          ))}
        </select>
      </div>
    </AsyncBoundary>
  )
}
