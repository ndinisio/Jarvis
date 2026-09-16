import { useStore } from '../state/store'

const VOICE_LABELS: Record<string, string> = {
  off: 'MIC OFF',
  waiting_for_wake: 'AWAITING WAKE WORD',
  listening: 'LISTENING',
  transcribing: 'TRANSCRIBING',
  speaking: 'SPEAKING',
}

/** Microphone, wake-word and speech state — always visible, never ambiguous. */
export function VoiceIndicator({ send }: { send: (m: Record<string, unknown>) => boolean }) {
  const voiceState = useStore((s) => s.voiceState)
  const level = useStore((s) => s.voiceLevel)
  const speaking = useStore((s) => s.speaking)
  const status = useStore((s) => s.status)
  const lastWake = useStore((s) => s.lastWake)

  const voice = status?.voice ?? {}
  const wakeWord = voice?.wake?.word ?? status?.config?.voice?.wake_word ?? 'jarvis'
  const micOk = voice?.microphone?.ok
  const sttOk = voice?.stt?.ok
  const live = voiceState !== 'off'
  const recentWake = Date.now() - lastWake < 2500

  const bars = Array.from({ length: 14 }, (_, index) => {
    const threshold = (index + 1) / 14
    const magnitude = Math.min(1, level * 9)
    return magnitude >= threshold * 0.9
  })

  return (
    <section className="voice">
      <header className="panel__header">
        <h2>Voice</h2>
        <button
          className={`voice__toggle${live ? ' is-live' : ''}`}
          onClick={() => send({ type: live ? 'voice.stop' : 'voice.start' })}
          disabled={!micOk || !sttOk}
          title={micOk && sttOk ? 'Toggle continuous listening' : 'Local voice input is unavailable'}
        >
          {live ? 'ON' : 'OFF'}
        </button>
      </header>

      <div className={`voice__state${recentWake ? ' is-woken' : ''}`} data-state={voiceState}>
        {speaking ? VOICE_LABELS.speaking : VOICE_LABELS[voiceState] ?? voiceState}
      </div>

      <div className="voice__meter" aria-hidden="true">
        {bars.map((on, index) => (
          <span key={index} className={`voice__bar${on ? ' is-on' : ''}`} />
        ))}
      </div>

      <dl className="voice__facts">
        <div>
          <dt>Wake word</dt>
          <dd>“{wakeWord}”</dd>
        </div>
        <div>
          <dt>Recogniser</dt>
          <dd className={sttOk ? '' : 'is-missing'}>
            {sttOk ? voice?.stt?.engine : 'unavailable'}
          </dd>
        </div>
        <div>
          <dt>Speech</dt>
          <dd className={voice?.tts?.ok ? '' : 'is-missing'}>
            {voice?.tts?.ok ? voice?.tts?.engine : 'unavailable'}
          </dd>
        </div>
      </dl>

      {(!micOk || !sttOk) && (
        <p className="voice__note">
          {!micOk ? voice?.microphone?.note : voice?.stt?.note}
          {' — use the microphone button to talk instead.'}
        </p>
      )}
    </section>
  )
}
