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
                    │                  understand → recipe│
                    │                  or operator: act → │
              wrong tool? ─────────►   look → prove → done │
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
understand (in context)  →  resolve references  →  operate:
  tool calls  →  act  →  look again  →  verify  →  …  →  finish, proven
  →  answer
```

A turn is bounded by `intelligence.max_steps` (default 6 actions); a
multi-step errand runs the same operator as a background task under
`automation.max_steps` / `max_wall_s` / `max_model_calls`. Every tool call
goes through the same registry, with the same risk gate, as every other path.
See [`intelligence.md`](intelligence.md).

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

## Surfaces (`surfaces/`, v3.0)

What the operator acts on, each listed for the model the same way — elements
with short handles, the operator reading the view again after every action:

* **Web** (`surfaces/web/`) — JARVIS's own Chrome over the DevTools protocol
  (genuine input, network-aware settling) or the everyday browser over
  AppleScript, chosen per task by the browser hub.
* **Native** (`surfaces/native/`) — Mac apps through the Accessibility API:
  `ax.py` turns a window into a bounded listing of `[axN]` controls, `input.py`
  posts genuine key and mouse events (Quartz), `marks.py` reads a screenshot's
  text with Apple's Vision framework and numbers what's clickable, and
  `surface.py` owns the handles and the actions. `backend.py` is the only
  module that imports PyObjC, and the only one the tests replace.

## Skills (`skills/`, v3.0)

Recipes for the errands people ask for most, stated the way a person would
describe each step — never as a handle, which means nothing on the next visit:

```
errand ─► library.direct(objective) ──fits, all parameters known──► runner
               │                                               │
               └─ fits, something missing ─► offered to        ├─ every step grounded in the
                  the operator as skill_* tools                 │  latest listing, one registry
                                                                │  call each (gate included)
                                          proven ─◄─────────────┤
                                                                └─ a step doesn't fit ─► operator
                                                                   carries on from that page
```

* `model.py` — the recipe format (steps: `go`, `open`, `app`, `click`,
  `fill`, `key`, `menu`, `type`, `wait`, `scroll_until`, `expect`, and — for
  built-in recipes only — `tool`), parameters with where they come from (the
  errand's subject, a domain the user named, the app), and `done_when`.
* `grounding.py` — finds "the *Add to Basket* button" or "the result that
  best matches *AA batteries*" in a page or window listing.
* `runner.py` — runs the steps and reports exactly how far it got.
* `library.py` — which recipe fits (`sites`/`apps`, `words`, `requires`,
  `excludes`), which can run directly, which to offer, tips from
  `knowledge/*.md`, and the learned recipes' bookkeeping. A request to buy,
  pay or order matches no skill.
* `learning.py` — turns a proven operator run into a recipe: each element
  stored as what it was, the errand's subject as a parameter, the proof as
  the success check. Anything that changed state beyond plain operating
  (sending, deleting) means nothing is learned.

A recipe that finishes costs no model calls. A foreground turn and a
background errand both try one before starting the operator and, when it
stops part-way, start the operator with the recipe's account, the current
page and everything it saw.

---

## Telemetry (`core/telemetry.py`)

Spans for: `router.quick`, `router.heuristic`, `router.model`, `model.stream`,
`model.ttft`, `tool.<name>`, `task.<kind>`, `voice.wake`, `voice.stt`,
`voice.tts`, `turn.total`, and per request `request.total`,
`request.first_action` and `request.spoken`. The developer panel shows p50/p95
per span and the routing path of recent requests — the point being to make an
expensive path for a cheap question immediately visible.

**Request timelines** (`core/latency.py`). `turn.total` stops when the turn
returns, which for an errand is the acknowledgement. A timeline follows the
whole request instead: it rides a context variable (like the turn id), so the
background task a turn starts — which copies the context — records onto the
same timeline without anything being passed around. Every model and tool span
lands on the current timeline; page waits inside a tool (`latency.waiting()`,
used by `observe.settle` and the JARVIS Chrome driver) are counted as waiting,
not acting. Speech is tagged when it's queued: only speech of the *result*
(not "on it", not a progress line) marks the request as spoken. The timeline
is published as `request.timing` when the turn returns, when a background task
delivers, and when the answer starts playing.

**Settling** (`tools/browser/observe.py`). Acting on or reading a page first
waits until it's loaded, finished fetching and still. A full settle is
remembered (when, and the page's DOM signature — JARVIS's own `data-jarvis-id`
tags don't count as changes); the next one returns at once if nothing has
acted on the page since (every page tool marks it; any tool outside the
browser toolkit marks every page), no request has gone out and the signature
is unchanged. JARVIS Chrome installs a counter of each document's pending
short timers at document start; a page with nothing queued, no animation
running and the network idle for 0.25 s needs 0.25 s of stillness, otherwise
the full rule applies (0.5 s still, and 0.75 s since the last request).

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
