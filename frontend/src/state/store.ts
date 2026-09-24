import { create } from 'zustand'
import type {
  Activity, AssistantState, Confirmation, JarvisEvent, Message, Panel,
  Reasoning, RouteTrace, Task, TelemetrySpan, TraceEntry, VoiceState,
} from '../lib/events'
import { EV } from '../lib/events'

const MAX_MESSAGES = 120
const MAX_ACTIVITIES = 60
const MAX_PANELS = 24
const MAX_TRACES = 60
const MAX_TRACE_ENTRIES = 80
const MAX_FINISHED_TASKS = 20

let counter = 0
const nextId = () => `${Date.now().toString(36)}-${(counter++).toString(36)}`

interface Status {
  version?: string
  platform?: { system: string; is_macos: boolean; machine: string }
  models?: any
  voice?: any
  capabilities?: string[]
  tools?: Record<string, string[]>
  workspace?: string
  config?: any
  onboarding_complete?: boolean
}

interface StoreState {
  connected: boolean
  /** The server no longer accepts this tab's session token (see lib/api.ts). */
  sessionExpired: boolean
  assistantState: AssistantState
  voiceState: VoiceState
  voiceLevel: number
  speaking: boolean
  speechText: string
  messages: Message[]
  activities: Activity[]
  tasks: Record<string, Task>
  panels: Panel[]
  confirmation: Confirmation | null
  status: Status
  traces: RouteTrace[]
  reasoning: Reasoning | null
  reasoningTrace: TraceEntry[]
  telemetry: TelemetrySpan[]
  notices: { id: string; level: string; message: string; ts: number }[]
  devMode: boolean
  showSettings: boolean
  showOnboarding: boolean
  lastWake: number

  setConnected: (v: boolean) => void
  setSessionExpired: (v: boolean) => void
  setDevMode: (v: boolean) => void
  setShowSettings: (v: boolean) => void
  setShowOnboarding: (v: boolean) => void
  apply: (event: JarvisEvent) => void
  pushLocalMessage: (text: string) => void
  dismissNotice: (id: string) => void
  clearConversation: () => void
}

