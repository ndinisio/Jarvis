import { useStore } from '../state/store'

/** Identity, connection, model and the developer toggle. Quiet by design. */
export function StatusBar() {
  const connected = useStore((s) => s.connected)
  const status = useStore((s) => s.status)
  const devMode = useStore((s) => s.devMode)
  const setDevMode = useStore((s) => s.setDevMode)
  const setShowSettings = useStore((s) => s.setShowSettings)

  const slots = status?.models?.slots ?? {}
  const fast = slots.fast ?? {}
  const general = slots.general ?? {}
  const modelLabel = general.ready ? general.resolved : fast.ready ? fast.resolved : 'no model'

  return (
    <header className="statusbar">
      <div className="statusbar__identity">
        <span className="statusbar__mark" aria-hidden="true" />
        <span className="statusbar__name">JARVIS</span>
        <span className="statusbar__version">v{status?.version ?? '1.2'}</span>
      </div>

      <div className="statusbar__meta">
        <span className={`statusbar__pill${general.ready || fast.ready ? '' : ' is-warn'}`}>
          {modelLabel}
        </span>
        {status?.platform && !status.platform.is_macos && (
          <span className="statusbar__pill is-warn">non-macOS host</span>
        )}
        <span className={`statusbar__pill${connected ? ' is-ok' : ' is-warn'}`}>
          {connected ? 'connected' : 'reconnecting'}
        </span>
        <button
          className={`statusbar__button${devMode ? ' is-active' : ''}`}
          onClick={() => setDevMode(!devMode)}
          title="Developer mode: routing decisions, latency, tool calls"
        >
          DEV
        </button>
        <button className="statusbar__button" onClick={() => setShowSettings(true)}>
          SETTINGS
        </button>
      </div>
    </header>
  )
}
