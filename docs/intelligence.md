# Intelligence (V1.2, routing revised V1.3)

> V1.1 made JARVIS work on a real Mac. V1.2 is about making it *think*. V1.3
> replaces the guess about *whether* something is an action with a reliable
> one.

V1.0 and V1.1 had one shape:

```
sentence → capability label → one tool → answer
```

That shape is right for "what time is it" and structurally incapable of
"anything from my brother?", because the sentence alone does not carry its own
meaning. V1.2 puts an agent loop above the tool layer and leaves the fast path
exactly where it was. V1.3 adds one step at the top of that loop: a single,
reliable chat-vs-action decision, replacing a keyword scorer and a 1B-model
guess that a benchmark showed misclassifying real actions as chat.

```
input
  ↓
quick command engine ──────────────► a certainty: answer it, ~4 ms
  ↓ (declined, or the tool was the wrong one)
intent triage: chat or action?  ──chat──► converse (no tools, no planner)
  ↓ action
is triage's own objective already confident and complete?  ──yes──► use it directly
  ↓ no
intent + entity understanding
  ↓
is anything genuinely ambiguous?  ──yes──► ask
  ↓ no
plan  (multi-step work only)
  ↓
┌─► decide next action
│      ↓
│   execute (through the registry — permissions included)
│      ↓
│   observe: the result becomes context
│      ↓
│   verify: did that achieve what was asked?
│      ↓
│   ok? ──yes──► next step, or done
│      ↓ no
│   recover: retry / re-argue / re-route / ask / report
└──────┘
  ↓
answer (streamed)
```

## Intent triage (V1.3)

`triage.py` is the one semantic authority for chat vs. action — nothing below
it (`ToolCatalog`, the planner, the execution loop) runs until triage has said
"action". It always uses the `reasoning` slot (defers to `general` —
llama3.1:8b — never `fast`): `scripts/bench_triage.py` measured the 1B model
classifying every explicit action request as chat, with schema-validation
failures on top, so speed is deliberately traded for reliability here. The
fast gateway (`router/quick.py`, unchanged) is what keeps genuinely
deterministic requests — time, volume, arithmetic, explicit browser commands —
fast; it was already tight, anchored, explicit-imperative patterns rather than
keyword scoring, so it needed no narrowing.

"Action" means the user appears to be asking JARVIS to do something — not that
there is enough information to act. Positive evidence is required: `Triage`
carries `action_evidence`, short literal phrases from the user's own words,
and a validator downgrades `action` to `chat` outright if that list is empty.
"Send it." and "Open Safari." are both action-shaped; only the second is
executable without asking. When triage's own objective is already confident
and complete (see `triage.objective_sufficient`), the agent uses it directly,
skipping a second model call; otherwise it escalates to the full
`Understanding` pass below, which still does the clarification-résumé,
reference-resolution and inheritance work regardless of which one produced
the objective (see `Understanding.finalize`).

A turn that answers a clarification JARVIS asked last turn skips triage
entirely — it is structurally an answer ("Ada.", "the second one"), not a
fresh utterance to classify, and the existing clarification-résumé mechanism
handles it.

The router's old `_heuristic` (keyword scoring across capabilities) and
`_classify` (1B capability guess) stages still exist but are no longer
authoritative for this decision — see `router/router.py`. They remain
load-bearing for exactly one case: `intelligence.enabled=False` (V1.1 mode),
which has no agent and no triage downstream and so has no other way to reach
a specific capability.

---

## The modules

| File | Responsibility |
| --- | --- |
| `state.py` | `ConversationState` — the bounded working set, plus `attach()` |
| `triage.py` | `IntentTriage` — sentence → chat or action (V1.3) |
| `understanding.py` | sentence → `Objective` |
| `entities.py` | `ReferenceResolver` — "it", "him", "the second one" |
| `catalog.py` | `ToolCard`, shortlisting, call validation |
| `planner.py` | plans, for multi-step work only |
| `agent.py` | `IntelligenceAgent` — the loop |
| `verify.py` | `Verifier` — did it actually work? |
| `recovery.py` | `RecoveryManager` — what to do when it didn't |
| `schema.py` | validated structured decisions |
| `observability.py` | `Trace` — the stage stream |

Each is independently testable and independently replaceable. The orchestrator
owns one `ConversationState` and one agent; the agent owns nothing global.

---

## Conversational state

```python
state.objective          # "research specialised cells"
state.browser            # app, url, title, when
state.email              # messages, unread, the one being read
state.research           # query, sources, report
state.screen             # description, extracted elements, expires after 180 s
state.entities           # deque(maxlen=40) of things mentioned, most recent last
state.observations       # deque(maxlen=…) of what tools returned
state.turns              # a handful of turns, verbatim
```

