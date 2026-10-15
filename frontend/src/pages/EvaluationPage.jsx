/**
 * Evaluation.
 *
 * The benchmark comparison the project builds toward, served verbatim from the
 * committed artifacts under data/benchmarks/results — the backend reads the
 * experiment runner's own files and never recomputes a number at request time.
 * When no artifact exists the page says so; it never renders a zero it did not
 * read.
 */

import { evaluation } from '../api/endpoints.js'
import { Badge, Card, PageHeader, StatTile } from '../components/ui/primitives.jsx'
import { AsyncBoundary, EmptyState } from '../components/ui/states.jsx'
import { useApi } from '../hooks/useApi.js'

const METRIC_LABELS = {
  claim_support_rate: 'Claim support rate',
  unsupported_claim_rate: 'Unsupported claim rate',
  citation_precision: 'Citation precision',
  citation_coverage: 'Citation coverage',
  retrieval_precision_at_k: 'Retrieval precision@k',
  conflict_detection_rate: 'Conflict detection rate',
  external_support_rate: 'External support rate',
  product_latency_seconds: 'Latency (s)',
  product_total_tokens: 'Tokens per run',
  product_cost_usd: 'Cost (USD)',
}

const DIRECTION_TONES = { improved: 'positive', worse: 'negative', unchanged: 'neutral' }

function value(x) {
  if (x === null || x === undefined) return '—'
  return typeof x === 'number' ? x.toFixed(4).replace(/\.?0+$/, '') : String(x)
}

export default function EvaluationPage() {
  const benchmark = useApi(({ signal }) => evaluation.benchmark({ signal }))
  const data = benchmark.data
  const noArtifacts = benchmark.error?.status === 404
  const primary = data?.external?.primary_matched
  const aggregatesV2 = data?.external?.aggregates_v2

  return (
    <>
      <PageHeader
        title="Evaluation"
        description="The frozen-test benchmark, read verbatim from the committed artifacts."
      />

      {noArtifacts ? (
        <Card title="Benchmark comparison">
          <EmptyState
            title="No benchmark artifacts"
            message="No experiment results exist under data/benchmarks/results for this backend. A placeholder number here would be a fabricated result, so nothing is shown."
          />
        </Card>
      ) : (
        <AsyncBoundary
          loading={benchmark.loading}
          error={benchmark.error}
          onRetry={benchmark.reload}
          loadingMessage="Reading benchmark artifacts…"
        >
          {data && (
            <>
              {primary && (
                <Card
                  title="Primary result — external support, matched questions"
                  description="Metrics v2: claims judged arm-blind against evaluator-owned evidence pools, one shared denominator for both arms. Supportability against retrieved pools, not ground-truth accuracy."
                >
                  <div className="grid grid--stats">
                    <StatTile
                      label={`Baseline (${primary.baseline_arm})`}
                      value={value(primary.baseline?.macro)}
                      hint="macro external support"
                    />
                    <StatTile
                      label={`AutoResearch (${primary.system_arm})`}
                      value={value(primary.system?.macro)}
                      hint="macro external support"
                    />
                    <StatTile
                      label="Difference"
                      value={value(primary.macro_difference_system_minus_baseline)}
                      hint={`macro, n = ${primary.n} matched questions`}
                    />
                  </div>
                  {primary.baseline?.per_question && primary.system?.per_question && (
                    <table className="table">
                      <thead>
                        <tr>
                          <th scope="col">Question</th>
                          <th scope="col">Baseline</th>
                          <th scope="col">AutoResearch</th>
                        </tr>
                      </thead>
                      <tbody>
                        {Object.keys(primary.baseline.per_question).map((question) => (
                          <tr key={question}>
                            <th scope="row">
                              <code>{question}</code>
                            </th>
                            <td>{value(primary.baseline.per_question[question])}</td>
                            <td>{value(primary.system.per_question[question])}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  )}
                  {primary.exclusions?.length > 0 && (
                    <p className="note">
                      Excluded from matching:{' '}
                      {primary.exclusions
                        .map((e) => `${e.question_id} × ${e.arm} (${e.reason})`)
                        .join('; ')}
                      . Missing cells are named, never imputed.
                    </p>
                  )}
                </Card>
              )}

              <Card
                title="Benchmark comparison (metrics v1)"
                description={`Baseline vs the full verified system on the ${data.set_name} set — artifact ${data.source_dir}, served verbatim.`}
              >
                <table className="table">
                  <thead>
                    <tr>
                      <th scope="col">Metric</th>
                      <th scope="col">Baseline</th>
                      <th scope="col">AutoResearch</th>
                      <th scope="col">Δ</th>
                      <th scope="col">Direction</th>
                    </tr>
                  </thead>
                  <tbody>
                    {data.comparison.map((row) => (
                      <tr key={row.metric}>
                        <th scope="row">{METRIC_LABELS[row.metric] ?? row.metric}</th>
                        <td>{value(row.baseline)}</td>
                        <td>{value(row.autoresearch)}</td>
                        <td>{value(row.absolute_difference)}</td>
                        <td>
                          {row.direction ? (
                            <Badge tone={DIRECTION_TONES[row.direction] ?? 'neutral'}>
                              {row.direction}
                            </Badge>
                          ) : (
                            '—'
                          )}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
                <p className="note">
                  Metrics v1 are self-grounding (verification against the run&apos;s own evidence), which
                  structurally zeroes the no-retrieval baseline on citation metrics — the reason the
                  arm-blind external evaluation above exists. Footnoted values and caveats live in
                  the committed evaluation report alongside the artifact.
                </p>
              </Card>

              {aggregatesV2 && (
                <Card
                  title="External support by arm (metrics v2, all available questions)"
                  description="Judged against identical evaluator-owned pools per question; absence from a pool is not falsity."
                >
                  <table className="table">
                    <thead>
                      <tr>
                        <th scope="col">Arm</th>
                        <th scope="col">Questions</th>
                        <th scope="col">Support (macro)</th>
                        <th scope="col">Contradiction (macro)</th>
                      </tr>
                    </thead>
                    <tbody>
                      {Object.entries(aggregatesV2).map(([arm, stats]) => (
                        <tr key={arm}>
                          <th scope="row">
                            <code>{arm}</code>
                          </th>
                          <td>{stats.eligible_questions ?? '—'}</td>
                          <td>{value(stats.external_support_rate_macro)}</td>
                          <td>{value(stats.external_contradiction_rate_macro)}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </Card>
              )}
            </>
          )}
        </AsyncBoundary>
      )}

      <Card
        title="How to read these numbers"
        description="The caveats are part of the result."
      >
        <ul className="planned__list">
          <li>
            <strong>Supportability, not accuracy</strong> — external support measures whether an
            independent evidence pool backs a claim, bounded by what the evaluator could retrieve.
          </li>
          <li>
            <strong>No significance claim</strong> — the frozen test set is small (one question per
            category, single pass); directions replicate across development and validation sets.
          </li>
          <li>
            <strong>Missing cells stay visible</strong> — infrastructure failures are excluded by
            name from matched comparisons, never imputed.
          </li>
          <li>
            <strong>Nothing is recomputed here</strong> — every number is read from the committed
            artifact files; re-running the benchmark regenerates them, hand-editing never does.
          </li>
        </ul>
      </Card>
    </>
  )
}
