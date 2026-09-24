import { useEffect } from 'react'
import { useStore } from '../state/store'

/**
 * The confirmation gate.
 *
 * Nothing medium- or high-risk executes without passing through here. The
 * dialog states exactly what will happen and to what, because "Allow?" with no
 * detail is not consent.
 *
 * A *handoff* travels the same channel but asks for something else: JARVIS
 * has paused because a step is the user's to do (a sign-in, a CAPTCHA), and
 * waits for "Done".
 */

/** Bookkeeping the backend attaches that means nothing to a person. */
const HIDDEN_DETAILS = new Set(['offer_remember', 'handoff'])
export function ConfirmDialog({ send }: { send: (m: Record<string, unknown>) => boolean }) {
  const confirmation = useStore((s) => s.confirmation)

  useEffect(() => {
    if (!confirmation) return
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        send({ type: 'confirm.response', id: confirmation.id, approved: false })
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [confirmation, send])

  if (!confirmation) return null

  const respond = (approved: boolean, remember = false) =>
    send({ type: 'confirm.response', id: confirmation.id, approved, remember })

  const details = Object.entries(confirmation.details ?? {}).filter(([key]) => !HIDDEN_DETAILS.has(key))

  if (confirmation.details?.handoff) {
    return (
      <div className="confirm" role="dialog" aria-modal="true">
        <div className="confirm__card" data-risk="low">
          <header>
            <h2>Over to you</h2>
          </header>
          <p className="confirm__summary">{confirmation.summary}</p>
          <div className="confirm__actions">
            <button className="btn btn--ghost" onClick={() => respond(false)}>
              Stop the task
            </button>
            <button className="btn btn--primary" onClick={() => respond(true)}>
              Done
            </button>
          </div>
        </div>
      </div>
    )
  }

  return (
    <div className="confirm" role="dialog" aria-modal="true">
      <div className="confirm__card" data-risk={confirmation.risk}>
        <header>
          <span className="confirm__risk">{confirmation.risk} risk</span>
          <h2>Confirmation required</h2>
        </header>
        <p className="confirm__summary">{confirmation.summary}</p>
        {details.length > 0 && (
          <dl className="confirm__details">
            {details.map(([key, value]) => (
              <div key={key}>
                <dt>{key}</dt>
                <dd>{typeof value === 'object' ? JSON.stringify(value) : String(value)}</dd>
              </div>
            ))}
          </dl>
        )}
        <div className="confirm__actions">
          <button className="btn btn--ghost" onClick={() => respond(false)}>
            Decline
          </button>
          {confirmation.risk !== 'high' && confirmation.details?.offer_remember !== false && (
            <button className="btn btn--ghost" onClick={() => respond(true, true)}>
              Allow for this session
            </button>
          )}
          <button className="btn btn--primary" onClick={() => respond(true)}>
            Allow once
          </button>
        </div>
      </div>
    </div>
  )
}
