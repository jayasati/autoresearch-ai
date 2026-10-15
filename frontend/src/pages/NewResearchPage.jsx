/**
 * New Research.
 *
 * The form posts to `POST /api/v1/research/start`, which runs the whole pipeline
 * synchronously: the response arrives only when the run has finished (roughly
 * 15-90+ seconds depending on depth), so the page keeps an explicit running state
 * instead of pretending the wait is short. A run makes paid OpenAI and Tavily
 * calls; the depth options say so rather than hiding it.
 */

import { Info, Play } from 'lucide-react'
import { useState } from 'react'

import { research } from '../api/endpoints.js'
import { Badge, Card, PageHeader } from '../components/ui/primitives.jsx'
import { ErrorState, Spinner } from '../components/ui/states.jsx'
import { RESEARCH_DEPTHS, RESEARCH_MODES } from '../lib/constants.js'

const MIN_TOPIC_LENGTH = 12

export default function NewResearchPage() {
  const [topic, setTopic] = useState('')
  const [mode, setMode] = useState('search_grounded')
  const [depth, setDepth] = useState('quick')
  const [touched, setTouched] = useState(false)
  const [running, setRunning] = useState(false)
  const [result, setResult] = useState(null)
  const [error, setError] = useState(null)

  const trimmed = topic.trim()
  const tooShort = trimmed.length > 0 && trimmed.length < MIN_TOPIC_LENGTH
  const isValid = trimmed.length >= MIN_TOPIC_LENGTH
  const selected = RESEARCH_MODES.find((m) => m.value === mode)

  async function submit(event) {
    event.preventDefault()
    if (!isValid || running) return
    setRunning(true)
    setResult(null)
    setError(null)
    try {
      setResult(await research.start({ topic: trimmed, mode, depth }))
    } catch (cause) {
      setError(cause)
    } finally {
      setRunning(false)
    }
  }

  return (
    <>
      <PageHeader
        title="New Research"
        description="Choose a topic and a research configuration."
      />

      <Card
        title="Research request"
        description="Runs the full pipeline and waits for the finished report."
      >
        <form className="form" onSubmit={submit}>
          <div className="field">
            <label className="field__label" htmlFor="topic">
              Research topic
            </label>
            <textarea
              id="topic"
              className="field__input field__input--area"
              rows={3}
              value={topic}
              placeholder="e.g. How effective is retrieval-augmented generation at reducing factual errors?"
              onChange={(event) => setTopic(event.target.value)}
              onBlur={() => setTouched(true)}
              disabled={running}
              aria-describedby="topic-help"
              aria-invalid={touched && tooShort ? 'true' : undefined}
            />
            <p className="field__help" id="topic-help">
              {touched && tooShort
                ? `Too short — give at least ${MIN_TOPIC_LENGTH} characters so the planner has something to decompose.`
                : 'A question works better than a keyword: the planner decomposes it into sub-questions.'}
            </p>
          </div>

          <fieldset className="field" disabled={running}>
            <legend className="field__label">Research configuration</legend>
            <div className="radio-group">
              {RESEARCH_MODES.map((option) => (
                <label
                  key={option.value}
                  className={`radio${mode === option.value ? ' radio--selected' : ''}`}
                >
                  <input
                    type="radio"
                    name="mode"
                    value={option.value}
                    checked={mode === option.value}
                    onChange={(event) => setMode(event.target.value)}
                  />
                  <span className="radio__body">
                    <span className="radio__label">{option.label}</span>
                    <span className="radio__summary">{option.summary}</span>
                    <span className="radio__sources">Sources: {option.sources}</span>
                  </span>
                </label>
              ))}
            </div>
          </fieldset>

          <fieldset className="field" disabled={running}>
            <legend className="field__label">Depth</legend>
            <div className="radio-group">
              {RESEARCH_DEPTHS.map((option) => (
                <label
                  key={option.value}
                  className={`radio${depth === option.value ? ' radio--selected' : ''}`}
                >
                  <input
                    type="radio"
                    name="depth"
                    value={option.value}
                    checked={depth === option.value}
                    onChange={(event) => setDepth(event.target.value)}
                  />
                  <span className="radio__body">
                    <span className="radio__label">{option.label}</span>
                    <span className="radio__summary">{option.summary}</span>
                  </span>
                </label>
              ))}
            </div>
          </fieldset>

          {selected && (
            <p className="note">
              <Info size={13} aria-hidden="true" /> {selected.detail}
            </p>
          )}

          <p className="note note--warn">
            <Info size={13} aria-hidden="true" /> A run makes real OpenAI and Tavily calls and the
            request waits for the whole pipeline — expect 15&ndash;90+ seconds depending on depth.
          </p>

          <div className="form__actions">
            <button
              type="submit"
              className="btn btn--primary"
              disabled={!isValid || running}
              aria-disabled={!isValid || running ? 'true' : undefined}
            >
              {running ? <Spinner label="Research running" /> : <Play size={14} aria-hidden="true" />}
              {running ? 'Running — this takes a while…' : 'Start research'}
            </button>
            <Badge tone={isValid ? 'positive' : 'neutral'}>
              {isValid ? 'request is valid' : 'incomplete'}
            </Badge>
          </div>
        </form>
      </Card>

      {error && (
        <Card title="Run failed">
          <ErrorState error={error} title="The research run failed" />
        </Card>
      )}

      {result && (
        <>
          <Card
            title="Report"
            description={`Run ${result.research_id} — exactly what the pipeline produced, unedited.`}
            action={<Badge tone={result.status === 'completed' ? 'positive' : 'warning'}>{result.status}</Badge>}
          >
            <div className="report-body">{result.report}</div>
          </Card>

          <Card
            title={`Sources (${result.sources.length})`}
            description="What the report's [S#] citations resolve to."
          >
            {result.sources.length === 0 ? (
              <p className="state__message">
                No sources — a model_only run cites nothing by design.
              </p>
            ) : (
              <ul className="planned__list">
                {result.sources.map((source) => (
                  <li key={source.label}>
                    <strong>[{source.label.replace(/^\[|\]$/g, '')}]</strong>{' '}
                    {source.url ? (
                      <a href={source.url} target="_blank" rel="noreferrer">
                        {source.title ?? source.url}
                      </a>
                    ) : (
                      source.title ?? '(untitled)'
                    )}{' '}
                    — {source.source_type}
                    {source.year ? `, ${source.year}` : ''} · {source.chunks_used} passage
                    {source.chunks_used === 1 ? '' : 's'} ·{' '}
                    <Badge tone={source.cited_in_report ? 'positive' : 'neutral'}>
                      {source.cited_in_report ? 'cited in report' : 'retrieved, not cited'}
                    </Badge>
                  </li>
                ))}
              </ul>
            )}
          </Card>

          <Card
            title="Execution metadata"
            description="What the run actually did and spent. Observations, never estimates."
          >
            {result.execution_metadata.provider_failures.length > 0 && (
              <p className="note note--warn">
                <Info size={13} aria-hidden="true" /> Provider failures:{' '}
                {result.execution_metadata.provider_failures
                  .map((failure) => `${failure.provider} (${failure.code})`)
                  .join(', ')}{' '}
                — the run proceeded on the remaining providers.
              </p>
            )}
            {result.execution_metadata.warnings.map((warning) => (
              <p className="note note--warn" key={warning}>
                <Info size={13} aria-hidden="true" /> {warning}
              </p>
            ))}
            <pre className="code-block">
              <code>{JSON.stringify(result.execution_metadata, null, 2)}</code>
            </pre>
          </Card>
        </>
      )}

      <Card title="Request this form will send" description="Shape only — this is not a result.">
        <pre className="code-block">
          <code>{JSON.stringify({ topic: trimmed || '<your topic>', mode, depth }, null, 2)}</code>
        </pre>
      </Card>
    </>
  )
}
