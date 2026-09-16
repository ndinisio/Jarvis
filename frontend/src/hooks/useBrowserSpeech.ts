import { useEffect, useRef } from 'react'
import { useStore } from '../state/store'

/**
 * Browser-side speech synthesis.
 *
 * Used when the backend's TTS engine is "browser" — off macOS, or when `say`
 * isn't available. The backend emits a speech.start event carrying the text and
 * this speaks it, reporting completion so the assistant's state stays honest.
 */
export function useBrowserSpeech(send: (m: Record<string, unknown>) => boolean) {
  const lastSpokenRef = useRef<string>('')

  useEffect(() => {
    if (!('speechSynthesis' in window)) return
    let cancelled = false

    const unsubscribe = useStore.subscribe((state, previous) => {
      if (state.speechText === previous.speechText) return
      const engine = state.status?.voice?.tts?.engine
      if (engine !== 'browser') return
      const text = state.speechText
      if (!text || text === lastSpokenRef.current) return
      lastSpokenRef.current = text

      window.speechSynthesis.cancel()
      const utterance = new SpeechSynthesisUtterance(text)
      const preferred = pickVoice(state.status?.config?.voice?.tts_voice)
      if (preferred) utterance.voice = preferred
      utterance.rate = 1.03
      utterance.pitch = 0.95
      utterance.onend = () => {
        if (!cancelled) send({ type: 'speech.finished' })
      }
      window.speechSynthesis.speak(utterance)
    })

    return () => {
      cancelled = true
      unsubscribe()
      window.speechSynthesis?.cancel()
    }
  }, [send])

  // Stop speaking the moment the assistant is interrupted.
  useEffect(() => {
    const unsubscribe = useStore.subscribe((state, previous) => {
      if (previous.speaking && !state.speaking) window.speechSynthesis?.cancel()
    })
    return unsubscribe
  }, [])
}

function pickVoice(preferred?: string): SpeechSynthesisVoice | null {
  const voices = window.speechSynthesis?.getVoices?.() ?? []
  if (!voices.length) return null
  if (preferred) {
    const exact = voices.find((v) => v.name.toLowerCase() === preferred.toLowerCase())
    if (exact) return exact
  }
  const british = voices.find((v) => /en[-_]GB/i.test(v.lang) && /daniel|male|arthur/i.test(v.name))
  return british ?? voices.find((v) => /en[-_]GB/i.test(v.lang)) ?? null
}
