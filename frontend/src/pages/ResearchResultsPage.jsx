/**
 * Research Results.
 *
 * Stored runs read back from the database: the run's header, its plan, and the
 * report text verbatim. When a run was finalized the verification-aware final
 * report is shown and labelled as such; otherwise the immutable draft is, also
 * labelled — the page never silently picks one.
 */

import { useState } from 'react'

import { research } from '../api/endpoints.js'
import RunPicker from '../components/RunPicker.jsx'
import { Badge, Card, PageHeader, StatTile } from '../components/ui/primitives.jsx'
import { AsyncBoundary } from '../components/ui/states.jsx'
import { useApi } from '../hooks/useApi.js'

const STATUS_TONES = { completed: 'positive', failed: 'negative', running: 'warning' }

function duration(run) {
  if (!run.started_at || !run.completed_at) return '—'
  const seconds = (new Date(run.completed_at) - new Date(run.started_at)) / 1000
  return `${seconds.toFixed(1)}s`
}

export default function ResearchResultsPage() {
  const [runId, setRunId] = useState(null)

  const detail = useApi(({ signal }) => research.get(runId, { signal }), {
    enabled: Boolean(runId),
    deps: [runId],
  })
  const report = useApi(({ signal }) => research.report(runId, { signal }), {
    enabled: Boolean(runId),
    deps: [runId],
  })

  const run = detail.data?.run
  const reportMissing = report.error?.status === 404

  return (
    <>
      <PageHeader
        title="Research Results"
        description="Generated reports, with every claim traceable to a source."
      />

      <Card title="Pick a run">
        <RunPicker value={runId} onChange={setRunId} />
      </Card>

      {runId && (
        <AsyncBoundary
          loading={detail.loading}
          error={detail.error}
          onRetry={detail.reload}
          loadingMessage="Loading the run…"
        >
          {run && (
            <Card
              title={run.topic}
              description={`Run ${runId}`}
              action={<Badge tone={STATUS_TONES[run.status] ?? 'neutral'}>{run.status}</Badge>}
            >
              <div className="grid grid--stats">
                <StatTile label="Mode" value={run.configuration.mode} />
                <StatTile label="Model" value={run.configuration.model} />
                <StatTile label="Duration" value={duration(run)} />
                <StatTile label="Sources" value={detail.data.source_count} />
                <StatTile label="Claims" value={detail.data.claim_count} />
                <StatTile label="Conflicts" value={detail.data.conflict_count} />
              </div>
              {run.error_message && (
                <p className="note note--warn">
                  Failed with <code>{run.error_code}</code>: {run.error_message}
                </p>
              )}
              {run.subquestions.length > 0 && (
                <>
                  <p className="field__label">Plan</p>
                  <ol className="planned__list">
                    {run.subquestions.map((q) => (
                      <li key={q.id}>{q.text}</li>
                    ))}
                  </ol>
                </>
              )}
            </Card>
          )}
        </AsyncBoundary>
      )}

      {runId &&
        (reportMissing ? (
          <Card title="Report">
            <p className="state__message">
              This run has no report — it failed before synthesis, so there is honestly nothing to
              show for it beyond the error above.
            </p>
          </Card>
        ) : (
          <AsyncBoundary
            loading={report.loading}
            error={report.error}
            onRetry={report.reload}
            loadingMessage="Loading the report…"
          >
            {report.data && (
              <Card
                title={report.data.title ?? 'Report'}
                description={
                  report.data.finalized
                    ? 'The verification-aware final report, exactly as stored.'
                    : 'The draft report, exactly as stored. This run was never finalized.'
                }
                action={
                  <Badge tone={report.data.finalized ? 'positive' : 'neutral'}>
                    {report.data.finalized ? 'final report' : 'draft'}
                  </Badge>
                }
              >
                <div className="report-body">
                  {report.data.finalized ? report.data.final_markdown : report.data.markdown}
                </div>
              </Card>
            )}
          </AsyncBoundary>
        ))}
    </>
  )
}
