import { useCallback, useEffect, useState } from 'react'
import { useStore } from '../state/store'

interface Permission {
  kind: string
  label: string
  why: string
  granted: boolean
  note: string
}

/**
 * First run.
 *
 * Permissions are explained before they are requested, and only the ones whose
 * capability is enabled are asked for — JARVIS does not demand the whole
 * privacy surface of the machine on day one.
 */
export function Onboarding() {
  const show = useStore((s) => s.showOnboarding)
  const setShow = useStore((s) => s.setShowOnboarding)
  const status = useStore((s) => s.status)
  const [permissions, setPermissions] = useState<Permission[]>([])
  const [checking, setChecking] = useState(false)
  const [step, setStep] = useState(0)

  const check = useCallback(async () => {
    setChecking(true)
    try {
      const response = await fetch('/api/permissions')
      const data = await response.json()
      setPermissions(data.permissions ?? [])
    } catch {
      setPermissions([])
    } finally {
      setChecking(false)
    }
  }, [])

  useEffect(() => {
    if (show && step === 1) check()
  }, [show, step, check])

  if (!show) return null

  const finish = async () => {
    await fetch('/api/config', {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ onboarding_complete: true }),
    })
    setShow(false)
  }

  const models = status?.models?.slots ?? {}
  const modelsReady = Object.values<any>(models).some((slot) => slot.ready)

  return (
    <div className="sheet sheet--onboarding" role="dialog" aria-modal="true">
      <div className="sheet__card">
        {step === 0 && (
          <>
            <header className="sheet__header">
              <h2>Good day, sir.</h2>
            </header>
            <div className="sheet__body onboarding__intro">
              <p>
                I'm JARVIS — a local assistant for this Mac. I run on your machine: speech
                recognition, reasoning and memory all stay here, and nothing is sent to a paid
                service unless you configure one.
              </p>
              <ul className="onboarding__list">
                <li><strong>Say “Jarvis”</strong> to wake me, then ask.</li>
                <li><strong>Simple things are instant</strong> — the time, storage, opening an app.</li>
                <li><strong>Long work runs in the background</strong> — research, mail, diagnostics.</li>
                <li><strong>Anything risky asks first</strong> — sending mail, deleting files.</li>
              </ul>
              <div className={`onboarding__status${modelsReady ? ' is-ok' : ' is-warn'}`}>
                {modelsReady
                  ? `Local model ready: ${models.general?.resolved ?? models.fast?.resolved}`
                  : 'No local model detected. Install Ollama and pull a model — commands still work without one.'}
              </div>
            </div>
            <footer className="sheet__footer">
              <span className="sheet__path">Workspace: {status?.workspace}</span>
              <button className="btn btn--primary" onClick={() => setStep(1)}>Continue</button>
            </footer>
          </>
        )}

        {step === 1 && (
          <>
            <header className="sheet__header">
              <h2>Permissions</h2>
              <button className="sheet__close" onClick={finish} aria-label="Skip">×</button>
            </header>
            <div className="sheet__body">
              <p className="onboarding__lead">
                Each of these unlocks one capability. Grant only what you want me to do —
                everything else keeps working.
              </p>
              <ul className="onboarding__permissions">
                {permissions.map((permission) => (
                  <li key={permission.kind} data-granted={permission.granted}>
                    <div>
                      <span className="permission__label">{permission.label}</span>
                      <span className="permission__why">{permission.why}</span>
                    </div>
                    {permission.granted ? (
                      <span className="permission__granted">Granted</span>
                    ) : (
                      <button
                        className="btn btn--ghost btn--small"
                        onClick={async () => {
                          await fetch('/api/permissions/open', {
                            method: 'POST',
                            headers: { 'Content-Type': 'application/json' },
                            body: JSON.stringify({ kind: permission.kind }),
                          })
                        }}
                      >
                        Open Settings
                      </button>
                    )}
                  </li>
                ))}
                {permissions.length === 0 && !checking && (
                  <li className="onboarding__empty">
                    No macOS permissions are needed on this host.
                  </li>
                )}
              </ul>
            </div>
            <footer className="sheet__footer">
              <button className="btn btn--ghost" onClick={check} disabled={checking}>
                {checking ? 'Checking…' : 'Re-check'}
              </button>
              <button className="btn btn--primary" onClick={finish}>Begin</button>
            </footer>
          </>
        )}
      </div>
    </div>
  )
}