**Filled by an observer, not by the agent.** `attach(state, registry)` registers
a callback that sees every tool call with its *structured* result:

```python
registry.observe(lambda tool, args, result, category:
                 state.note_observation(tool, args, result.ok, result.summary,
                                        result.data, category))
```

This is why "check my emails" (answered by the fast path and the V1.1 email
capability) can be followed by "anything from my brother?" (answered by the
agent) and still mean something. Capability responses are absorbed the same way
by the orchestrator, because research reads pages without passing through the
registry.

Extraction (`ConversationState._absorb`) dispatches on **category and payload
shape**, never on tool names, so a new tool in an existing category contributes
context with no change to this file.

---

## Understanding

The output is an `Objective`, not a label:

```jsonc
{
  "goal": "find mail from the user's brother",
  "kind": "read email",
  "targets": ["brother"],
  "constraints": [],
  "references": [{"text": "my brother", "kind": "person"}],
  "needs_tools": true,
  "complexity": "simple",              // trivial | simple | multi_step
  "confidence": "confident",           // confident | probable | ambiguous | impossible
  "refines_previous": true,
  "is_correction": false,
  "missing": []
}
```

Three post-processing passes run afterwards, in order:

1. **Clarification** — if JARVIS asked a question last turn, this turn is the
   answer, so the waiting objective is resumed rather than replaced.
2. **Reference resolution** — each reference is bound to something concrete, or
   marked ambiguous, which downgrades the objective's confidence.
3. **Inheritance** — a refinement keeps the previous goal and adds its own text
   as a constraint. "Which ones are used in medicine?" is still *research
   specialised cells*, narrowed.

A deterministic fallback covers the case where no model can be reached. It is
deliberately modest — pretending to understand offline would be worse than
admitting the limit — and the agent then answers conversationally or says the
service is unavailable, never inventing a tool call.

---

## Reference resolution

Six stages, most specific first:

| | Stage | Example |
| --- | --- | --- |
| 1 | relationship words | "my brother" → *candidates*, never a guess |
| 2 | ordinals | "the second one", "the last one" |
| 3 | discriminating words | "the one **from Ada**" |
| 4 | live context objects | "this page", "the browser" |
| 5 | pronouns → most recent of that kind | "close it" |
| 6 | loose label match | "the roof email" |

Cutting across stages 1 and 6 is **salience**: being in context is not the same
as being talked about. Three people are in the inbox, but one of them was named
out loud two sentences ago, so "him" means Tom rather than whoever was added
last. `ReferenceResolver._mentioned` is a whole-word search of the last few
turns — linguistic recency, a general signal, not a rule about mail.

The mechanisms are general — kind inference from the noun phrase, ordinals,
deixis, recency, salience, label matching — so a reference no test mentions
still resolves. When several candidates remain genuinely plausible the resolver
says `ambiguous`.

### Ambiguity is only worth interrupting for when it matters

Misreading "anything from my brother?" costs a wrong answer, and the user
corrects it in four words. Misreading "email him" sends a message to the wrong
person, and nobody can take that back. So:

* **low-risk ambiguity** is inferred from context — the candidates are in the
  decision prompt, and the model answers from them;
* **consequential ambiguity** is a question:

  > "Which of them, sir — Tom, Ada or the invoice?"

What counts as consequential is decided by the same shortlist that picks the
tool: if the best-matching tool for the objective changes state or needs
confirmation, the ambiguity is consequential. There is a second check
immediately before any state-changing call, because an objective that looked
like a read can still end up proposing a send. No list of dangerous verbs is
involved.

---

## Tool selection

The reasoning model never sees all 50 tools. `ToolCatalog.shortlist()` scores every
tool against the objective's tokens (lightly stemmed, whole tokens only — "out"
must not match inside "output volume") plus its examples, boosts categories that
are live in context, and slightly prefers reads over writes because a read is a
safer first move. The top dozen are rendered as one line each:

```
- check_email([limit:integer]): Check for new mail and summarise what has arrived — returns messages with sender, subject and preview
- draft_email(to:array, subject:string, body:string): Prepare an email without sending it — changes state; asks the user first
```

Everything in that line is a decision input: what it needs, what comes back,
whether it changes anything, whether it will ask first.

A proposed call is validated against the tool's schema *before* execution. A
rejection is not an error — it is fed back as an observation and corrected on
the next step.

---

## Verification

`ok=True` means the call completed, not that the world changed as intended.

