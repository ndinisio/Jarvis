# Architecture

## The one rule

> Use the cheapest competent execution path for every request.

Everything below follows from that. If macOS can answer a question in 20 ms, no
model is involved. If a question needs a model, the smallest one that can answer
it is used. If the work is genuinely slow, it is acknowledged in under a second
and moved to the background.

---

## A turn, end to end

```
 speech ──► wake word ──► STT ──► orchestrator.handle(text)
                                        │
                              1. publish transcript
                              2. stop any speech (barge-in)
                              3. store the user turn + begin the state turn
                              4. route  ─────────────────┐
                                        │                │
                    ┌───────────────────┴──────┐         │  telemetry span
                    │                          │         │  per stage
              quick match                 everything     │
             (a certainty)                   else        │
                    │                          │         │
             control / tool          intelligence agent   │
                    │                  understand → plan  │
                    │                  → decide → act     │
              wrong tool? ─────────►   → verify → repair   │
                    │                          │         │
                   short?  ─────┬──────────────┘  long?  ─┘
                     │          │                    │
              answer inline     │      acknowledge + spawn task
                     │          │                    │
                     │          │            progress events → UI
                     │          │                    │
                     └──────────┴───────► respond: event + speech + memory
```

### The fast path (`router/quick.py`)

Roughly 60 regular expressions mapping directly to a control handler or a tool
call with arguments. No model, no I/O beyond the tool itself. Patterns are
deliberately tight: anything ambiguous falls through rather than guessing.

Measured at ~0.02–0.1 ms of routing, 3–5 ms end to end.

A quick match is taken because it is a *certainty*, not a guess. When the tool
it chose turns out not to be the right one — `ToolResult.wrong_tool`, as when
"open the BBC" finds no such application — the turn falls through to the agent
instead of ending in a dead end. A tool that tried and *failed* does not do
this: that failure is the honest answer.

### The intelligence agent (`intelligence/`, V1.2)

Everything the fast path declines. One turn is:

```
understand (in context)  →  resolve references  →  [plan, if multi-step]
  →  decide  →  act  →  observe  →  verify  →  repair / continue / ask
  →  answer
```

Bounded by `intelligence.max_steps` (default 6) and `recovery_budget` (2). Every
tool call goes through the same registry, with the same risk gate, as every
other path. See [`intelligence.md`](intelligence.md).

Behind it, the V1.1 stages are still present and still used:

* **Heuristics** (`router/router.py::_heuristic`) — weighted keyword scoring,
  which still decides whether a request is *long-running* and therefore gets an
  immediate acknowledgement.
* **Fast-model classification** — a 1B-class model returns
  `{"capability": …, "confidence": …}` in JSON mode with a hard timeout.
  `extract_json` is forgiving, because small local models wrap JSON in prose.
* **Capabilities** — the whole V1.1 dispatch, used by the quick path and as the
  fallback when `intelligence.enabled` is off.

---

## Capabilities, not agents

A capability owns an area of competence and a handful of tools. Most are a thin
model-assisted mapping from a sentence to a tool call and share
`ToolPlanCapability`; the planner only ever sees *that capability's* tools —
three or four lines — which is what makes a small local model reliable enough.

Four capabilities implement their own flow because they genuinely differ:

| Capability | Why it is special |
| --- | --- |
| `research` | multi-step: plan → search → fetch → extract → compare → synthesise |
| `diagnostics` | measures first, then asks the model to interpret the measurements |
| `email` | triage is deterministic; drafting and sending are separate, gated steps |
| `conversation` | streaming, slot selection, layered context |

There is one orchestrator. Capabilities share the model layer, tools, memory,
permissions, task manager, telemetry and the event bus. Independent contexts are
used where they help (vision, research synthesis) — not to look sophisticated.

---

## Two kinds of context

They are different things and they are kept apart:

| | `core/context.py` | `intelligence/state.py` |
| --- | --- | --- |
| Question it answers | who is this person, what do they like, what have we discussed | what are we doing *right now* |
| Lifetime | across sessions (SQLite) | this conversation, bounded |
| Contents | identity, preferences, facts, recent turns | objective, open page, inbox, sources, screen, entities |
| Filled by | the memory store | an observer on the tool registry |
| Used for | the system prompt | reference resolution, tool shortlisting, decisions |

