import { useEffect, useRef, useState } from 'react'
import { useStore } from '../state/store'
import { VoiceIndicator } from './VoiceIndicator'

const STATE_LABEL: Record<string, string> = {
  off: 'mic off',
  waiting_for_wake: 'listening for wake word',
  listening: 'listening',
  transcribing: 'transcribing',
  speaking: 'speaking',
}

/**
 * Compact voice status for the status bar. Replaces the always-visible
 * VoiceIndicator rail panel — the same detail (wake word, recogniser,
 * speech engine, level meter, on/off toggle) is one click away instead of
 * permanently on screen for a signal that's idle most of the time.
 */
export function VoiceChip({ send }: { send: (m: Record<string, unknown>) => boolean }) {
  const [open, setOpen] = useState(false)
  const ref = useRef<HTMLDivElement | null>(null)

  const voiceState = useStore((s) => s.voiceState)
  const speaking = useStore((s) => s.speaking)
  const status = useStore((s) => s.status)

  const micOk = status?.voice?.microphone?.ok
  const sttOk = status?.voice?.stt?.ok
  const live = voiceState !== 'off'
  const label = speaking ? STATE_LABEL.speaking : (STATE_LABEL[voiceState] ?? voiceState)

  useEffect(() => {
    if (!open) return
    const onClick = (event: MouseEvent) => {
      if (ref.current && !ref.current.contains(event.target as Node)) setOpen(false)
    }
    document.addEventListener('mousedown', onClick)
    return () => document.removeEventListener('mousedown', onClick)
  }, [open])

  return (
    <div className="voicechip" ref={ref}>
      <button
        className={`voicechip__button${live ? ' is-live' : ''}`}
        onClick={() => setOpen((value) => !value)}
        title={micOk && sttOk ? 'Voice status' : 'Local voice input is unavailable'}
      >
        <span className="voicechip__dot" data-state={voiceState} data-ok={Boolean(micOk && sttOk)} />
        <span className="voicechip__label">{label}</span>
      </button>
      {open && (
        <div className="voicechip__popover">
          <VoiceIndicator send={send} />
        </div>
      )}
    </div>
  )
}
