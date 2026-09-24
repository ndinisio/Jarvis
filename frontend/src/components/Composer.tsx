import { useEffect, useRef, useState } from 'react'
import { useStore } from '../state/store'
import { usePushToTalk } from '../hooks/usePushToTalk'

interface Props {
  send: (message: Record<string, unknown>) => boolean
}

/**
 * Text entry and push-to-talk. Text is a first-class input, not a fallback:
 * typing is faster than speaking for a URL, and it keeps JARVIS usable in a
 * quiet room.
 */
export function Composer({ send }: Props) {
  const [value, setValue] = useState('')
  const inputRef = useRef<HTMLInputElement | null>(null)
  const pushLocalMessage = useStore((s) => s.pushLocalMessage)
  const connected = useStore((s) => s.connected)
  const sessionExpired = useStore((s) => s.sessionExpired)
  const assistantState = useStore((s) => s.assistantState)
  const { recording, start, stop, error } = usePushToTalk()

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === '/' && document.activeElement !== inputRef.current) {
        event.preventDefault()
        inputRef.current?.focus()
      }
      if (event.key === 'Escape') {
        send({ type: 'cancel' })
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [send])

  const submit = (event: React.FormEvent) => {
    event.preventDefault()
    const text = value.trim()
    if (!text) return
    pushLocalMessage(text)
    send({ type: 'utterance', text, source: 'text' })
    setValue('')
  }

  const toggleRecording = async () => {
    if (recording) {
      await stop()
    } else {
      await start()
    }
  }

  const busy = ['processing', 'executing', 'researching'].includes(assistantState)

  return (
    <div className="composer">
      {error && <p className="composer__error">{error}</p>}
      <form className="composer__row" onSubmit={submit}>
        <button
          type="button"
          className={`composer__mic${recording ? ' is-recording' : ''}`}
          onClick={toggleRecording}
          title={recording ? 'Stop and transcribe' : 'Hold a thought — push to talk'}
          aria-label="Push to talk"
        >
          <MicIcon />
        </button>
        <input
          ref={inputRef}
          className="composer__input"
          value={value}
          onChange={(event) => setValue(event.target.value)}
          placeholder={recording ? 'Listening…'
            : connected ? 'Ask JARVIS…  (press / to focus)'
            : sessionExpired ? 'This window is from an earlier JARVIS run — open the link JARVIS printed when it started'
            : 'Reconnecting…'}
          disabled={!connected}
          spellCheck={false}
          autoComplete="off"
        />
        {busy ? (
          <button type="button" className="composer__stop" onClick={() => send({ type: 'cancel' })}>
            STOP
          </button>
        ) : (
          <button type="submit" className="composer__send" disabled={!value.trim() || !connected}>
            SEND
          </button>
        )}
      </form>
    </div>
  )
}

function MicIcon() {
  return (
    <svg viewBox="0 0 24 24" width="17" height="17" fill="none" stroke="currentColor" strokeWidth="1.6">
      <rect x="9" y="3" width="6" height="11" rx="3" />
      <path d="M5 11a7 7 0 0 0 14 0M12 18v3" strokeLinecap="round" />
    </svg>
  )
}
