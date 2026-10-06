/**
 * The New Research form.
 *
 * The form is interactive so the request shape can be settled now; submission is
 * impossible so nothing can fabricate a run.
 */

import { screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it } from 'vitest'

import { renderApp, stubHealthyBackend } from './utils.jsx'

beforeEach(() => {
  stubHealthyBackend()
})

describe('the form works', () => {
  it('accepts a topic', async () => {
    const user = userEvent.setup()
    renderApp('/research/new')
    const topic = screen.getByLabelText(/research topic/i)
    await user.type(topic, 'Does RAG reduce factual errors?')
    expect(topic).toHaveValue('Does RAG reduce factual errors?')
  })

  it('offers all three research configurations', () => {
    renderApp('/research/new')
    expect(screen.getAllByRole('radio')).toHaveLength(3)
  })

  it('defaults to the fully grounded configuration', () => {
    renderApp('/research/new')
    expect(screen.getByRole('radio', { name: /search grounded/i })).toBeChecked()
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

    const preview = document.querySelector('.code-block').textContent
    expect(preview).toContain('Retrieval grounding in LLMs')
    expect(preview).toContain('hybrid')
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
    await user.type(screen.getByLabelText(/research topic/i), 'Does retrieval reduce hallucination?')
    expect(screen.getByText(/request would be valid/i)).toBeInTheDocument()
  })

  it('starts out incomplete', () => {
    renderApp('/research/new')
    expect(screen.getByText(/incomplete/i)).toBeInTheDocument()
  })
})

describe('submission is impossible', () => {
  it('keeps the submit button disabled even with a valid topic', async () => {
    const user = userEvent.setup()
    renderApp('/research/new')
    await user.type(screen.getByLabelText(/research topic/i), 'Does retrieval reduce hallucination?')

    const submit = screen.getByRole('button', { name: /start research/i })
    expect(submit).toBeDisabled()
    expect(submit).toHaveTextContent(/endpoint not available/i)
  })

  it('never posts to the backend', async () => {
    const user = userEvent.setup()
    const fetchMock = stubHealthyBackend()
    renderApp('/research/new')

    await user.type(screen.getByLabelText(/research topic/i), 'Does retrieval reduce hallucination?')
    await user.click(screen.getByRole('button', { name: /start research/i }))

    const posted = fetchMock.mock.calls.filter(([, init]) => init?.method === 'POST')
    expect(posted).toHaveLength(0)
  })

  it('explains that the endpoint does not exist', async () => {
    renderApp('/research/new')
    expect(
      await screen.findByText(/Starting a research run is not implemented yet/i),
    ).toBeInTheDocument()
  })
})
