/**
 * Dashboard.
 *
 * Everything on this page is read live from the backend: integration status from
 * `/api/v1/system/capabilities`, the run list from `/api/v1/research`, and the
 * benchmark headline from `/api/v1/evaluation/benchmark`. Nothing is invented; a
 * number that cannot be read shows as absent, never as zero.
 */

import { CheckCircle2, CircleDashed, ExternalLink } from 'lucide-react'
import { Link } from 'react-router-dom'

import { evaluation, research } from '../api/endpoints.js'
import { Badge, Card, PageHeader, StatTile } from '../components/ui/primitives.jsx'
import { AsyncBoundary, EmptyState } from '../components/ui/states.jsx'
import { useApi } from '../hooks/useApi.js'
import { useCapabilities } from '../hooks/useBackendStatus.js'
import { RESEARCH_MODES } from '../lib/constants.js'

/** One integration and whether the backend has usable credentials for it. */
function IntegrationRow({ name, configured, note }) {
  return (
    <li className="integration">
      {configured ? (
        <CheckCircle2 size={15} className="integration__icon integration__icon--ok" aria-hidden="true" />
      ) : (
        <CircleDashed size={15} className="integration__icon" aria-hidden="true" />
      )}
      <span className="integration__name">{name}</span>
      <Badge tone={configured ? 'positive' : 'neutral'}>
        {configured ? 'configured' : 'not configured'}
      </Badge>
      {note && <span className="integration__note">{note}</span>}
    </li>
  )
}

const INTEGRATION_LABELS = {
  openai: { name: 'OpenAI', note: 'foundation model' },
  tavily: { name: 'Tavily', note: 'web search' },
  semantic_scholar: { name: 'Semantic Scholar', note: 'academic search · public API' },
  postgres: { name: 'PostgreSQL', note: 'structured data' },
  chromadb: { name: 'ChromaDB', note: 'vector store · local' },
}

const STATUS_TONES = { completed: 'positive', failed: 'negative', running: 'warning' }

export default function DashboardPage() {
  const { data, error, loading, reload } = useCapabilities()
  const runs = useApi(({ signal }) => research.list({ signal }))
  const benchmark = useApi(({ signal }) => evaluation.benchmark({ signal }))

  const backendModes = data?.modes ?? []
  // The mode list is duplicated in lib/constants.js so routing and labels work
  // before any request resolves. Comparing the two here means a drift between
  // frontend and backend shows up as a visible warning instead of silently
  // mislabelling a benchmark configuration.
  const localModes = RESEARCH_MODES.map((m) => m.value)
  const modesAgree =
    backendModes.length === localModes.length && backendModes.every((m) => localModes.includes(m))

  const primary = benchmark.data?.external?.primary_matched
  const supportGain =
    primary && typeof primary.macro_difference_system_minus_baseline === 'number'
      ? `+${primary.macro_difference_system_minus_baseline.toFixed(3)}`
      : null

  return (
    <>
      <PageHeader
        title="Dashboard"
        description="What is configured, what has run, and what the benchmark measured."
      />

      <div className="grid grid--stats">
        <StatTile
          label="Research runs"
          value={runs.data ? String(runs.data.length) : '—'}
          hint={runs.data ? 'stored in the database (most recent 20)' : 'reading…'}
        />
        <StatTile
          label="Research modes"
          value={backendModes.length || '—'}
          hint="declared by the backend"
        />
        <StatTile
          label="External support gain"
          value={supportGain ?? '—'}
          hint={
            supportGain
              ? `frozen test set, macro over ${primary.n} matched questions`
              : 'no benchmark artifact readable'
          }
        />
        <StatTile
          label="Pipeline"
          value={data?.implemented?.length ? 'live' : '—'}
          hint={
            data?.implemented?.length
              ? `modes implemented: ${data.implemented.join(', ')}`
              : 'reading…'
          }
        />
      </div>

      <Card
        title="Backend integrations"
        description="Read live from /api/v1/system/capabilities. A placeholder key counts as not configured."
      >
        <AsyncBoundary
          loading={loading}
          error={error}
          onRetry={reload}
          loadingMessage="Reading backend capabilities…"
        >
          {data && (
            <>
              <ul className="integration-list">
                {Object.entries(data.integrations ?? {}).map(([key, configured]) => (
                  <IntegrationRow
                    key={key}
                    name={INTEGRATION_LABELS[key]?.name ?? key}
                    note={INTEGRATION_LABELS[key]?.note}
                    configured={configured}
                  />
                ))}
              </ul>

              {data.missing_credentials?.length > 0 && (
                <p className="note">
                  Still to set in <code>.env</code>: {data.missing_credentials.join(', ')}. Needed
                  for live research runs.
                </p>
              )}

              {!modesAgree && backendModes.length > 0 && (
                <p className="note note--warn">
                  The backend declares modes [{backendModes.join(', ')}] but this build knows [
                  {localModes.join(', ')}]. One of them is out of date.
                </p>
              )}
            </>
          )}
        </AsyncBoundary>
      </Card>

      <Card title="Recent research runs" description="Read live from /api/v1/research.">
        <AsyncBoundary
          loading={runs.loading}
          error={runs.error}
          onRetry={runs.reload}
          loadingMessage="Loading runs…"
          isEmpty={runs.data?.length === 0}
          empty={
            <EmptyState
              title="No research runs yet"
              message="Start one from the New Research page; it will appear here and under Research Results."
              action={
                <Link className="btn btn--ghost" to="/research/new">
                  Start a research run
                </Link>
              }
            />
          }
        >
          <table className="table">
            <thead>
              <tr>
                <th scope="col">Topic</th>
                <th scope="col">Mode</th>
                <th scope="col">Status</th>
                <th scope="col">Created</th>
              </tr>
            </thead>
            <tbody>
              {(runs.data ?? []).slice(0, 8).map((run) => (
                <tr key={run.id}>
                  <td>
                    <Link className="link" to="/research/results">
                      {run.topic.length > 60 ? `${run.topic.slice(0, 60)}…` : run.topic}
                    </Link>
                  </td>
                  <td>
                    <code>{run.mode}</code>
                  </td>
                  <td>
                    <Badge tone={STATUS_TONES[run.status] ?? 'neutral'}>{run.status}</Badge>
                  </td>
                  <td>{run.created_at ? new Date(run.created_at).toLocaleString() : '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </AsyncBoundary>
      </Card>

      <Card
        title="Research configurations"
        description="The configurations the benchmark compares."
      >
        <ul className="mode-list">
          {RESEARCH_MODES.map((mode) => (
            <li key={mode.value} className="mode">
              <div className="mode__head">
                <span className="mode__label">{mode.label}</span>
                <code className="mode__value">{mode.value}</code>
              </div>
              <p className="mode__summary">{mode.summary}</p>
              <p className="mode__sources">Sources: {mode.sources}</p>
            </li>
          ))}
        </ul>
      </Card>

      <Card title="API reference">
        <p className="note">
          The running backend serves its own interactive spec.{' '}
          <a className="link" href="http://localhost:8000/docs" target="_blank" rel="noreferrer">
            Open /docs <ExternalLink size={12} aria-hidden="true" />
          </a>
        </p>
      </Card>
    </>
  )
}
