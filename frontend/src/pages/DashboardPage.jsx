/**
 * Dashboard.
 *
 * The one page backed by real data: it reads `GET /api/v1/system/capabilities`
 * and reports exactly what the backend says. Nothing here is invented, which is
 * also what makes it the page that genuinely exercises the loading, error and
 * empty states.
 */

import { CheckCircle2, CircleDashed, ExternalLink } from 'lucide-react'
import { Link } from 'react-router-dom'

import { AsyncBoundary, EmptyState, NotImplemented } from '../components/ui/states.jsx'
import { Badge, Card, PageHeader, PlannedContents, StatTile } from '../components/ui/primitives.jsx'
import { useCapabilities } from '../hooks/useBackendStatus.js'
import { CURRENT_STAGE, RESEARCH_MODES, STAGES } from '../lib/constants.js'

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

export default function DashboardPage() {
  const { data, error, loading, reload } = useCapabilities()

  const backendModes = data?.modes ?? []
  // The mode list is duplicated in lib/constants.js so routing and labels work
  // before any request resolves. Comparing the two here means a drift between
  // frontend and backend shows up as a visible warning instead of silently
  // mislabelling a benchmark configuration.
  const localModes = RESEARCH_MODES.map((m) => m.value)
  const modesAgree =
    backendModes.length === localModes.length && backendModes.every((m) => localModes.includes(m))

  return (
    <>
      <PageHeader
        title="Dashboard"
        description="What is built, what is configured, and what is still to come."
      />

      <div className="grid grid--stats">
        <StatTile
          label="Build stage"
          value={`${CURRENT_STAGE.number} / ${CURRENT_STAGE.total}`}
          hint={CURRENT_STAGE.name}
        />
        <StatTile label="Research runs" value="0" hint="no runs have been created" />
        <StatTile
          label="Research modes"
          value={backendModes.length || '—'}
          hint="declared by the backend"
        />
        <StatTile label="Verified claims" value="0" hint="evidence layer not built" />
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
                  Still to set in <code>.env</code>: {data.missing_credentials.join(', ')}. Not
                  needed until the retrieval stage.
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

      <Card
        title="Research configurations"
        description="The three settings the benchmark will compare."
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

      <Card title="Recent research runs">
        <EmptyState
          title="No research runs"
          message="Nothing has been run because the research API does not exist yet. Creating and storing runs arrives in stage 4."
          action={
            <Link className="btn btn--ghost" to="/research/new">
              See the research form
            </Link>
          }
        />
      </Card>

      <Card title="Implemented capabilities">
        {/* Deliberately not inside an AsyncBoundary: this notice is static, and
            rendering the same failed request as a second error block would be
            noise rather than information. Only the line that reads backend data
            is conditional. */}
        <NotImplemented feature="Research orchestration" stage={STAGES.MODEL_ONLY}>
          {data && (
            <p className="state__message">
              The backend reports <code>implemented: {JSON.stringify(data.implemented ?? [])}</code>
              . That list fills in as each stage lands.
            </p>
          )}
          <PlannedContents
            items={[
              { label: 'Planning', detail: 'topic to sub-questions to search queries' },
              { label: 'Retrieval', detail: 'web, academic papers, RAG over full text' },
              { label: 'Synthesis', detail: 'report with inline citation markers' },
              { label: 'Evidence', detail: 'claim extraction, verification, citation validation' },
              { label: 'Evaluation', detail: 'metrics and the three-way benchmark' },
            ]}
          />
        </NotImplemented>
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