export const useStore = create<StoreState>((set, get) => ({
  connected: false,
  sessionExpired: false,
  assistantState: 'idle',
  voiceState: 'off',
  voiceLevel: 0,
  speaking: false,
  speechText: '',
  messages: [],
  activities: [],
  tasks: {},
  panels: [],
  confirmation: null,
  status: {},
  traces: [],
  reasoning: null,
  reasoningTrace: [],
  telemetry: [],
  notices: [],
  devMode: false,
  showSettings: false,
  showOnboarding: false,
  lastWake: 0,

  setConnected: (v) => set({ connected: v }),
  setSessionExpired: (v) => set({ sessionExpired: v }),
  setDevMode: (v) => set({ devMode: v }),
  setShowSettings: (v) => set({ showSettings: v }),
  setShowOnboarding: (v) => set({ showOnboarding: v }),

  pushLocalMessage: (text) =>
    set((s) => ({
      messages: [...s.messages,
        { id: nextId(), role: 'user' as const, text, ts: Date.now() / 1000 }]
        .slice(-MAX_MESSAGES),
    })),

  dismissNotice: (id) => set((s) => ({ notices: s.notices.filter((n) => n.id !== id) })),

  clearConversation: () =>
    set({ messages: [], panels: [], activities: [], reasoning: null, reasoningTrace: [] }),

  apply: (event) => {
    const state = get()
    switch (event.type) {
      case EV.HELLO: {
        const { type, ts, seq, ...status } = event
        set({
          status: status as Status,
          devMode: status.config?.ui?.developer_mode ?? state.devMode,
          showOnboarding: status.onboarding_complete === false,
        })
        break
      }
      case EV.CONFIG:
        set((s) => ({
          status: { ...s.status, config: event.config },
          devMode: event.config?.ui?.developer_mode ?? s.devMode,
        }))
        break

      case EV.STATE:
        set({ assistantState: event.state as AssistantState })
        break

      case EV.TRANSCRIPT: {
        if (!event.final) break
        set((s) => {
          // The optimistic message added when typing is already there.
          const last = s.messages[s.messages.length - 1]
          if (last && last.role === 'user' && last.text === event.text) return s
          const message: Message = {
            id: nextId(), role: 'user', text: event.text, ts: event.ts,
          }
          return { messages: [...s.messages, message].slice(-MAX_MESSAGES) }
        })
        break
      }

      case EV.ASSISTANT_DELTA:
        set((s) => {
          const messages = [...s.messages]
          const last = messages[messages.length - 1]
          if (last && last.role === 'assistant' && last.streaming) {
            messages[messages.length - 1] = { ...last, text: last.text + event.delta }
          } else {
            const message: Message = {
              id: nextId(), role: 'assistant', text: event.delta, ts: event.ts,
              streaming: true, taskId: event.task_id ?? null,
            }
            messages.push(message)
          }
          return { messages: messages.slice(-MAX_MESSAGES) }
        })
        break

      case EV.ASSISTANT_MESSAGE:
        set((s) => {
          const messages = [...s.messages]
          const last = messages[messages.length - 1]
          const message: Message = {
            id: nextId(), role: 'assistant', text: event.text, ts: event.ts,
            route: event.route, path: event.path, error: event.error,
            taskId: event.task_id ?? null,
          }
          if (last && last.role === 'assistant' && last.streaming) {
            messages[messages.length - 1] = { ...message, id: last.id }
          } else if (event.text) {
            messages.push(message)
          }
          return { messages: messages.slice(-MAX_MESSAGES) }
        })
        break

      case EV.ACTIVITY:
        set((s) => ({
          activities: [...s.activities, {
            id: nextId(), message: event.message, ts: event.ts,
            tool: event.tool, taskId: event.task_id ?? null,
          }].slice(-MAX_ACTIVITIES),
        }))
        break

      case EV.TOOL_CALL:
        set((s) => ({
          activities: [...s.activities, {
            id: nextId(), message: describeTool(event.tool, event.args), ts: event.ts,
            category: event.category, tool: event.tool, taskId: event.task_id ?? null,
          }].slice(-MAX_ACTIVITIES),
        }))
        break

      case EV.INTELLIGENCE_TRACE: {
        const { type, seq, ...entry } = event
        set((s) => ({
          // A background errand's trace belongs to its task card, not to the
          // picture of the current turn.
          reasoning: entry.task_id ? s.reasoning : reduceReasoning(s.reasoning, entry as TraceEntry),
          reasoningTrace: [...s.reasoningTrace, { id: nextId(), ...entry } as TraceEntry]
            .slice(-MAX_TRACE_ENTRIES),
        }))
        break
      }

      case EV.TASK_CREATED:
      case EV.TASK_UPDATED:
      case EV.TASK_FINISHED:
        // Unlike every other collection here, this had no cap at all —
        // running/pending tasks are always kept (there are only ever a
        // handful at once), but finished ones accumulate forever in a long
        // session unless trimmed the same way messages/activities/panels
        // already are.
        set((s) => {
          const tasks: Record<string, Task> = { ...s.tasks, [event.id]: event as unknown as Task }
          const finished = Object.values(tasks)
            .filter((t) => t.status !== 'running' && t.status !== 'pending')
            .sort((a, b) => (b.finished ?? 0) - (a.finished ?? 0))
          for (const stale of finished.slice(MAX_FINISHED_TASKS)) delete tasks[stale.id]
          return { tasks }
        })
        break

      case EV.VOICE_STATE:
        set({
          voiceState: event.state as VoiceState,
          voiceLevel: typeof event.level === 'number' ? event.level : state.voiceLevel,
        })
        break

      case EV.WAKE:
        set({ lastWake: Date.now() })
        break

      case EV.SPEECH_START:
        set({ speaking: true, speechText: event.text ?? '' })
        break

      case EV.SPEECH_END:
        set({ speaking: false, speechText: '' })
        break

      case EV.CONFIRM_REQUEST:
        set({ confirmation: event as unknown as Confirmation })
        break

      case EV.CONFIRM_RESOLVED:
        set((s) => (s.confirmation?.id === event.id ? { confirmation: null } : s))
        break

      case EV.SCREEN_IMAGE:
        set((s) => ({
          panels: [...s.panels, {
            id: nextId(), kind: 'image', title: 'Screen', image: event.image,
            path: event.path, ts: event.ts, taskId: event.task_id ?? null,
          }].slice(-MAX_PANELS),
        }))
        break

      case EV.RESULT_PANEL: {
        const { type, ts, seq, task_id, ...rest } = event
        set((s) => ({
          panels: [...s.panels, { id: nextId(), ts, taskId: task_id ?? null, ...rest } as Panel]
            .slice(-MAX_PANELS),
        }))
        break
      }

      case EV.ROUTE:
        set((s) => ({
          traces: [...s.traces, {
            id: nextId(), kind: event.kind, name: event.name, path: event.path,
            confidence: event.confidence, latency_ms: event.latency_ms,
            reason: event.reason, ts: event.ts,
          }].slice(-MAX_TRACES),
        }))
        break

      case EV.TELEMETRY:
        set((s) => ({
          telemetry: [...s.telemetry, event as unknown as TelemetrySpan].slice(-120),
        }))
        break

      case EV.ERROR:
      case EV.NOTICE:
        set((s) => ({
          notices: [...s.notices, {
            id: nextId(), level: event.level ?? 'error',
            message: event.message ?? 'Something went wrong.', ts: event.ts,
          }].slice(-4),
        }))
        break
    }
  },
}))

