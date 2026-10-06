/**
 * New Research.
 *
 * The form is real and fully interactive -- typing, mode selection and validation
 * all work -- but submission is disabled, because `POST /api/v1/research` does not
 * exist. Building the form now settles the shape of the request before the
 * endpoint is written; leaving submit disabled means nothing can produce a
 * fabricated run.
 */

import { Info, Lock } from 'lucide-react'
import { useState } from 'react'

import { Badge, Card, PageHeader } from '../components/ui/primitives.jsx'
import { NotImplemented } from '../components/ui/states.jsx'
import { RESEARCH_MODES, STAGES } from '../lib/constants.js'

const MIN_TOPIC_LENGTH = 12

export default function NewResearchPage() {
  const [topic, setTopic] = useState('')
  const [mode, setMode] = useState('search_grounded')
  const [touched, setTouched] = useState(false)

  const trimmed = topic.trim()
  const tooShort = trimmed.length > 0 && trimmed.length < MIN_TOPIC_LENGTH
  const isValid = trimmed.length >= MIN_TOPIC_LENGTH
  const selected = RESEARCH_MODES.find((m) => m.value === mode)

  return (
    <>
      <PageHeader
        title="New Research"
        description="Choose a topic and a research configuration."
      />

      <Card
        title="Research request"
        description="This is the request body the backend will accept once the endpoint exists."
      >
        <form
          className="form"
          onSubmit={(event) => {
            // Submission is impossible by design; this only stops a stray Enter
            // key from reloading the page.
            event.preventDefault()
          }}
        >
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
              aria-describedby="topic-help"
              aria-invalid={touched && tooShort ? 'true' : undefined}
            />
            <p className="field__help" id="topic-help">
              {touched && tooShort
                ? `Too short — give at least ${MIN_TOPIC_LENGTH} characters so the planner has something to decompose.`
                : 'A question works better than a keyword: the planner decomposes it into sub-questions.'}
            </p>
          </div>

          <fieldset className="field">
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

          {selected && (
            <p className="note">
              <Info size={13} aria-hidden="true" /> {selected.detail}
            </p>
          )}

          <div className="form__actions">
            <button type="submit" className="btn btn--primary" disabled aria-disabled="true">
              <Lock size={14} aria-hidden="true" />
              Start research — endpoint not available
            </button>
            <Badge tone={isValid ? 'positive' : 'neutral'}>
              {isValid ? 'request would be valid' : 'incomplete'}
            </Badge>
          </div>
        </form>
      </Card>

      <Card title="Why submit is disabled">
        <NotImplemented feature="Starting a research run" stage={STAGES.DATA_MODEL}>
          <p className="state__message">
            There is no <code>POST /api/v1/research</code> yet, so there is nothing to submit to.
            The button stays disabled rather than calling a route that does not exist — a fake
            success or a confusing 404 would both be worse than saying so.
          </p>
        </NotImplemented>
      </Card>

      <Card title="Request this form will send" description="Shape only — this is not a result.">
        <pre className="code-block">
          <code>{JSON.stringify({ topic: trimmed || '<your topic>', mode }, null, 2)}</code>
        </pre>
      </Card>
    </>
  )
}
