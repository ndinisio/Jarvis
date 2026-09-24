import { useEffect, useState } from 'react'
import { useStore } from '../state/store'
import { apiFetch } from '../lib/api'
import type { RequestTiming } from '../lib/events'

/**
 * Developer mode.
 *
 * The point of this panel is to make the *cost* of every request obvious: which
 * path the router took, which model answered, and how long each stage took. If
 * something simple is being sent to a slow model, this is where it shows.
 */
export function DevPanel() {
  const traces = useStore((s) => s.traces)
  const reasoningTrace = useStore((s) => s.reasoningTrace)
  const telemetry = useStore((s) => s.telemetry)
  const live = useStore((s) => s.timings)
  const status = useStore((s) => s.status)
  const [summary, setSummary] = useState<Record<string, any>>({})
  const [loaded, setLoaded] = useState<RequestTiming[]>([])

  useEffect(() => {
    let alive = true
    const load = async () => {
      try {
        const response = await apiFetch('/api/telemetry')
        const data = await response.json()
        if (alive) {
          setSummary(data.summary ?? {})
          setLoaded(data.requests ?? [])
        }
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
  // What the server remembers, updated by what the socket has said since.
  const byId = new Map<string, RequestTiming>()
  for (const timing of [...loaded, ...live]) byId.set(timing.id, timing)
  const requests = [...byId.values()].sort((a, b) => b.started - a.started).slice(0, 6)
  const spans = telemetry.slice(-10).reverse()
  const thinking = reasoningTrace.slice(-12).reverse()

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

      <h3 className="dev__heading">Requests</h3>
      <ul className="dev__requests">
        {requests.length === 0 && <li className="dev__empty">No requests yet.</li>}
        {requests.map((timing) => <RequestRow key={timing.id} timing={timing} />)}
      </ul>

      <h3 className="dev__heading">Reasoning</h3>
      <ul className="dev__routes">
        {thinking.length === 0 && <li className="dev__empty">Nothing yet.</li>}
        {thinking.map((entry) => (
          <li key={entry.id}>
            <span className="dev__stage" data-stage={entry.stage}>{entry.stage}</span>
            <span className="dev__route">{summariseStage(entry)}</span>
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

const SHARES: { key: 'model_ms' | 'act_ms' | 'look_ms' | 'wait_ms' | 'other_ms'; label: string }[] = [
  { key: 'model_ms', label: 'model' },
  { key: 'act_ms', label: 'acting' },
  { key: 'look_ms', label: 'looking' },
  { key: 'wait_ms', label: 'waiting for pages' },
  { key: 'other_ms', label: 'other' },
]

/**
 * One request: how long until JARVIS did something, and where the rest of
 * the time went — the answer to "why was that slow?".
 */
function RequestRow({ timing }: { timing: RequestTiming }) {
  const total = Math.max(1, timing.total_ms)
  const seconds = (ms: number | null | undefined) => (ms == null ? '—' : `${(ms / 1000).toFixed(1)}s`)
  const facts = [
    `first action ${seconds(timing.first_action_ms)}`,
    timing.background ? `answered ${seconds(timing.answered_ms)}` : '',
    timing.heard_to_spoken_ms != null ? `heard→spoken ${seconds(timing.heard_to_spoken_ms)}` : '',
    `${timing.model_calls} model · ${timing.tool_calls} tool`,
    timing.prompt_tokens ? `${timing.prompt_tokens + timing.completion_tokens} tok` : '',
  ].filter(Boolean)
  return (
    <li className="dev__request">
      <div className="dev__request-head">
        <span className="dev__route" title={timing.text}>{timing.text}</span>
        <span className="dev__ms">{timing.finished ? seconds(timing.total_ms) : 'running'}</span>
      </div>
      <div className="dev__bar" role="img"
           aria-label={SHARES.map((s) => `${s.label} ${seconds(timing[s.key])}`).join(', ')}>
        {SHARES.map((s) => timing[s.key] > 0 && (
          <span key={s.key} data-share={s.key} style={{ width: `${(100 * timing[s.key]) / total}%` }}
                title={`${s.label}: ${seconds(timing[s.key])}`} />
        ))}
      </div>
      <div className="dev__request-facts">{facts.join(' · ')}</div>
    </li>
  )
}

/**
 * One line per stage. Structured state only — what was decided and what came
 * back — never the model's private reasoning, which the backend does not send.
 */
function summariseStage(entry: Record<string, any>): string {
  switch (entry.stage) {
    case 'intent':
      return `${entry.kind}: ${entry.goal} [${entry.confidence}]`
    case 'checklist': {
      const items = entry.items ?? []
      return `${items.filter((i: { done: boolean }) => i.done).length}/${items.length} proven`
    }
    case 'decision':
      return `${entry.action} ${entry.tool ?? ''}`.trim()
    case 'step':
      return `${entry.tool} ${entry.note ?? ''}`.trim()
    case 'result':
      return `${entry.tool} ${entry.ok ? 'ok' : 'failed'} — ${entry.summary ?? ''}`
    case 'verify':
      return entry.verified ? `verified ${entry.evidence ?? ''}` : `not verified — ${entry.problem}`
    case 'recover':
      return `${entry.strategy}: ${entry.reason ?? ''}`
    case 'clarify':
      return entry.question ?? ''
    case 'complete':
      return `${entry.steps} step(s), ${entry.tool_calls} tool, ${entry.model_calls} model, ` +
        `${entry.elapsed_ms} ms`
    default:
      return ''
  }
}
