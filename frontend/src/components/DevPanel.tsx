import { useEffect, useState } from 'react'
import { useStore } from '../state/store'

/**
 * Developer mode.
 *
 * The point of this panel is to make the *cost* of every request obvious: which
 * path the router took, which model answered, and how long each stage took. If
 * something simple is being sent to a slow model, this is where it shows.
 */
export function DevPanel() {
  const traces = useStore((s) => s.traces)
  const telemetry = useStore((s) => s.telemetry)
  const status = useStore((s) => s.status)
  const [summary, setSummary] = useState<Record<string, any>>({})

  useEffect(() => {
    let alive = true
    const load = async () => {
      try {
        const response = await fetch('/api/telemetry')
        const data = await response.json()
        if (alive) setSummary(data.summary ?? {})
      } catch {
        /* the socket is the source of truth; this is a convenience */
      }
    }
    load()
    const timer = setInterval(load, 4000)
    return () => {
      alive = false
      clearInterval(timer)
    }
  }, [])

  const recent = traces.slice(-8).reverse()
  const spans = telemetry.slice(-10).reverse()

  return (
    <section className="dev">
      <header className="panel__header">
        <h2>Diagnostics</h2>
      </header>

      <h3 className="dev__heading">Routing</h3>
      <ul className="dev__routes">
        {recent.length === 0 && <li className="dev__empty">No requests yet.</li>}
        {recent.map((trace) => (
          <li key={trace.id}>
            <span className="dev__path" data-path={trace.path}>{trace.path}</span>
            <span className="dev__route">{trace.kind}:{trace.name}</span>
            <span className="dev__ms">{trace.latency_ms.toFixed(1)} ms</span>
          </li>
        ))}
      </ul>

      <h3 className="dev__heading">Latency (p50 / p95)</h3>
      <ul className="dev__metrics">
        {Object.entries(summary).slice(0, 10).map(([name, value]: [string, any]) => (
          <li key={name}>
            <span className="dev__metric">{name}</span>
            <span className="dev__ms">
              {value.p50_ms?.toFixed?.(0)} / {value.p95_ms?.toFixed?.(0)} ms
              <em> ×{value.count}</em>
            </span>
          </li>
        ))}
        {Object.keys(summary).length === 0 && <li className="dev__empty">Collecting…</li>}
      </ul>

      {spans.length > 0 && (
        <>
          <h3 className="dev__heading">Recent spans</h3>
          <ul className="dev__metrics">
            {spans.map((span, index) => (
              <li key={index}>
                <span className="dev__metric">{span.name}</span>
                <span className="dev__ms">{span.duration_ms.toFixed(1)} ms</span>
              </li>
            ))}
          </ul>
        </>
      )}

      <h3 className="dev__heading">Model slots</h3>
      <ul className="dev__metrics">
        {Object.entries(status?.models?.slots ?? {}).map(([slot, info]: [string, any]) => (
          <li key={slot}>
            <span className="dev__metric">{slot}</span>
            <span className={`dev__ms${info.ready ? '' : ' is-warn'}`}>
              {info.ready ? info.resolved : info.reason ?? 'unavailable'}
            </span>
          </li>
        ))}
      </ul>
    </section>
  )
}