/**
 * Fold one trace entry into the picture of the current turn.
 *
 * `intent` starts a new turn; everything after it refines the same picture. A
 * step is only shown as done once its result came back *and* verified, so the
 * panel never claims success the backend hasn't established.
 */
function reduceReasoning(current: Reasoning | null, entry: TraceEntry): Reasoning | null {
  switch (entry.stage) {
    case 'intent':
      return {
        objective: entry.goal ?? '',
        kind: entry.kind ?? '',
        context: entry.context ?? '',
        complexity: entry.complexity ?? '',
        steps: [],
        checklist: [],
        question: null,
        done: false,
        toolCalls: 0,
        modelCalls: 0,
        elapsedMs: 0,
      }
    case 'checklist':
      return current ? { ...current, checklist: entry.items ?? [] } : current
    case 'decision': {
      if (!current || entry.action !== 'tool_call') return current
      const label = humaniseTool(entry.tool, entry.arguments)
      const steps = [...current.steps]
      const slot = steps.findIndex((s) => s.state === 'active' || s.state === 'pending')
      if (slot === -1) steps.push({ label, state: 'active' })
      else steps[slot] = { label, state: 'active' }
      return { ...current, steps }
    }
    case 'verify': {
      if (!current) return current
      const steps = [...current.steps]
      const slot = steps.map((s) => s.state).lastIndexOf('active')
      if (slot === -1) return current
      steps[slot] = { ...steps[slot], state: entry.verified ? 'done' : 'failed' }
      const next = steps.findIndex((s) => s.state === 'pending')
      if (entry.verified && next !== -1) steps[next] = { ...steps[next], state: 'active' }
      return { ...current, steps }
    }
    case 'clarify':
      return current ? { ...current, question: entry.question ?? null, done: true } : current
    case 'complete':
      return current
        ? {
            ...current, done: true,
            toolCalls: entry.tool_calls ?? 0,
            modelCalls: entry.model_calls ?? 0,
            elapsedMs: entry.elapsed_ms ?? 0,
          }
        : current
    default:
      return current
  }
}

function humaniseTool(tool: string, args: Record<string, any> = {}): string {
  const label = String(tool ?? '').replace(/_/g, ' ')
  const hint = args?.query ?? args?.name ?? args?.url ?? args?.label ?? args?.path
  return hint ? `${label} — ${String(hint).slice(0, 48)}` : label
}

function describeTool(tool: string, args: Record<string, any> = {}): string {
  const label = tool.replace(/_/g, ' ')
  const hint = args?.name || args?.query || args?.url || args?.path || args?.question
  return hint ? `${label}: ${String(hint).slice(0, 70)}` : label
}
