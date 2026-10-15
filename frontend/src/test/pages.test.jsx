/**
 * Every page renders, is reachable, and is honest about what is not built.
 */

import { screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it } from 'vitest'

import { NAV_ROUTES } from '../routes.js'
import { renderApp, stubHealthyBackend } from './utils.jsx'

beforeEach(() => {
  stubHealthyBackend()
})

describe('shell', () => {
  it('renders the brand and all six navigation links', () => {
    renderApp('/')
    expect(screen.getByText('AutoResearch AI')).toBeInTheDocument()

    const nav = screen.getByRole('navigation', { name: 'Main' })
    expect(within(nav).getAllByRole('link')).toHaveLength(6)
  })

  it('lists exactly the expected pages', () => {
    renderApp('/')
    const nav = screen.getByRole('navigation', { name: 'Main' })
    const labels = within(nav)
      .getAllByRole('link')
      .map((link) => link.textContent)
    expect(labels).toEqual([
      'Dashboard',
      'New Research',
      'Research Results',
      'Sources',
      'Evidence Audit',
      'Evaluation',
    ])
  })

  it('states in the sidebar that nothing shown is invented', () => {
    renderApp('/')
    expect(screen.getByText(/nothing shown is invented/i)).toBeInTheDocument()
  })

  it('marks the active route', () => {
    renderApp('/sources')
    const nav = screen.getByRole('navigation', { name: 'Main' })
    const active = within(nav)
      .getAllByRole('link')
      .filter((link) => link.className.includes('nav__item--active'))
    expect(active).toHaveLength(1)
    expect(active[0]).toHaveTextContent('Sources')
  })

  it('offers a skip link for keyboard users', () => {
    renderApp('/')
    expect(screen.getByRole('link', { name: /skip to content/i })).toBeInTheDocument()
  })
})

describe('each route renders its page', () => {
  it.each(NAV_ROUTES.map((route) => [route.path, route.label]))(
    '%s renders a heading',
    async (path, label) => {
      renderApp(path)
      expect(await screen.findByRole('heading', { level: 1, name: label })).toBeInTheDocument()
    },
  )
})

describe('empty states are honest', () => {
  // With no stored runs and no benchmark artifacts, every data page must say so
  // rather than showing anything that could pass for a result. The populated
  // behaviour is covered in storedRuns.test.jsx.
  it.each([['/research/results'], ['/sources'], ['/evidence']])(
    '%s explains there are no runs yet',
    async (path) => {
      renderApp(path)
      expect(await screen.findByText(/no research runs yet/i)).toBeInTheDocument()
    },
  )

  it('/evaluation explains there are no benchmark artifacts', async () => {
    renderApp('/evaluation')
    expect(await screen.findByText(/no benchmark artifacts/i)).toBeInTheDocument()
    expect(screen.getByText(/fabricated result/i)).toBeInTheDocument()
  })
})

describe('unknown routes', () => {
  it('render the not-found page inside the layout', async () => {
    renderApp('/definitely-not-a-page')
    expect(await screen.findByRole('heading', { level: 1, name: /page not found/i })).toBeInTheDocument()
    // The shell is still there, so a wrong URL does not look like a crash.
    expect(screen.getByRole('navigation', { name: 'Main' })).toBeInTheDocument()
  })
})

describe('navigation', () => {
  it('moves between pages without a reload', async () => {
    const user = userEvent.setup()
    renderApp('/')
    expect(await screen.findByRole('heading', { level: 1, name: 'Dashboard' })).toBeInTheDocument()

    await user.click(screen.getByRole('link', { name: 'Evidence Audit' }))
    expect(
      await screen.findByRole('heading', { level: 1, name: 'Evidence Audit' }),
    ).toBeInTheDocument()
  })
})
