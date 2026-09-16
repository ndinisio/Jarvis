// Event names shared with the backend (backend/jarvis/core/events.py).
export const EV = {
  HELLO: 'hello',
  CONFIG: 'config',
  STATE: 'state',
  ERROR: 'error',
  NOTICE: 'notice',
  TRANSCRIPT: 'transcript',
  ASSISTANT_DELTA: 'assistant.delta',
  ASSISTANT_MESSAGE: 'assistant.message',
  ROUTE: 'route',
  ACTIVITY: 'activity',
  TOOL_CALL: 'tool.call',
  TOOL_RESULT: 'tool.result',
  TASK_CREATED: 'task.created',
  TASK_UPDATED: 'task.updated',
  TASK_FINISHED: 'task.finished',
  VOICE_STATE: 'voice.state',
  SPEECH_START: 'speech.start',
  SPEECH_END: 'speech.end',
  WAKE: 'wake',
  CONFIRM_REQUEST: 'confirm.request',
  CONFIRM_RESOLVED: 'confirm.resolved',
  SCREEN_IMAGE: 'screen.image',
  RESULT_PANEL: 'result.panel',
  MEMORY: 'memory',
  TELEMETRY: 'telemetry',
} as const

export type AssistantState =
  | 'idle' | 'listening' | 'processing' | 'speaking' | 'executing'
  | 'researching' | 'awaiting_confirmation' | 'error'

export type VoiceState =
  | 'off' | 'waiting_for_wake' | 'listening' | 'transcribing' | 'speaking'

export interface JarvisEvent {
  type: string
  ts: number
  seq: number
  [key: string]: any
}

export interface Message {
  id: string
  role: 'user' | 'assistant'
  text: string
  ts: number
  route?: string
  path?: string
  error?: string | null
  streaming?: boolean
  taskId?: string | null
}

export interface Activity {
  id: string
  message: string
  ts: number
  category?: string
  tool?: string
  taskId?: string | null
}

export interface Task {
  id: string
  kind: string
  title: string
  status: 'pending' | 'running' | 'succeeded' | 'failed' | 'cancelled'
  progress: number
  steps: { message: string; ts: number }[]
  started: number
  finished?: number | null
  elapsed_s: number
  error?: string | null
  cancellable: boolean
}

export interface Panel {
  id: string
  kind: string
  title?: string
  ts: number
  taskId?: string | null
  [key: string]: any
}

export interface Confirmation {
  id: string
  action: string
  risk: 'low' | 'medium' | 'high'
  summary: string
  details: Record<string, any>
}

export interface RouteTrace {
  id: string
  kind: string
  name: string
  path: string
  confidence: number
  latency_ms: number
  reason?: string
  ts: number
}

export interface TelemetrySpan {
  name: string
  duration_ms: number
  ok: boolean
  ts: number
  [key: string]: any
}
