/**
 * Talking to JARVIS's own server.
 *
 * Every API call and the WebSocket carry this run's session token, which the
 * server requires (backend/jarvis/core/auth.py) so that nothing else on this
 * Mac — including a web page JARVIS is browsing — can drive it. The token
 * arrives once, in the address JARVIS opens (`/?token=…`). It's kept for this
 * tab in sessionStorage so a reload still works, and taken out of the address
 * bar so it isn't left where it can be copied or screen-shared.
 */

const STORAGE_KEY = 'jarvis.session-token'
const TOKEN_HEADER = 'X-Jarvis-Token'

function readToken(): string {
  const fromUrl = new URLSearchParams(location.search).get('token')
  if (fromUrl) {
    try {
      sessionStorage.setItem(STORAGE_KEY, fromUrl)
    } catch {
      /* storage unavailable: the token lives in memory for this page load */
    }
    const url = new URL(location.href)
    url.searchParams.delete('token')
    history.replaceState(history.state, '', url.pathname + url.search + url.hash)
    return fromUrl
  }
  try {
    return sessionStorage.getItem(STORAGE_KEY) ?? ''
  } catch {
    return ''
  }
}

export const sessionToken = readToken()

/** `fetch` for JARVIS's API: same-origin, with the session token attached. */
export function apiFetch(path: string, init: RequestInit = {}): Promise<Response> {
  const headers = new Headers(init.headers)
  if (sessionToken) headers.set(TOKEN_HEADER, sessionToken)
  return fetch(path, { ...init, headers })
}

/** The event stream's address, token included. */
export function socketUrl(): string {
  const protocol = location.protocol === 'https:' ? 'wss' : 'ws'
  return `${protocol}://${location.host}/ws?token=${encodeURIComponent(sessionToken)}`
}

/**
 * After the socket fails to connect: is this JARVIS unreachable (keep
 * retrying), or does it no longer accept this tab's token — a window left
 * open from an earlier run, which no amount of retrying will fix?
 */
export async function sessionRefused(): Promise<boolean> {
  try {
    const response = await apiFetch('/api/session')
    return response.status === 401
  } catch {
    return false // unreachable, not refused
  }
}
