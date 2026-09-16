import { useEffect, useRef } from 'react'
import { useStore } from '../state/store'
import type { Message } from '../lib/events'

/**
 * The exchange itself. Not a chat app: user speech is shown as a transcript
 * line, JARVIS' replies as spoken responses, and the detail of any result lives
 * in the panels beside it rather than being dumped into the stream.
 */
export function Conversation() {
  const messages = useStore((s) => s.messages)
  const devMode = useStore((s) => s.devMode)
  const endRef = useRef<HTMLDivElement | null>(null)

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: 'smooth', block: 'end' })
  }, [messages.length, messages[messages.length - 1]?.text])

  if (!messages.length) {
    return (
      <div className="conversation conversation--empty">
        <p className="conversation__hint">
          Say <em>“Jarvis”</em> to wake me, or type below.
        </p>
      </div>
    )
  }

  return (
    <div className="conversation">
      {messages.map((message) => (
        <Line key={message.id} message={message} devMode={devMode} />
      ))}
      <div ref={endRef} />
    </div>
  )
}

function Line({ message, devMode }: { message: Message; devMode: boolean }) {
  const isUser = message.role === 'user'
  return (
    <article className={`line line--${message.role}${message.error ? ' line--error' : ''}`}>
      <div className="line__gutter">
        <span className="line__marker" aria-hidden="true" />
        <span className="line__who">{isUser ? 'YOU' : 'JARVIS'}</span>
      </div>
      <div className="line__body">
        <p className="line__text">
          {message.text}
          {message.streaming && <span className="line__caret" />}
        </p>
        {devMode && message.route && (
          <p className="line__meta">
            {message.route} · {message.path}
          </p>
        )}
      </div>
    </article>
  )
}
