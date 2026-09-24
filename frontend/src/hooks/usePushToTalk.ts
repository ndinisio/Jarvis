import { useCallback, useRef, useState } from 'react'
import { apiFetch } from '../lib/api'

/**
 * Browser microphone capture, used for push-to-talk and as the fallback when
 * the backend can't open the microphone itself (no sounddevice, or the user
 * prefers the browser's permission prompt).
 *
 * Audio is downsampled to 16 kHz mono 16-bit PCM — exactly what Whisper wants —
 * and posted to the backend for local transcription. Nothing leaves the machine.
 */
export function usePushToTalk(onTranscript?: (text: string) => void) {
  const [recording, setRecording] = useState(false)
  const [level, setLevel] = useState(0)
  const [error, setError] = useState<string | null>(null)
  const contextRef = useRef<AudioContext | null>(null)
  const streamRef = useRef<MediaStream | null>(null)
  const chunksRef = useRef<Float32Array[]>([])
  const processorRef = useRef<ScriptProcessorNode | null>(null)

  const stop = useCallback(async (): Promise<string> => {
    setRecording(false)
    processorRef.current?.disconnect()
    streamRef.current?.getTracks().forEach((t) => t.stop())
    const context = contextRef.current
    const chunks = chunksRef.current
    chunksRef.current = []
    processorRef.current = null
    streamRef.current = null
    if (!context || !chunks.length) return ''
    const sampleRate = context.sampleRate
    await context.close()
    contextRef.current = null

    const merged = mergeChunks(chunks)
    const resampled = resample(merged, sampleRate, 16000)
    const pcm = toPCM16(resampled)
    const base64 = arrayBufferToBase64(pcm.buffer)

    try {
      const response = await apiFetch('/api/voice/transcribe', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ audio: base64, sample_rate: 16000, dispatch: !onTranscript }),
      })
      const data = await response.json()
      if (data.error) {
        setError(data.error)
        return ''
      }
      if (data.text && onTranscript) onTranscript(data.text)
      return data.text ?? ''
    } catch (err) {
      setError(String(err))
      return ''
    }
  }, [onTranscript])

  const start = useCallback(async () => {
    setError(null)
    try {
      const stream = await navigator.mediaDevices.getUserMedia({
        audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true },
      })
      streamRef.current = stream
      const context = new AudioContext()
      contextRef.current = context
      const source = context.createMediaStreamSource(stream)
      const processor = context.createScriptProcessor(4096, 1, 1)
      processorRef.current = processor
      chunksRef.current = []
      processor.onaudioprocess = (event) => {
        const input = event.inputBuffer.getChannelData(0)
        chunksRef.current.push(new Float32Array(input))
        let sum = 0
        for (let i = 0; i < input.length; i += 16) sum += input[i] * input[i]
        setLevel(Math.sqrt(sum / (input.length / 16)))
      }
      source.connect(processor)
      processor.connect(context.destination)
      setRecording(true)
    } catch (err) {
      setError(
        'Microphone access was refused. Allow it for this page, and in System Settings → ' +
          'Privacy & Security → Microphone.',
      )
      console.warn(err)
    }
  }, [])

  return { recording, level, error, start, stop }
}

function mergeChunks(chunks: Float32Array[]): Float32Array {
  const length = chunks.reduce((total, chunk) => total + chunk.length, 0)
  const merged = new Float32Array(length)
  let offset = 0
  for (const chunk of chunks) {
    merged.set(chunk, offset)
    offset += chunk.length
  }
  return merged
}

function resample(input: Float32Array, from: number, to: number): Float32Array {
  if (from === to) return input
  const ratio = from / to
  const output = new Float32Array(Math.floor(input.length / ratio))
  for (let i = 0; i < output.length; i++) {
    const position = i * ratio
    const index = Math.floor(position)
    const fraction = position - index
    const next = Math.min(index + 1, input.length - 1)
    output[i] = input[index] * (1 - fraction) + input[next] * fraction
  }
  return output
}

function toPCM16(input: Float32Array): Int16Array {
  const output = new Int16Array(input.length)
  for (let i = 0; i < input.length; i++) {
    const clamped = Math.max(-1, Math.min(1, input[i]))
    output[i] = clamped < 0 ? clamped * 0x8000 : clamped * 0x7fff
  }
  return output
}

function arrayBufferToBase64(buffer: ArrayBufferLike): string {
  const bytes = new Uint8Array(buffer)
  let binary = ''
  const CHUNK = 0x8000
  for (let i = 0; i < bytes.length; i += CHUNK) {
    binary += String.fromCharCode(...bytes.subarray(i, i + CHUNK))
  }
  return btoa(binary)
}
