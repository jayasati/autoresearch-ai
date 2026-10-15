/**
 * Sources.
 *
 * Every web page and paper a run retrieved — all of them, not only the cited
 * ones, because a source that was retrieved and then unused is part of what the
 * run did. Evidence depth stays a first-class column: a claim grounded in a full
 * text and one grounded in an abstract are not equally well supported.
 */

import { useState } from 'react'

import { research } from '../api/endpoints.js'
import RunPicker from '../components/RunPicker.jsx'
import { Badge, Card, PageHeader } from '../components/ui/primitives.jsx'
import { AsyncBoundary, EmptyState } from '../components/ui/states.jsx'
import { useApi } from '../hooks/useApi.js'

const DEPTH_TONES = { full_text: 'positive', abstract: 'warning', snippet: 'neutral', metadata: 'neutral' }

function hostOf(url) {
  try {
    return new URL(url).hostname
  } catch {
    return url
  }
}

export default function SourcesPage() {
  const [runId, setRunId] = useState(null)
  const sources = useApi(({ signal }) => research.sources(runId, { signal }), {
    enabled: Boolean(runId),
    deps: [runId],
  })

  return (
    <>
      <PageHeader
        title="Sources"
        description="Every web page and paper retrieved, and how deeply each one was read."
      />

      <Card title="Pick a run">
        <RunPicker value={runId} onChange={setRunId} />
      </Card>

      {runId && (
        <Card title="Retrieved sources" description="Read live from the run's stored source links.">
          <AsyncBoundary
            loading={sources.loading}
            error={sources.error}
            onRetry={sources.reload}
            loadingMessage="Loading sources…"
            isEmpty={sources.data?.length === 0}
            empty={
              <EmptyState
                title="No sources"
                message="This run retrieved nothing — a model_only run performs no retrieval by design."
              />
            }
          >
            <table className="table">
              <thead>
                <tr>
                  <th scope="col">Label</th>
                  <th scope="col">Title</th>
                  <th scope="col">Type</th>
                  <th scope="col">Venue / domain</th>
                  <th scope="col">Year</th>
                  <th scope="col">Retriever</th>
                  <th scope="col">Evidence depth</th>
                </tr>
              </thead>
              <tbody>
                {(sources.data ?? []).map((source) => (
                  <tr key={source.source_id}>
                    <td>{source.label ? <code>[{source.label}]</code> : '—'}</td>
                    <td>
                      {source.url ? (
                        <a className="link" href={source.url} target="_blank" rel="noreferrer">
                          {source.title ?? source.url}
                        </a>
                      ) : (
                        (source.title ?? '(untitled)')
                      )}
                    </td>
                    <td>
                      <code>{source.source_type}</code>
                    </td>
                    <td>{source.venue ?? (source.url ? hostOf(source.url) : '—')}</td>
                    <td>{source.year ?? '—'}</td>
                    <td>{source.retriever ?? '—'}</td>
                    <td>
                      <Badge tone={DEPTH_TONES[source.evidence_depth] ?? 'neutral'}>
                        {source.evidence_depth}
                      </Badge>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            <p className="note">
              A row without a label was retrieved but not selected as evidence; the report never
              cites it. Labels like <code>[S1]</code> are what the report&apos;s citations resolve
              to.
            </p>
          </AsyncBoundary>
        </Card>
      )}

      <Card title="Why evidence depth is a column">
        <p className="note">
          Many papers yield only an abstract. A claim grounded in a full text and a claim grounded in
          an abstract are not equally well supported, so each source records how deeply it was
          actually read — otherwise the groundedness metrics would overstate the result.
        </p>
      </Card>
    </>
  )
}
