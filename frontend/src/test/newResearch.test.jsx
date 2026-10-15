/**
 * The New Research form.
 *
 * The form posts to the real endpoint and waits for the finished run, so these
 * tests cover the whole arc: the request it sends, the running state while the
 * backend works, the rendered report on success, and the error envelope on
 * failure. Everything is driven through a stubbed `fetch`; no network.
 */

import { screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it } from 'vitest'

import { jsonResponse, renderApp, stubHealthyBackend } from './utils.jsx'

const VALID_TOPIC = 'Does retrieval reduce hallucination?'

/** A completed run, shaped like `ResearchStartResponse`. */
const RUN_RESULT = {
  research_id: '7f3b2a90-0000-4000-8000-000000000001',
  topic: VALID_TOPIC,
  status: 'completed',
  report: '# Findings\n\nRetrieval reduces unsupported claims [S1].',
  sources: [
    {
      label: 'S1',
      source_id: null,
      source_type: 'web',
      title: 'A study of retrieval grounding',
      url: 'https://example.com/study',
      doi: null,
      provider: 'tavily',
      year: 2024,
      venue: null,
      evidence_depth: 'full_text',
      fingerprint: 'abc123',
      chunks_used: 3,
      cited_in_report: true,
    },
  ],
  execution_metadata: {
    mode: 'search_grounded',
    depth: 'quick',
    model: 'gpt-test',
    evidence_selection: 'semantic_top_k',
    embedding_model: 'all-MiniLM-L6-v2',
    collection: 'run_abc',
    web_candidates: 5,
    academic_candidates: 2,
    sources_attached: 3,
    documents_processed: 3,
    documents_with_text: 3,
    chunks_indexed: 40,
    chunks_retrieved: 12,
    sources_in_evidence: 3,
    sources_cited: 1,
    llm_calls: 4,
    token_usage: { prompt_tokens: 1000, completion_tokens: 234, total_tokens: 1234 },
    timings_seconds: { retrieval: 8.1, synthesis: 13.4 },
    total_seconds: 21.5,
    provider_failures: [],
    warnings: [],
  },
}

beforeEach(() => {
  stubHealthyBackend()
})

async function fillValidTopic(user) {
  await user.type(screen.getByLabelText(/research topic/i), VALID_TOPIC)
}

describe('the form works', () => {
  it('accepts a topic', async () => {
    const user = userEvent.setup()
    renderApp('/research/new')
    const topic = screen.getByLabelText(/research topic/i)
    await user.type(topic, 'Does RAG reduce factual errors?')
    expect(topic).toHaveValue('Does RAG reduce factual errors?')
  })

  it('offers all three research configurations and all three depths', () => {
    renderApp('/research/new')
    expect(
      screen.getAllByRole('radio', { name: /model only|hybrid|search grounded/i }),
    ).toHaveLength(3)
    expect(screen.getAllByRole('radio', { name: /quick|medium|deep/i })).toHaveLength(3)
  })

  it('defaults to the fully grounded configuration at quick depth', () => {
    renderApp('/research/new')
    expect(screen.getByRole('radio', { name: /search grounded/i })).toBeChecked()
    expect(screen.getByRole('radio', { name: /quick/i })).toBeChecked()
  })

  it('lets the configuration be changed', async () => {
    const user = userEvent.setup()
    renderApp('/research/new')
    await user.click(screen.getByRole('radio', { name: /model only/i }))
    expect(screen.getByRole('radio', { name: /model only/i })).toBeChecked()
    expect(screen.getByRole('radio', { name: /search grounded/i })).not.toBeChecked()
  })

  it('reflects the chosen values in the request preview', async () => {
    const user = userEvent.setup()
    renderApp('/research/new')
    await user.type(screen.getByLabelText(/research topic/i), 'Retrieval grounding in LLMs')
    await user.click(screen.getByRole('radio', { name: /hybrid/i }))
    await user.click(screen.getByRole('radio', { name: /deep/i }))

    const preview = document.querySelector('.code-block').textContent
    expect(preview).toContain('Retrieval grounding in LLMs')
    expect(preview).toContain('hybrid')
    expect(preview).toContain('deep')
  })
})

