/**
 * Evaluation.
 *
 * The benchmark comparison across the three configurations -- the result the whole
 * project builds toward. The metric definitions are listed because they must be
 * agreed *before* any measurement, but every value is absent.
 */

import { Card, PageHeader, PlannedContents } from '../components/ui/primitives.jsx'
import { EmptyState, NotImplemented } from '../components/ui/states.jsx'
import { RESEARCH_MODES, STAGES } from '../lib/constants.js'

const METRICS = [
  { name: 'Claim support rate', intent: 'share of extracted claims verified as supported' },
  { name: 'Hallucination rate', intent: 'share unsupported or contradicted' },
  { name: 'Citation precision', intent: 'of the citations present, how many are valid' },
  { name: 'Citation recall', intent: 'of the claims needing a citation, how many have a valid one' },
  { name: 'Fabrication rate', intent: 'share of citations pointing at sources that do not exist' },
  { name: 'Coverage', intent: 'share of planned sub-questions actually addressed' },
  { name: 'Source diversity', intent: 'distinct domains and venues per run' },
  { name: 'Evidence depth', intent: 'full-text grounding versus abstract-only' },
  { name: 'Cost', intent: 'tokens and USD per run, from the LLM call log' },
]

export default function EvaluationPage() {
  return (
    <>
      <PageHeader
        title="Evaluation"
        description="Quality metrics, and the benchmark comparing the three configurations."
      />

      <Card
        title="Benchmark comparison"
        description="The same topic set run through each configuration, with everything else held constant."
      >
        <table className="table">
          <thead>
            <tr>
              <th scope="col">Metric</th>
              {RESEARCH_MODES.map((mode) => (
                <th key={mode.value} scope="col">
                  {mode.label}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {METRICS.slice(0, 4).map((metric) => (
              <tr key={metric.name}>
                <th scope="row">{metric.name}</th>
                {RESEARCH_MODES.map((mode) => (
                  <td key={mode.value} className="table__pending">
                    not measured
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
        <p className="note">
          Every cell reads <em>not measured</em>. No benchmark has been run, and a placeholder number
          here would be a fabricated result.
        </p>
      </Card>

      <Card
        title="Metric definitions"
        description="Written down before anything is measured, so the numbers cannot be reverse-engineered to look good."
      >
        <ul className="metric-list">
          {METRICS.map((metric) => (
            <li key={metric.name} className="metric">
              <span className="metric__name">{metric.name}</span>
              <span className="metric__intent">{metric.intent}</span>
              <span className="metric__value">not measured</span>
            </li>
          ))}
        </ul>
      </Card>

      <Card title="Benchmark runs">
        <EmptyState
          title="No benchmark runs"
          message="A benchmark run executes the same topics through all three configurations. Neither the pipelines nor the metrics exist yet."
        />
      </Card>

      <Card title="The harness">
        <NotImplemented feature="Metrics and benchmarking" stage={STAGES.EVALUATION}>
          <PlannedContents
            items={[
              { label: 'Metrics module', detail: 'the definitions above, computed per run' },
              {
                label: 'Benchmark harness',
                detail: 'holds the topic set and prompts constant, varies only the mode',
              },
              { label: 'Topic set', detail: 'committed under data/benchmarks so runs are repeatable' },
              { label: 'Comparison tables', detail: 'regenerated from scratch, never hand-edited' },
              { label: 'Charts', detail: 'per-metric comparison across the three configurations' },
            ]}
          />
        </NotImplemented>
      </Card>
    </>
  )
}
