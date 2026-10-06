/**
 * The application shell: sidebar, header, and the routed page.
 *
 * Owns no data of its own beyond backend reachability, which it shows in the
 * header. Pages render their own content into the outlet.
 */

import { FlaskConical } from 'lucide-react'
import { NavLink, Outlet } from 'react-router-dom'

import { useHealth } from '../../hooks/useBackendStatus.js'
import { CURRENT_STAGE } from '../../lib/constants.js'
import { NAV_ROUTES } from '../../routes.js'
import { Badge } from '../ui/primitives.jsx'

/**
 * Backend reachability, always visible.
 *
 * Put in the header on purpose: almost every confusing failure in a project like
 * this is "the backend is not running", and a permanent indicator answers that
 * before the user starts debugging their own work.
 */
function BackendStatus() {
  const { data, error, loading } = useHealth()

  if (loading) return <Badge tone="neutral">Checking backend…</Badge>
  if (error) {
    return <Badge tone="negative">{error.isUnreachable ? 'Backend offline' : 'Backend error'}</Badge>
  }
  return (
    <Badge tone="positive">
      Backend v{data.version} · {data.environment}
    </Badge>
  )
}

export default function AppLayout() {
  return (
    <div className="shell">
      <a className="skip-link" href="#main">
        Skip to content
      </a>

      <aside className="sidebar">
        <div className="brand">
          <FlaskConical size={20} aria-hidden="true" />
          <div>
            <p className="brand__name">AutoResearch AI</p>
            <p className="brand__tagline">Evidence-grounded research</p>
          </div>
        </div>

        <nav className="nav" aria-label="Main">
          {NAV_ROUTES.map(({ path, label, icon: Icon }) => (
            <NavLink
              key={path}
              to={path}
              end={path === '/'}
              className={({ isActive }) => `nav__item${isActive ? ' nav__item--active' : ''}`}
            >
              <Icon size={16} aria-hidden="true" />
              {label}
            </NavLink>
          ))}
        </nav>

        <p className="sidebar__footer">
          <span className="sidebar__stage">
            Stage {CURRENT_STAGE.number} of {CURRENT_STAGE.total} · {CURRENT_STAGE.name}
          </span>
          <span>Research functionality is not implemented yet.</span>
        </p>
      </aside>

      <div className="content">
        <header className="topbar">
          <BackendStatus />
        </header>
        <main className="main" id="main">
          <Outlet />
        </main>
      </div>
    </div>
  )
}