describe('validation', () => {
  it('flags a topic that is too short, once the field is left', async () => {
    const user = userEvent.setup()
    renderApp('/research/new')
    const topic = screen.getByLabelText(/research topic/i)
    await user.type(topic, 'RAG')
    await user.tab()
    expect(screen.getByText(/too short/i)).toBeInTheDocument()
    expect(topic).toHaveAttribute('aria-invalid', 'true')
  })

  it('reports a long enough topic as valid', async () => {
    const user = userEvent.setup()
    renderApp('/research/new')
    await fillValidTopic(user)
    expect(screen.getByText(/request is valid/i)).toBeInTheDocument()
  })

  it('keeps submit disabled until the topic is valid', async () => {
    const user = userEvent.setup()
    renderApp('/research/new')
    const submit = screen.getByRole('button', { name: /start research/i })
    expect(submit).toBeDisabled()
    await fillValidTopic(user)
    expect(submit).toBeEnabled()
  })
})

describe('submission', () => {
  it('posts exactly the previewed request to /api/v1/research/start', async () => {
    const user = userEvent.setup()
    const fetchMock = stubHealthyBackend({ '/api/v1/research/start': RUN_RESULT })
    renderApp('/research/new')
    await fillValidTopic(user)
    await user.click(screen.getByRole('radio', { name: /hybrid/i }))
    await user.click(screen.getByRole('button', { name: /start research/i }))

    await screen.findByText(/Retrieval reduces unsupported claims/i)
    const [url, init] = fetchMock.mock.calls.find(([, options]) => options?.method === 'POST')
    expect(String(url)).toContain('/api/v1/research/start')
    expect(JSON.parse(init.body)).toEqual({ topic: VALID_TOPIC, mode: 'hybrid', depth: 'quick' })
  })

  it('shows the running state while the backend works', async () => {
    const user = userEvent.setup()
    const fetchMock = stubHealthyBackend()
    const respond = fetchMock.getMockImplementation()
    fetchMock.mockImplementation((url, init) =>
      init?.method === 'POST' ? new Promise(() => {}) : respond(url, init),
    )
    renderApp('/research/new')
    await fillValidTopic(user)
    await user.click(screen.getByRole('button', { name: /start research/i }))

    const running = await screen.findByRole('button', { name: /running/i })
    expect(running).toBeDisabled()
  })

  it('renders the report, sources and metadata verbatim on success', async () => {
    const user = userEvent.setup()
    stubHealthyBackend({ '/api/v1/research/start': RUN_RESULT })
    renderApp('/research/new')
    await fillValidTopic(user)
    await user.click(screen.getByRole('button', { name: /start research/i }))

    expect(await screen.findByText(/Retrieval reduces unsupported claims/i)).toBeInTheDocument()
    expect(screen.getByText('completed')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: /a study of retrieval grounding/i })).toHaveAttribute(
      'href',
      'https://example.com/study',
    )
    expect(screen.getByText(/cited in report/i)).toBeInTheDocument()
    // Metadata is shown as the backend reported it, not summarized away.
    expect(screen.getByText(/"total_seconds": 21.5/)).toBeInTheDocument()
  })

  it('shows the error envelope, with its code and request id, on failure', async () => {
    const user = userEvent.setup()
    const fetchMock = stubHealthyBackend()
    const respond = fetchMock.getMockImplementation()
    fetchMock.mockImplementation((url, init) =>
      init?.method === 'POST'
        ? jsonResponse(
            {
              error: {
                code: 'no_evidence',
                message: 'No usable evidence was found for this topic.',
                details: {},
                request_id: 'req-42',
              },
            },
            { status: 422 },
          )
        : respond(url, init),
    )
    renderApp('/research/new')
    await fillValidTopic(user)
    await user.click(screen.getByRole('button', { name: /start research/i }))

    expect(await screen.findByText(/no usable evidence/i)).toBeInTheDocument()
    expect(screen.getByText('no_evidence')).toBeInTheDocument()
    expect(screen.getByText('req-42')).toBeInTheDocument()
    // The form recovers: submit is enabled again for a retry.
    expect(screen.getByRole('button', { name: /start research/i })).toBeEnabled()
  })
})
