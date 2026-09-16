import { useCallback, useEffect, useRef } from 'react'
import { useStore } from '../state/store'
import type { JarvisEvent } from '../lib/events'

const RECONNECT_MS = [500, 1000, 2000, 4000, 8000]

/**
 * The single connection to JARVIS. Every state change in the UI arrives here;
 * commands go back over the same socket. Reconnects with backoff so a backend
 * restart doesn't require a page reload.
 */
export function useSocket() {
  const socketRef = useRef<WebSocket | null>(null)
  const attemptRef = useRef(0)
  const closedRef = useRef(false)
  const apply = useStore((s) => s.apply)
  const setConnected = useStore((s) => s.setConnected)

  const connect = useCallback(() => {
    if (closedRef.current) return
    const protocol = location.protocol === 'https:' ? 'wss' : 'ws'
    const host = import.meta.env.DEV ? location.host : location.host
    const socket = new WebSocket(`${protocol}://${host}/ws`)
    socketRef.current = socket

    socket.onopen = () => {
      attemptRef.current = 0
      setConnected(true)
    }
    socket.onmessage = (raw) => {
      try {
        apply(JSON.parse(raw.data) as JarvisEvent)
      } catch {
        /* a malformed frame must never break the UI */
      }
    }
    socket.onclose = () => {
      setConnected(false)
      if (closedRef.current) return
      const delay = RECONNECT_MS[Math.min(attemptRef.current++, RECONNECT_MS.length - 1)]
      setTimeout(connect, delay)
    }
    socket.onerror = () => socket.close()
  }, [apply, setConnected])

  useEffect(() => {
    closedRef.current = false
    connect()
    return () => {
      closedRef.current = true
      socketRef.current?.close()
    }
  }, [connect])

  const send = useCallback((message: Record<string, unknown>) => {
    const socket = socketRef.current
    if (socket && socket.readyState === WebSocket.OPEN) {
      socket.send(JSON.stringify(message))
      return true
    }
    return false
  }, [])

  return { send }
}
