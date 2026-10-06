/**
 * Evidence Audit.
 *
 * The project's core contribution, and the page that will carry the most weight.
 * The verdict and citation-status vocabularies shown here are real -- they come
 * from the backend enums -- but no claim has ever been verified, so there is
 * nothing to audit.
 */

import { Card, PageHeader, PlannedContents, Badge } from '../components/ui/primitives.jsx'
import { EmptyState, NotImplemented } from '../components/ui/states.jsx'
import { CITATION_STATUSES, STAGES, VERIFICATION_VERDICTS } from '../lib/constants.js'

export default function EvidenceAuditPage() {
  return (
    <>
      <PageHeader
        title="Evidence Audit"
        description="Claim-level verification, citation validation, and conflicts between sources."
      />

      <Card title="Claims">
        <EmptyState
          title="No claims"
          message="Claims are extracted from a generated report. No report exists, so there is nothing to extract or verify."
        />
      </Card>

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

      <Card title="The audit itself">
        <NotImplemented feature="Claim extraction and verification" stage={STAGES.EVIDENCE}>
          <PlannedContents
            items={[
              { label: 'Claim extractor', detail: 'report prose into atomic, checkable claims' },
              { label: 'Evidence linker', detail: 'each claim to candidate supporting passages' },
              {
                label: 'Verifier',
                detail: 'a verdict plus a quoted span; no quote means not_enough_evidence',
              },
              {
                label: 'Citation validator',
                detail: 'does the cited source exist, resolve, and support this claim',
              },
              {
                label: 'Conflict detector',
                detail: 'numeric, directional, temporal and scope disagreements between sources',
              },
              {
                label: 'Traceability',
                detail: 'the claim to passage to source to URL chain, drillable in the UI',
              },
            ]}
          />
        </NotImplemented>
      </Card>
    </>
  )
}
