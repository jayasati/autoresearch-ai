/**
 * Evidence Audit.
 *
 * The project's core contribution: every claim a run's report made, the verdict
 * it received, each citation's validation outcome, and the conflicts found
 * between sources. Everything is a stored row read back verbatim — a run whose
 * claims were never verified shows exactly that, not a blank.
 */

import { useState } from 'react'

import { research } from '../api/endpoints.js'
import RunPicker from '../components/RunPicker.jsx'
import { Badge, Card, PageHeader } from '../components/ui/primitives.jsx'
import { AsyncBoundary, EmptyState } from '../components/ui/states.jsx'
import { useApi } from '../hooks/useApi.js'
import { CITATION_STATUSES, VERIFICATION_VERDICTS } from '../lib/constants.js'

const VERDICT_TONES = Object.fromEntries(VERIFICATION_VERDICTS.map((v) => [v.value, v.tone]))
const VERDICT_LABELS = Object.fromEntries(VERIFICATION_VERDICTS.map((v) => [v.value, v.label]))
const CITATION_TONES = Object.fromEntries(CITATION_STATUSES.map((s) => [s.value, s.tone]))

function ClaimRow({ claim }) {
  return (
    <li className="vocab__item" style={{ alignItems: 'flex-start' }}>
      <Badge tone={claim.verdict ? (VERDICT_TONES[claim.verdict] ?? 'neutral') : 'neutral'}>
        {claim.verdict ? (VERDICT_LABELS[claim.verdict] ?? claim.verdict) : 'not verified'}
      </Badge>
      <div>
        <p style={{ margin: 0 }}>
          {claim.text}
          {claim.downgraded && (
            <>
              {' '}
              <Badge tone="warning">downgraded</Badge>
            </>
          )}
        </p>
        <p className="field__help" style={{ marginTop: 4 }}>
          {claim.claim_type} · importance {claim.importance}
          {claim.section ? ` · section “${claim.section}”` : ''}
          {claim.confidence != null ? ` · confidence ${claim.confidence}` : ''}
          {Object.entries(claim.citations).map(([marker, status]) => (
            <span key={marker}>
              {' '}
              <Badge tone={CITATION_TONES[status] ?? 'neutral'}>
                [{marker}] {status}
              </Badge>
            </span>
          ))}
        </p>
        {claim.rationale && <p className="field__help">{claim.rationale}</p>}
      </div>
    </li>
  )
}

export default function EvidenceAuditPage() {
  const [runId, setRunId] = useState(null)
  const evidence = useApi(({ signal }) => research.evidence(runId, { signal }), {
    enabled: Boolean(runId),
    deps: [runId],
  })

  const noReport = evidence.error?.status === 404
  const audit = evidence.data

  return (
    <>
      <PageHeader
        title="Evidence Audit"
        description="Claim-level verification, citation validation, and conflicts between sources."
      />

      <Card title="Pick a run">
        <RunPicker value={runId} onChange={setRunId} />
      </Card>

      {runId &&
        (noReport ? (
          <Card title="Claims">
            <EmptyState
              title="Nothing to audit"
              message="This run produced no report, so no claims were ever extracted from it."
            />
          </Card>
        ) : (
          <AsyncBoundary
            loading={evidence.loading}
            error={evidence.error}
            onRetry={evidence.reload}
            loadingMessage="Loading the audit…"
          >
            {audit && (
              <>
                <Card
                  title={`Claims (${audit.claims.length})`}
                  description={`“${audit.topic}” — every claim stored for this report, verbatim.`}
                  action={
                    <Badge tone={audit.finalized ? 'positive' : 'neutral'}>
                      {audit.finalized ? 'finalized' : 'not finalized'}
                    </Badge>
                  }
                >
                  {audit.claims.length === 0 ? (
                    <EmptyState
                      title="No claims extracted"
                      message="The report exists but claim extraction was never run for it. POST /api/v1/research/{id}/claims extracts them."
                    />
                  ) : (
                    <ul className="vocab">
                      {audit.claims.map((claim) => (
                        <ClaimRow key={claim.claim_id} claim={claim} />
                      ))}
                    </ul>
                  )}
                </Card>

                <Card
                  title={`Conflicts (${audit.conflicts.length})`}
                  description="Disagreements the verifier found between passages, with their stored descriptions."
                >
                  {audit.conflicts.length === 0 ? (
                    <p className="state__message">No conflicts were stored for this run.</p>
                  ) : (
                    <ul className="planned__list">
                      {audit.conflicts.map((conflict) => (
                        <li key={conflict.conflict_id}>
                          <Badge tone="warning">{conflict.conflict_type}</Badge>{' '}
                          {conflict.description}
                        </li>
                      ))}
                    </ul>
                  )}
                </Card>

                <Card
                  title={`Labelled sources (${audit.sources.length})`}
                  description="What the report's citation markers resolve to."
                >
                  {audit.sources.length === 0 ? (
                    <p className="state__message">No labelled sources — nothing was cited.</p>
                  ) : (
                    <ul className="planned__list">
                      {audit.sources.map((source) => (
                        <li key={source.label}>
                          <code>[{source.label}]</code>{' '}
                          {source.url ? (
                            <a className="link" href={source.url} target="_blank" rel="noreferrer">
                              {source.title ?? source.url}
                            </a>
                          ) : (
                            (source.title ?? '(untitled)')
                          )}
                          {source.venue ? ` — ${source.venue}` : ''}
                          {source.year ? `, ${source.year}` : ''}
                        </li>
                      ))}
                    </ul>
                  )}
                </Card>
              </>
            )}
          </AsyncBoundary>
        ))}

      <Card
        title="Verification vocabulary"
        description="The verdicts a claim can receive. These mirror the backend enums exactly."
      >
        <ul className="vocab">
          {VERIFICATION_VERDICTS.map((verdict) => (
            <li key={verdict.value} className="vocab__item">
              <Badge tone={verdict.tone}>{verdict.label}</Badge>
              <code>{verdict.value}</code>
            </li>
          ))}
        </ul>
      </Card>

      <Card
        title="Citation outcomes"
        description="Four distinct failure modes, counted separately."
      >
        <ul className="vocab">
          {CITATION_STATUSES.map((status) => (
            <li key={status.value} className="vocab__item">
              <Badge tone={status.tone}>{status.label}</Badge>
              <code>{status.value}</code>
            </li>
          ))}
        </ul>
        <p className="note">
          A <strong>fabricated</strong> citation (no such paper exists) and a{' '}
          <strong>misattributed</strong> one (a real paper cited for a claim it does not make) are
          different defects with different causes. Collapsing them into a single “bad citation” flag
          would hide the most interesting result this project can produce.
        </p>
      </Card>
    </>
  )
}
