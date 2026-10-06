/**
 * Research Results.
 *
 * Shows no report, because no report can be produced. The page describes the
 * structure a report will have, which is useful to fix now, and shows nothing
 * that could be mistaken for generated output.
 */

import { Card, PageHeader, PlannedContents } from '../components/ui/primitives.jsx'
import { EmptyState, NotImplemented } from '../components/ui/states.jsx'
import { STAGES } from '../lib/constants.js'

export default function ResearchResultsPage() {
  return (
    <>
      <PageHeader
        title="Research Results"
        description="Generated reports, with every claim traceable to a source."
      />

      <Card title="Reports">
        <EmptyState
          title="No reports"
          message="Nothing has been generated. No research run has ever executed, because the orchestration pipeline does not exist yet."
        />
      </Card>

      <Card title="Report view">
        <NotImplemented feature="Report generation and display" stage={STAGES.MODEL_ONLY}>
          <PlannedContents
            items={[
              { label: 'Run header', detail: 'topic, configuration, status, duration, token cost' },
              { label: 'Live timeline', detail: 'planning, retrieving, drafting, verifying' },
              { label: 'Report body', detail: 'sections with inline citation markers such as [S3]' },
              {
                label: 'Claim highlighting',
                detail: 'each sentence shaded by its verification verdict',
              },
              { label: 'Citation hover', detail: 'the quoted supporting passage, not just a link' },
              { label: 'Export', detail: 'Markdown and JSON, with the evidence chain included' },
            ]}
          />
        </NotImplemented>
      </Card>

      <Card title="Why nothing is shown instead of a sample">
        <p className="note">
          A mock report here would be indistinguishable from a real one in a screenshot, and this
          project exists to measure how often generated text is unsupported. Showing invented output
          — even clearly labelled — would undermine the thing being measured.
        </p>
      </Card>
    </>
  )
}
