/**
 * Loading, error and empty states.
 *
 * Driven by stubbing `fetch`, not by mocking the api client, so the client's own
 * envelope parsing is exercised at the same time.
 */

import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it } from 'vitest'

import {
  renderApp,
  stubFailingBackend,
  stubHealthyBackend,
  stubOfflineBackend,
  stubPendingBackend,
} from './utils.jsx'

describe('loading state', () => {
  it('shows while the request is in flight', () => {
    stubPendingBackend()
    renderApp('/')
    expect(screen.getByText(/reading backend capabilities/i)).toBeInTheDocument()
  })

  it('is announced to assistive technology', () => {
    stubPendingBackend()
    renderApp('/')
    const statuses = screen.getAllByRole('status')
    expect(statuses.some((node) => node.getAttribute('aria-busy') === 'true')).toBe(true)
  })

  it('is replaced by content once the request resolves', async () => {
    stubHealthyBackend()
    renderApp('/')
    expect(await screen.findByText('Semantic Scholar')).toBeInTheDocument()
    expect(screen.queryByText(/reading backend capabilities/i)).not.toBeInTheDocument()
  })
})

describe('error state', () => {
  it('reports an unreachable backend plainly', async () => {
    stubOfflineBackend()
    renderApp('/')
    const alerts = await screen.findAllByRole('alert')
    expect(alerts.length).toBeGreaterThan(0)
    expect(screen.getAllByText(/backend not reachable/i).length).toBeGreaterThan(0)
  })

  it('tells the user how to start the backend', async () => {
    stubOfflineBackend()
    renderApp('/')
    expect(await screen.findByText(/uvicorn app\.main:app/)).toBeInTheDocument()
  })

  it('shows the stable error code and the correlation id', async () => {
    stubFailingBackend(500, 'internal_error')
    renderApp('/')
    const alert = (await screen.findAllByRole('alert'))[0]
    expect(within(alert).getByText('internal_error')).toBeInTheDocument()
    expect(within(alert).getByText('abc123def456')).toBeInTheDocument()
  })

  it('offers a retry that re-issues the request', async () => {
    const user = userEvent.setup()
    const fetchMock = stubFailingBackend()
    renderApp('/')

    const retry = (await screen.findAllByRole('button', { name: /try again/i }))[0]
    const before = fetchMock.mock.calls.length
    await user.click(retry)
    await waitFor(() => expect(fetchMock.mock.calls.length).toBeGreaterThan(before))
  })

  it('shows an offline badge in the header', async () => {
    stubOfflineBackend()
    renderApp('/')
    expect(await screen.findByText(/backend offline/i)).toBeInTheDocument()
  })

  it('shows a healthy badge with version and environment when up', async () => {
    stubHealthyBackend()
    renderApp('/')
    expect(await screen.findByText(/backend v0\.1\.0 · development/i)).toBeInTheDocument()
  })
})

describe('empty state', () => {
  it('appears for research runs, and is not confused with an error', async () => {
    stubHealthyBackend()
    renderApp('/')
    expect(await screen.findByText('No research runs')).toBeInTheDocument()
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  it('explains why it is empty rather than just saying "none"', async () => {
    stubHealthyBackend()
    renderApp('/')
    expect(await screen.findByText(/the research API does not exist yet/i)).toBeInTheDocument()
  })

  it.each([
    ['/research/results', 'No reports'],
    ['/sources', 'No sources'],
    ['/evidence', 'No claims'],
    ['/evaluation', 'No benchmark runs'],
  ])('%s shows "%s"', async (path, title) => {
    stubHealthyBackend()
    renderApp(path)
    expect(await screen.findByText(title)).toBeInTheDocument()
  })
})

describe('the dashboard reports only what the backend says', () => {
  it('marks a placeholder credential as not configured', async () => {
    stubHealthyBackend()
    renderApp('/')
    await screen.findByText('OpenAI')
    const openai = screen.getByText('OpenAI').closest('li')
    expect(within(openai).getByText('not configured')).toBeInTheDocument()
  })

  it('names the credentials still missing', async () => {
    stubHealthyBackend()
    renderApp('/')
    expect(await screen.findByText(/OPENAI_API_KEY, TAVILY_API_KEY/)).toBeInTheDocument()
  })

  it('warns when the backend mode list disagrees with this build', async () => {
    stubHealthyBackend({
      '/api/v1/system/capabilities': {
        modes: ['model_only', 'hybrid'],
        integrations: {},
        missing_credentials: [],
        implemented: [],
      },
    })
    renderApp('/')
    expect(await screen.findByText(/one of them is out of date/i)).toBeInTheDocument()
  })

  it('does not warn when the mode lists agree', async () => {
    stubHealthyBackend()
    renderApp('/')
    await screen.findByText('Semantic Scholar')
    expect(screen.queryByText(/one of them is out of date/i)).not.toBeInTheDocument()
  })
})
