/**
 * The bridge to JARVIS's Mac app (macapp/), when the interface runs inside it.
 *
 * The app injects `window.__JARVIS_NATIVE__` and a `jarvis` message handler
 * before the page loads. The page tells the app about the two moments worth
 * a notification once you've switched to something else — JARVIS needs your
 * OK, or a task has finished — and the app's global shortcut can start
 * push-to-talk through `window.jarvisNative`. In a browser tab none of this
 * exists and every call here does nothing.
 */
import { EV, type JarvisEvent } from './events'

interface NativeMessage {
  kind: 'confirm' | 'task'
  title: string
  body: string
  id: string
}

declare global {
  interface Window {
    __JARVIS_NATIVE__?: boolean
    webkit?: { messageHandlers?: { jarvis?: { postMessage: (message: NativeMessage) => void } } }
    jarvisNative?: { pushToTalk?: () => void }
  }
}

export function inNativeShell(): boolean {
  return Boolean(window.__JARVIS_NATIVE__ && window.webkit?.messageHandlers?.jarvis)
}

function tell(message: NativeMessage): void {
  try {
    window.webkit?.messageHandlers?.jarvis?.postMessage(message)
  } catch {
    /* the app went away; nothing to tell */
  }
}

/** Pass the moments that matter on to the app, which decides whether you
 * need a notification (only when its window isn't the one you're using). */
export function notifyShell(event: JarvisEvent): void {
  if (!inNativeShell()) return
  if (event.type === EV.CONFIRM_REQUEST) {
    const handoff = Boolean(event.details?.handoff)
    tell({
      kind: 'confirm',
      title: handoff ? 'JARVIS needs you for a moment' : 'JARVIS is asking first',
      body: String(event.summary ?? ''),
      id: String(event.id ?? ''),
    })
  } else if (event.type === EV.TASK_FINISHED) {
    const status = String(event.status ?? '')
    tell({
      kind: 'task',
      title: status === 'succeeded' ? 'Done' : status === 'cancelled' ? 'Stopped' : 'Couldn’t finish',
      body: String(event.title ?? ''),
      id: String(event.id ?? ''),
    })
  }
}

/** Let the app's global shortcut start (or stop) push-to-talk. */
export function offerPushToTalk(toggle: () => void): () => void {
  if (!inNativeShell()) return () => {}
  window.jarvisNative = { ...(window.jarvisNative ?? {}), pushToTalk: toggle }
  return () => {
    if (window.jarvisNative?.pushToTalk === toggle) delete window.jarvisNative.pushToTalk
  }
}