V1.2 deliberately did not turn into a memory project. The short-term state is
only what understanding *now* requires: a handful of turns, at most 40 entities,
and a screen description that expires after three minutes.

---

## Context assembly (`core/context.py`)

Never the whole conversation. Five layers, each included only when relevant:

1. **Identity** — the system prompt from `personality.py`
2. **Preferences** — up to six, from memory
3. **Conversation** — the last `memory.context_turns` exchanges, truncated
4. **Task context** — what the current background task is doing
5. **Tool results** — the last few, capped at 1200 characters each

Facts are retrieved by keyword overlap scored against recency and importance, so
a 40-fact memory contributes at most six lines to a prompt.

---

## Models (`models/`)

```
ModelProvider (abstract)
├── OllamaProvider              local, default
├── OpenAICompatibleProvider    LM Studio, llama.cpp, vLLM, OpenRouter, OpenAI
└── AnthropicProvider           optional

ModelRouter
├── slot resolution with substitution (configured → fallback list → heuristic)
├── streaming with time-to-first-token telemetry
├── JSON mode with forgiving extraction
└── status reporting for `jarvis doctor` and the UI
```

Slot resolution is cached for 60 seconds; a configuration change clears it. If
the configured model isn't installed, the router picks from what *is* installed —
smallest for `fast`, largest for `general`, any vision-capable model for
`vision` — and marks the resolution as substituted.

---

## Tasks (`tasks/manager.py`)

Every long operation is a `Task` with an id, status, progress, a step trail, an
elapsed clock and a cancellation token. Tools poll `ctx.cancelled()`; a task that
ignores the flag for 1.5 seconds is cancelled hard. Up to four run concurrently,
and the conversation never waits for any of them.

Cancellation reaches three places at once: the speech queue, the running task's
cancel event, and any outstanding confirmation.

---

## Events (`core/events.py`)

The UI is a projection of one event stream — there is no polling and no second
source of truth. A slow subscriber drops its oldest events rather than blocking
the orchestrator, and a reconnecting client replays the last 20 events after the
handshake.

Event families: lifecycle (`hello`, `config`, `state`, `error`, `notice`),
conversation (`transcript`, `assistant.delta`, `assistant.message`, `route`),
execution (`activity`, `tool.call`, `tool.result`, `task.*`), voice
(`voice.state`, `speech.*`, `wake`), interaction (`confirm.*`, `screen.image`,
`result.panel`, `memory`) and `telemetry`.

---

## The macOS layer (`tools/macos/controller.py`)

The single place in the codebase that shells out. Preference order:

1. AppleScript / `osascript` — semantic, supported, permission-aware
2. purpose-built CLI — `pbpaste`, `screencapture`, `pmset`, `sysctl`, `mdfind`
3. generic shell — allowlisted, and only when nothing better exists

Commands are passed as argv lists, never as shell strings, so there is no
injection surface. Every method degrades on non-Darwin hosts, which is what
keeps the test suite runnable anywhere.

---

## Telemetry (`core/telemetry.py`)

Spans for: `router.quick`, `router.heuristic`, `router.model`, `model.stream`,
`model.ttft`, `tool.<name>`, `task.<kind>`, `voice.wake`, `voice.stt`,
`voice.tts`, `turn.total`. The developer panel shows p50/p95 per span and the
routing path of recent requests — the point being to make an expensive path for
a cheap question immediately visible.

---

## Failure behaviour

| Missing | Consequence |
| --- | --- |
| Ollama | conversation and research synthesis explain themselves; all deterministic tools work |
| a slot's model | substituted from installed models, or a clear message |
| microphone / Whisper | voice input disabled with the reason; typing and push-to-talk work |
| `say` | speech falls back to the browser |
| Screen Recording | capture reports the permission, and the analysis says so |
| network | web tools report that they need connectivity; nothing else is affected |
| a tool crash | caught, logged, turned into a calm sentence; the turn continues |
