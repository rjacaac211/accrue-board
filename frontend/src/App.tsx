import { useEffect, useState } from 'react'
import { fetchHealth, type Health } from './api'

type State = { kind: 'loading' } | { kind: 'ready'; health: Health } | { kind: 'error' }

export default function App() {
  const [state, setState] = useState<State>({ kind: 'loading' })

  useEffect(() => {
    const controller = new AbortController()
    fetchHealth(controller.signal)
      .then((health) => setState({ kind: 'ready', health }))
      .catch((error: unknown) => {
        if (!controller.signal.aborted) {
          console.error(error)
          setState({ kind: 'error' })
        }
      })
    return () => controller.abort()
  }, [])

  return (
    <main className="shell">
      <h1>AccrueBoard</h1>
      <p className="tagline">Bookkeeping pipeline and live human/AI task board.</p>
      <section aria-label="System status" className="status">
        {state.kind === 'loading' && <p>Checking API…</p>}
        {state.kind === 'error' && <p role="alert">API unreachable.</p>}
        {state.kind === 'ready' && (
          <dl>
            <dt>API</dt>
            <dd>v{state.health.version}</dd>
            <dt>Database</dt>
            <dd>{state.health.database}</dd>
          </dl>
        )}
      </section>
    </main>
  )
}
