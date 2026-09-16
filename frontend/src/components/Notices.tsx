import { useEffect } from 'react'
import { useStore } from '../state/store'

const DISMISS_AFTER_MS = 12000

/** Transient, dismissible problems. Never a stack trace — those go to the log. */
export function Notices() {
  const notices = useStore((s) => s.notices)
  const dismiss = useStore((s) => s.dismissNotice)

  // A notice states a condition; it shouldn't sit on the interface forever.
  useEffect(() => {
    if (!notices.length) return
    const timers = notices.map((notice) =>
      window.setTimeout(() => dismiss(notice.id), DISMISS_AFTER_MS),
    )
    return () => timers.forEach(window.clearTimeout)
  }, [notices, dismiss])

  if (!notices.length) return null
  return (
    <div className="notices">
      {notices.map((notice) => (
        <div key={notice.id} className={`notice notice--${notice.level}`} role="status">
          <span>{notice.message}</span>
          <button onClick={() => dismiss(notice.id)} aria-label="Dismiss">×</button>
        </div>
      ))}
    </div>
  )
}
