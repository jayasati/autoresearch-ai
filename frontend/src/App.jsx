import { useEffect, useState } from 'react'
import { getHealth } from './api/client.js'

/**
 * Stage 1 shell: proves the frontend builds and can reach the backend.
 * Research UI (topic input, run timeline, report view, claim inspector,
 * benchmark dashboard) lands in later stages under src/features/.
 */
export default function App() {
  const [health, setHealth] = useState({ state: 'loading' })

  useEffect(() => {
    getHealth()
      .then((data) => setHealth({ state: 'ok', data }))
      .catch((err) => setHealth({ state: 'error', message: String(err) }))
  }, [])

  return (
    <main className="shell">
      <h1>AutoResearch AI</h1>
      <p className="tagline">Evidence-grounded agentic research system</p>

      <section className="card">
        <h2>Backend connection</h2>
        {health.state === 'loading' && <p>Checking…</p>}
        {health.state === 'ok' && (
          <p className="ok">
            Connected — v{health.data.version} ({health.data.env})
          </p>
        )}
        {health.state === 'error' && (
          <p className="err">
            Not reachable. Start the backend on port 8000.
            <br />
            <code>{health.message}</code>
          </p>
        )}
      </section>

      <section className="card">
        <h2>Not implemented yet</h2>
        <ul>
          <li>Topic submission &amp; run orchestration</li>
          <li>Retrieval (web / academic / RAG)</li>
          <li>Claim extraction &amp; verification</li>
          <li>Citation validation &amp; conflict detection</li>
          <li>Metrics &amp; benchmark comparison</li>
        </ul>
      </section>
    </main>
  )
}
