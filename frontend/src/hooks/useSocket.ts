import { useCallback, useEffect, useRef } from 'react'
import { useStore } from '../state/store'
import { sessionRefused, socketUrl } from '../lib/api'
import type { JarvisEvent } from '../lib/events'

const RECONNECT_MS = [500, 1000, 2000, 4000, 8000]

/**
 * The single connection to JARVIS. Every state change in the UI arrives here;
 * commands go back over the same socket. Reconnects with backoff so a backend
 * restart doesn't require a page reload — unless the server no longer
 * accepts this tab's session token (a window left open from an earlier run),
 * which retrying can't fix, so it stops and says so.
 */
export function useSocket() {
  const socketRef = useRef<WebSocket | null>(null)
  const attemptRef = useRef(0)
  const closedRef = useRef(false)
  const apply = useStore((s) => s.apply)
  const setConnected = useStore((s) => s.setConnected)
  const setSessionExpired = useStore((s) => s.setSessionExpired)

  const connect = useCallback(() => {
    if (closedRef.current) return
    const socket = new WebSocket(socketUrl())
    socketRef.current = socket
    let opened = false

    socket.onopen = () => {
      opened = true
      attemptRef.current = 0
      setConnected(true)
      setSessionExpired(false)
    }
    socket.onmessage = (raw) => {
      try {
        apply(JSON.parse(raw.data) as JarvisEvent)
      } catch {
        /* a malformed frame must never break the UI */
      }
    }
    socket.onclose = async () => {
      setConnected(false)
      if (closedRef.current) return
      // A refused handshake looks exactly like an unreachable server from
      // here, so ask the API which one it was before deciding to retry.
      if (!opened && (await sessionRefused())) {
        setSessionExpired(true)
        return
      }
      if (closedRef.current) return
      const delay = RECONNECT_MS[Math.min(attemptRef.current++, RECONNECT_MS.length - 1)]
      setTimeout(connect, delay)
    }
    socket.onerror = () => socket.close()
  }, [apply, setConnected, setSessionExpired])

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