| Category | Evidence used |
| --- | --- |
| Navigation | the returned page: a not-found body, or a destination that doesn't resemble what was asked for |
| Files | the filesystem — present after a write, absent after a delete |
| Application launch | whether the process is actually running |
| Screen | whether the vision model hedged ("I can't see any clear text") |
| Plain reads | whether any data came back |
| Everything else | `skipped=True` — explicitly not verified, never assumed good |

Verification runs on evidence the tool already returned wherever possible, so
the common case costs nothing extra.

---

## Recovery

One decision per failure, within a budget (default 2 attempts per step):

| Strategy | When |
| --- | --- |
| `retry` | the failure looks transient **and** the tool is safe to repeat |
| `modify_arguments` | the arguments were wrong or incomplete |
| `alternative_tool` | a different tool would get there |
| `ask_user` | only the user can resolve it |
| `report` | nothing sensible remains |

Some decisions need no model at all:

* a **declined confirmation** is an answer, not an obstacle — always `report`;
* a **missing or invalid argument** is the model's to fix — `modify_arguments`;
* a **macOS-only tool on another host** will never start working — `report`.

`retry` on a state-changing tool is refused outright. Blind retry loops are
worse than an honest failure.

---

## Model roles

```
fast        1B-class    routing, classification, greetings, short rewrites
general     8B-class    conversation and synthesis
reasoning   —           understanding, planning, decisions, verification, repair
vision      7B-class    screen understanding
specialist  —           optional domain model: code, maths, a local fine-tune
```

`reasoning` and `specialist` are empty by default and **defer** to another slot
(`SLOT_DEFERS_TO` in `models/registry.py`): `specialist` → `reasoning` →
`general`. That is what lets the roles exist in the code from day one without
requiring anyone to download five models, and makes upgrading one a single line
of configuration rather than a refactor.

Do not expect a bigger model to fix orchestration. The loop — verify, recover,
ask — is what stops a wrong first move becoming a wrong answer, and it works
independently of which model is in the slot.

---

## Structured output

Every judgement comes back as validated Pydantic, never regex-scraped prose:

```jsonc
{"action": "tool_call", "tool": "search_web", "arguments": {"query": "…"}, "reason": "…"}
{"action": "clarify",  "question": "Which person do you mean?"}
{"action": "respond",  "content": "…"}
{"action": "complete", "reason": "…"}
```

`schema.load()` parses, validates, and makes one repair pass (dropping unknown
keys) before giving up — small local models miss by a little far more often than
they miss by a lot. A decision that still doesn't validate returns `None`, and
the caller falls back rather than a parser silently misreading an instruction.

---

## Observability

`Trace` publishes one `intelligence.trace` event per stage and logs the same
line (at INFO in developer mode, DEBUG otherwise):

```
INTENT    read email: find mail from the user's brother [confident]
DECISION  tool_call check_email
RESULT    check_email ok: You have 4 new messages.
VERIFY    verified — result contained data
COMPLETE  2 step(s), 1 tool call(s), 3 model call(s), 1840.2 ms
```

**Chain-of-thought is never published.** Every entry is structured state —
decisions, arguments, outcomes, statuses — plus the one-line justification the
model attaches to a decision. Sensitive argument values (bodies, tokens,
passwords) are redacted before the event leaves the process.

The frontend renders this twice: the **Working on it** panel as a live plan, and
the developer panel as a stage stream. Both are projections of what actually
happened — a step turns green only once verification said so.

---

## Security

Unchanged from V1.1, deliberately. The agent has no privileged path:

* every tool call goes through `ToolRegistry.call`, which applies the risk level
  and the permission broker;
* HIGH-risk actions (sending email, deleting data, destructive shell) always
  require explicit confirmation, whatever the model decided;
* a declined confirmation is reported, never worked around;
* `intelligence.enabled: false` removes the agent entirely and restores V1.1.

Tested in `tests/test_intelligence.py`:
`test_the_agent_cannot_bypass_the_high_risk_confirmation` and
`test_a_declined_confirmation_is_reported_not_worked_around`.

---

## Testing it

`tests/test_intelligence.py` scripts the reasoning model by the **purpose** of
each prompt rather than by call order, because the number of model calls depends
on what the tools return:

```python
brain.understand(goal="check email", kind="read email")
brain.decide(action="tool_call", tool="check_email", arguments={"limit": 8},
             reason="the user asked for their mail")
await app.ask("check my emails")
```

Any stage left unscripted gets a default that *ends* the turn, so an
under-scripted test fails with a short answer rather than looping.

`brain.prompts("decide")` returns what the model was actually shown, which is
how the follow-up tests assert that context reached it — the point is not that
the scripted model said the right thing, but that it was given what it needed
to.
