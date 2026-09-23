# JARVIS V1.3

A local-first AI operating layer for macOS. You speak; it works out what you
mean *in the context of what you were just doing*, decides the cheapest
competent way to get there, and either answers instantly or goes away and does
the work while you carry on.

```
You:     "Jarvis."
JARVIS:  "Yes, sir?"
You:     "How much storage do I have?"
JARVIS:  "You have 412 gigabytes available."          ← 40 ms, no model involved

You:     "Check my emails."
JARVIS:  "You have four new messages. Two look important."
You:     "Anything from my brother?"                  ← no nouns, no app named
JARVIS:  "Tom wrote about Sunday lunch."              ← the inbox is still context

You:     "Research the best current MacBook deals and compare them."
JARVIS:  "I'll look into it, sir."                    ← immediate
         RESEARCH  Searching · Opening 5 results · Comparing · Preparing summary
JARVIS:  "I've finished the comparison."              ← spoken summary, detail on screen
You:     "Which of those is quietest?"                ← still the same five sources
```

Everything that can run on your machine does: wake word, speech recognition,
reasoning, memory and speech output. **No paid API is required.**

---

## What's new in V1.3

V1.2 gave JARVIS an agent loop; it still decided *whether* something was a
task the same way V1.1 did — a keyword scorer and a small classifier model,
prone to scoring a domain word mentioned in passing ("I hate dealing with
email") as evidence of a request. V1.3 replaces that guess with one reliable
decision: **intent triage** (`intelligence/triage.py`), made by the capable
model, not the fast one, and gated on positive evidence — a short literal
phrase from what the user actually said, not a topic that happened to be
live. Chat gets a direct path with no tool machinery involved; a request
that's already complete and unambiguous skips a redundant understanding
pass; everything else escalates normally. Full detail in
[`docs/intelligence.md`](docs/intelligence.md).

Three fixes came out of taking that guarantee seriously enough to verify it
end to end:

- **Stale context.** A finished task's results used to sit in every
  subsequent prompt indefinitely — a greeting minutes after a research task
  could still arrive describing that task as if it were live. Chat and
  triage now see a genuinely lean conversational view; the fuller state
  remains exactly where reference resolution and planning need it.
- **Contextual references quick-routed.** "Open the second one." matched
  the same deterministic pattern as "Open Safari." and could attempt a
  literal, spurious application launch before anything got the chance to
  ask what "the second one" meant. A quick match is now also checked for
  whether its argument is safe to treat as complete and deterministic, not
  merely shaped like one.
- **Browser destinations vs. application names.** "Open BBC.co.uk." was
  read as an application-launch request rather than a browser navigation,
  which meant the browser's own destination verification never ran. The
  fast gateway now tells the two apart before committing to either.

## What's new in V1.2

V1.1 made JARVIS work on a real Mac. **V1.2 makes it think.**

V1.0 and V1.1 followed one shape: classify the sentence into a capability, run
one action, answer. That is fine for "what time is it" and hopeless for
"anything from my brother?", because the sentence on its own doesn't say what
it means. V1.2 puts a proper agent loop above the tool layer:

```
input → understand in context → resolve what "it" and "him" refer to
      → decide the next action → act → observe → verify → repair or ask
      → answer
```

**The fast path is untouched.** A deterministic pattern match is still a
certainty, not a guess: "what time is it", "open Safari", "mute", "remember
that…" still cost a regular expression and a system call — around 4 ms, no
model. What changed is everything the pattern engine *declines*, which is most
of what anyone actually says.

**What that buys you, concretely**

| Before (V1.1) | Now (V1.2) |
| --- | --- |
| "Anything from my brother?" → new email search from scratch | answered from the inbox already on screen |
| "Go to the BBC" after opening Safari → picks a browser afresh | uses the browser that's open |
| "Which ones are used in medicine?" → a fresh web search | the five sources already gathered |
| "Click the search bar" → nothing to click with | finds the control by its accessibility label and clicks it |
| "Open the BBC" → *"I can't find an application called the BBC."* | recognises a website and opens it |
| A tool reports success → believed | checked: a 404 page is not a successful navigation |
| A failure → reported and dropped | diagnosed, then retried, re-argued, re-routed or asked about |
| "Email him" with three senders → guesses, or fails | *"Which of them, sir — Tom, Ada or the invoice?"* |

**The pieces**

- **Conversational state** (`intelligence/state.py`) — a small, bounded working
  set: the current objective, the open page, the inbox, the sources, what's on
  screen, the entities mentioned. Fed by *every* tool call through a registry
  observer, so context survives whichever path answered the previous turn.
- **Understanding** (`understanding.py`) — produces an *objective*, not a label:
  goal, targets, constraints, references, complexity and an honest confidence.
- **Reference resolution** (`entities.py`) — six precedence stages (relationship
  words, ordinals, discriminating words, live context, pronouns, loose match)
  plus salience: what the conversation was *just talking about* wins. Ambiguity
  that matters becomes a question; ambiguity that doesn't is inferred from
  context, and what counts as "matters" is whether the best-matching tool
  changes anything.
- **Tool selection** (`catalog.py`) — the model sees a scored shortlist of
  around a dozen tools described in decision terms (what it needs, what comes
  back, whether it changes anything, whether it will ask first) instead of 51.
- **Planning** (`planner.py`) — only for genuinely multi-step work, and the plan
  is advisory: every actual decision is made against what the last tool returned.
- **Verification** (`verify.py`) — "opened the BBC" is not "the BBC is open".
  Navigation checks the page, file operations check the filesystem, application
  launches check the process list, vision output is rejected when it hedges.
- **Recovery** (`recovery.py`) — retry, different arguments, different tool, ask,
  or report, with a budget. A declined confirmation is an answer, not an obstacle
  to route around.
- **Structured output** (`schema.py`) — every decision is validated Pydantic, with
  a repair pass for the near-misses small models make. Nothing is regex-scraped.
- **Interaction tools** (`tools/interaction/`) — `click_element`, `type_text`,
  `press_key`, `get_frontmost_app`, so seeing the screen and acting on it are the
  same conversation. Semantic, by accessibility label — never guessed coordinates.
- **Model roles** — `fast`, `general`, `reasoning`, `vision`, `specialist`. The
  last two are optional: an unconfigured slot *defers* (`specialist` → `reasoning`
  → `general`), so the roles exist in the code from day one and upgrading one is
  a single line of configuration rather than a refactor.

**Security is unchanged.** Every tool call the agent makes goes through the same
registry, the same risk levels and the same confirmation broker as V1.1. There
is no path from a model decision to a HIGH-risk action without you agreeing to
it — see [Security model](#security-model), and
`tests/test_intelligence.py::test_the_agent_cannot_bypass_the_high_risk_confirmation`.

**Turning it off.** `intelligence.enabled: false` restores V1.1 behaviour
exactly — capability classification, one action, one answer. It exists so the
new layer can be *measured* against the old one rather than merely asserted to
be better, and so there is somewhere to stand if it misbehaves.

More: [Intelligence](#intelligence) below, the full design in
[`docs/intelligence.md`](docs/intelligence.md), and
[Known limitations](#known-limitations).

---

## What's new in V1.1

A bug-fix release. V1.1 fixes the wake-word loop, which crashed on the first
audio frame on a real Mac.

**The bug.** The microphone layer produces raw 16-bit PCM as `bytes`, and those
bytes were handed straight to `openwakeword.Model.predict()`, which requires a
numpy array:

```
ValueError: The input audio data (x) must by a Numpy array,
            instead received an object of type <class 'bytes'>.
```

The microphone opened correctly (`microphone open at 16000 Hz`) and then the
listening loop died on the first frame, so the wake word never worked.

**The fix.** PCM conversion now lives at the audio boundary
(`backend/jarvis/voice/audio.py`) instead of being each detector's business:

| | |
| --- | --- |
| `to_int16_frame()` | raw PCM → 1-D, C-contiguous, writable, native-endian **int16** array |
| `to_float32_frame()` | raw PCM → float32 in [-1, 1], which is what Whisper wants |

`VoiceManager` converts once per frame and hands the array to the detector; both
detectors also convert defensively, so neither representation can crash the loop.

**Why `int16` specifically.** Making it "a numpy array" is not enough. openWakeWord
0.6 buffers each frame through a Python list and then `np.array(...).astype(np.int16)`.
Float samples in [-1, 1] are therefore **truncated to zero rather than rejected**:
no exception, no detection, a wake word that silently never fires. Converting to
int16 is what actually makes detection work — verified by inspecting the
library's internal buffer (see below).

**Also in V1.1**

- The wake-word model is loaded by `prepare()` *before* the listening loop
  starts, so a missing model or an unusable runtime reports itself at start-up
  instead of raising inside the hot loop.
- Model download uses an explicit `from openwakeword.utils import download_models`
  and verifies the resolved file exists, with an actionable message if it doesn't.
- A per-frame detector fault no longer kills listening: it is logged in full and
  surfaced in the interface, and after five consecutive failures the wake loop
  stops with a clear message rather than spinning on the same error. Errors are
  reported, never silently swallowed.
- `scripts/start.sh` exports `PYTHONPATH="$PWD/backend"` so the backend is
  importable when an editable install doesn't register its path.
- A partially-captured frame (odd byte count) is trimmed rather than raising.
- Stereo input is down-mixed to mono correctly — the first version of this fix
  scaled an already-int16-ranged average as if it were float, clipping every
  sample to the rails. Caught by a test, fixed before release.

**Verification.** The fix was verified against the real `openwakeword` 0.6.0
package and the real `hey_jarvis_v0.1.onnx` model on Linux: the V1.0 crash
reproduces exactly, the fixed path runs 12 s of audio through the real model
with the loop alive at ~3% of real time, and the audio inside openWakeWord's
buffer is bit-identical to what the microphone produced. **A true positive —
a person actually saying "Jarvis" — has not been verified**; that needs a
microphone and a Mac. See [Known limitations](#known-limitations).

---

## Contents

- [What's new in V1.2](#whats-new-in-v12)
- [What's new in V1.1](#whats-new-in-v11)
- [What it does](#what-it-does)
- [The idea: latency proportional to complexity](#the-idea-latency-proportional-to-complexity)
- [Intelligence](#intelligence)
- [Requirements](#requirements)
- [Installation](#installation)
- [Local models (Ollama)](#local-models-ollama)
- [macOS permissions](#macos-permissions)
- [Running it](#running-it)
- [Voice setup](#voice-setup)
- [Configuration](#configuration)
- [Security model](#security-model)
- [The workspace](#the-workspace)
- [Architecture](#architecture)
- [Development](#development)
- [Testing](#testing)
- [Adding a tool](#adding-a-tool)
- [Adding a model provider](#adding-a-model-provider)
- [Troubleshooting](#troubleshooting)
- [Known limitations](#known-limitations)

---

## What it does

| Area | Examples |
| --- | --- |
| **Conversation** | "Hello." · "What's the difference between APFS and HFS+?" |
| **System facts** | "How much storage is left?" · "What chip does this Mac have?" · "Battery?" |
| **Applications** | "Open Safari." · "Launch VS Code." · "Close Spotify." · "What's running?" |
| **Clipboard** | "What did I copy?" · "Copy that to my clipboard." |
| **Screen** | "What's on my screen?" · "What does this error mean?" |
| **Files & notes** | "Make a note that the deploy is Friday." · "What's in my workspace?" |
| **Email** | "Check my emails." · "Draft a reply to Ada." (sending always asks first) |
| **Calendar** | "What's on today?" · "What does my week look like?" |
| **Web research** | "Research the best local AI models for this Mac and compare them." |
| **Browser** | "Go to apple.com." · "Summarise this page." |
| **Diagnostics** | "Why is my Mac slow?" · "Is anything wrong?" |
| **Memory** | "Remember that I prefer short answers." · "What do you remember about me?" |
| **Screen actions** | "Click the search bar." · "Type that in." · "Press enter." |
| **Control** | "Stop." · "Mute." · "Turn the volume down." |
| **Follow-ups** | "Anything from my brother?" · "Which of those?" · "That's not right." |

50 tools across 9 categories, one agent loop, one orchestrator.

---

## The idea: latency proportional to complexity

Most assistants send *everything* to one large model, so "hello" costs the same
as "analyse this codebase". JARVIS routes each request down the cheapest path
that can competently answer it:

```
 request
    │
    ├─ 1. quick commands  ── deterministic patterns ........ ~0.05 ms, no model
    │     "open Safari", "what time is it", "mute", "stop", "remember that…"
    │     a pattern that maps a sentence onto one action — a certainty
    │
    └─ everything else ──── the intelligence agent
          │
          ├─ understand in context ....................... one reasoning call
          ├─ plan (multi-step work only) ................. one more, or none
          ├─ decide → act → verify → repair .............. per step
          └─ answer ...................................... streamed as it arrives
```

Three consequences you can feel:

* **Deterministic answers stay deterministic.** "How much storage do I have?"
  is `shutil.disk_usage()` plus a sentence template. The model is never asked.
* **Thinking is proportional too.** A simple request costs one understanding
  call and one decision; only genuinely multi-step work pays for a plan. The
  answer streams, so it starts arriving in a few hundred milliseconds.
* **Slow work never blocks you.** Anything long-running is acknowledged in under
  a second, becomes a background task with visible progress, and reports back
  when it's done — while you keep talking.

**A quick match that turns out to be wrong isn't a dead end.** "Open the BBC"
matches the open-an-application pattern; when no such application exists the
tool says *this wasn't mine to do* (`ToolResult.wrong_tool`) and the turn goes
to the agent, which works out that the BBC is a website. A launch that was
*refused* says something different, and is reported plainly instead.

Developer mode (the `DEV` button) shows the path, the model and the latency of
every request, so a slow path is obvious rather than mysterious.

---

## Intelligence

The agent loop lives in `backend/jarvis/intelligence/`. Each piece is a module
you can read on its own, and each one is replaceable.

```
backend/jarvis/intelligence/
├── state.py            conversational state + the registry observer that fills it
├── understanding.py    sentence → objective (goal, targets, references, confidence)
├── entities.py         "it", "him", "the second one", "the one from Ada"
├── catalog.py          tool cards and the shortlist the model actually sees
├── planner.py          plans, for multi-step work only
├── agent.py            the loop: decide → act → observe → verify → repair
├── verify.py           did that actually achieve what was asked?
├── recovery.py         retry / re-argue / re-route / ask / report
├── schema.py           validated structured decisions
└── observability.py    the trace the UI and the log render
```

### Context, and why follow-ups work

`ConversationState` is a **bounded** working set — a handful of turns, at most
40 entities, the live browser page, the current inbox, the current sources, and
a screen description that expires after three minutes. It is not a memory
project: V1.2 deliberately builds only the short-term context that understanding
*now* requires. Long-term memory remains the separate SQLite store it was in
V1.0.

It is filled by an observer registered on the tool registry, so **every** tool
call becomes context regardless of what made it — the fast path, a V1.1
capability, or the agent itself. That is why "check my emails" (a quick match)
can be followed by "anything from my brother?" (the agent) and still mean
something. Capability results are absorbed the same way, so research done by the
V1.1 research capability is still there for the follow-up.

Extraction dispatches on tool *category* and payload shape, not on tool names,
so a new tool in an existing category contributes context with no change here.

### Verification, and why it matters

A tool returning `ok=True` means the call completed, not that the world changed
as intended. V1.2 checks, proportionally to cost:

| Action | Check |
| --- | --- |
| Navigation | the page that came back — a 404 body, or a destination that doesn't resemble what was asked for |
| File write / delete | the filesystem |
| Application launch | whether the process is actually running |
| Screen analysis | whether the vision model hedged ("I can't see any clear text") |
| Plain reads | whether data came back at all |
| Anything else | marked *skipped* — never assumed good |

When a check fails, recovery decides once, within a budget: retry (only if the
tool is safe to repeat), modify the arguments, try a different tool, ask you, or
report honestly. Some decisions need no model at all — a declined confirmation
is an answer, a missing argument is the model's to fix, and a macOS-only tool on
another host will never start working.

### Model roles

```jsonc
"models": {
  "fast":       { "model": "llama3.2:1b" },   // classification, greetings, rewrites
  "general":    { "model": "llama3.1:8b" },   // conversation and synthesis
  "reasoning":  { "model": "" },              // ← empty: uses the general model
  "vision":     { "model": "llava:7b" },      // screen understanding
  "specialist": { "model": "" }               // ← empty: uses the reasoning model
}
```

An empty slot **defers** to another slot (`specialist` → `reasoning` →
`general`), so a stock install is intelligent out of the box with three models,
and giving the agent a better brain is one line:

```bash
ollama pull qwen2.5:14b
# Settings → Models → reasoning model: qwen2.5:14b
```

Nothing else changes. The architecture is what makes the loop work; a bigger
model makes its individual judgements better. Both matter, and neither
substitutes for the other.

### Watching it think

Developer mode (the `DEV` button) shows the trace stage by stage, and the log
prints the same thing:

```
INTENT    read email: find mail from the user's brother [confident]
CONTEXT   inbox: 4 messages, 2 unread
DECISION  tool_call check_email
RESULT    check_email ok: You have 4 new messages.
VERIFY    verified — result contained data
COMPLETE  2 step(s), 1 tool call(s), 3 model call(s), 1840.2 ms
```

The right-hand **Working on it** panel shows the same thing as a live plan.
Both are projections of what actually happened: a step only turns green once
verification said so, and no line is drawn for work that isn't running.

**Chain-of-thought is never exposed.** The trace carries structured state —
decisions, arguments, outcomes, statuses — and one-line justifications the model
attaches to a decision. Sensitive arguments (bodies, tokens, passwords) are
redacted before they reach the bus.

### What it costs

Measured per turn and visible in developer mode: intent latency, model latency,
tool latency, total task latency, model calls, tool calls and repairs. The
`intelligence.turn` telemetry span carries the same numbers.

| Path | Model calls | Typical |
| --- | --- | --- |
| Quick command | 0 | 3–5 ms |
| Simple agent turn | 2–3 | 1–3 s with an 8B local model |
| Multi-step | 4–8 | seconds, acknowledged immediately, run in the background |

Full design notes: [`docs/intelligence.md`](docs/intelligence.md).

---

## Requirements

| | Minimum | Recommended |
| --- | --- | --- |
| **macOS** | 13 Ventura | 14 Sonoma or later |
| **Hardware** | Intel or Apple Silicon | Apple Silicon (M1 or later) |
| **Memory** | 8 GB | 16 GB+ |
| **Python** | 3.10 | 3.11 or 3.12 |
| **Node** | 18 | 20+ |
| **Disk** | ~3 GB for a small model | ~12 GB for fast + general + vision |

**Apple Silicon** is strongly preferred: Ollama uses the GPU through Metal, and
`faster-whisper` runs comfortably on the efficiency cores. On Intel Macs, use
the smaller models (`./scripts/pull-models.sh --small`) and expect several
seconds for general-model answers.

JARVIS also **runs on Linux for development** — the macOS-specific tools report
that they need macOS instead of failing, and everything else works.

---

## Installation

```bash
git clone <this repository> jarvis
cd jarvis
./scripts/setup.sh
```

The script creates `.venv`, installs the package and the voice extras, builds
the interface, and tells you what is missing. Then:

```bash
./.venv/bin/jarvis doctor     # check models, voice, permissions, workspace
./scripts/start.sh            # run it
```

<details>
<summary>Manual installation</summary>

```bash
python3 -m venv .venv
./.venv/bin/pip install -e ".[voice,dev]"
cd frontend && npm install && npm run build && cd ..
./.venv/bin/jarvis serve
```
</details>

### What `setup.sh` installs

| Package | Why | Optional? |
| --- | --- | --- |
| `fastapi`, `uvicorn` | the server and WebSocket event stream | required |
| `httpx` | model and web requests | required |
| `pydantic` | configuration schema | required |
| `beautifulsoup4` | web page extraction | required |
| `sounddevice`, `numpy` | microphone capture | voice only |
| `faster-whisper` | local speech recognition | voice only |
| `openwakeword` | offline wake-word detection | voice only |
| `pillow` | screenshot downscaling (faster vision) | optional |

Without the voice extras JARVIS still works: you type, or use the microphone
button, and the browser speaks the replies.

---

## Local models (Ollama)

JARVIS defaults to [Ollama](https://ollama.com) — free, local, no account.

```bash
brew install ollama          # or download from ollama.com
ollama serve                 # leave running (the app installs a service too)

./scripts/pull-models.sh             # llama3.2:1b + llama3.1:8b  (~6 GB)
./scripts/pull-models.sh --vision    # adds llava:7b             (~4.5 GB)
./scripts/pull-models.sh --small     # lighter pair for 8 GB Macs
```

Five slots, each configurable. Two of them are empty by default and *defer* to
another slot, so a stock install needs three models and no extra downloads:

| Slot | Default | Used for |
| --- | --- | --- |
| **fast** | `llama3.2:1b` | routing, classification, short rewrites, spoken summaries |
| **general** | `llama3.1:8b` | conversation and synthesis |
| **reasoning** | *(empty → general)* | understanding, planning, tool choice, verification, repair |
| **vision** | `llava:7b` | screen understanding |
| **specialist** | *(empty → reasoning)* | an optional domain model: code, maths, a local fine-tune |

Giving the agent a stronger brain is one line — `ollama pull qwen2.5:14b`, then
set the **reasoning** slot — and nothing else changes. See
[Model roles](#model-roles).

If a configured model isn't installed, JARVIS substitutes a sensible one from
what *is* installed (smallest for fast, largest for general and reasoning, any
vision-capable model for vision) and says so in `jarvis doctor`. With no model
at all, every deterministic capability still works and the agent says plainly
that it can't reason rather than guessing.

**Other providers.** Any OpenAI-compatible server (LM Studio, llama.cpp,
vLLM, OpenRouter, OpenAI) and Anthropic are supported — set the keys in `.env`
and enable the provider in Settings. Nothing in the application is coupled to a
provider; see [Adding a model provider](#adding-a-model-provider).

---

## macOS permissions

JARVIS asks for permissions **one capability at a time**, on first run, with the
reason stated. Nothing is requested for a capability you have switched off.

| Permission | Needed for | Where |
| --- | --- | --- |
| **Microphone** | wake word and speech | Privacy & Security → Microphone |
| **Screen Recording** | "What's on my screen?" | Privacy & Security → Screen Recording |
| **Automation** | Safari, Mail, Calendar control | Privacy & Security → Automation |
| **Accessibility** | window titles, focusing apps, **clicking and typing** (V1.2) | Privacy & Security → Accessibility |
| **Mail / Calendars** | reading mail and events | Privacy & Security → Mail, Calendars |

The first AppleScript call to Mail or Calendar triggers the system prompt —
accept it once. `jarvis doctor` reports what is granted, and the onboarding
screen can open the right settings pane for you.

> macOS attributes automation permissions to the *terminal or app that launched
> JARVIS*. If you start it from Terminal, grant Terminal the permission.

---

## Running it

```bash
./scripts/start.sh                 # interface at http://127.0.0.1:8765
./scripts/start.sh --no-voice      # text only
./scripts/start.sh --dev           # with the developer panel on
```

Or from the CLI:

```bash
jarvis                    # same as `jarvis serve`
jarvis ask "how much storage do I have?"
jarvis ask "research the best local AI models" --json
jarvis doctor             # installation check
jarvis models             # what each slot resolves to
jarvis config             # the effective configuration
```

**In the interface**

| Action | How |
| --- | --- |
| Talk | say "Jarvis", then your request |
| Push to talk | the microphone button (works without local voice extras) |
| Type | the composer, or press `/` |
| Stop everything | say "stop", press `Escape`, or use the Cancel button |
| See what it's doing | the Activity panel (right) |
| See why it chose a path | the `DEV` toggle (top right) |

---

## Voice setup

**Wake word.** `openwakeword` provides a pretrained "hey jarvis" model — offline
and inexpensive (measured at roughly 2 ms per 80 ms frame, about 3% of one core).
Both "Jarvis" and "Hey Jarvis" use that model.

On first use the model files are downloaded from the openWakeWord GitHub
releases into the package's `resources/models` directory (~5 MB, including the
shared melspectrogram and embedding models). That download happens once, during
`prepare()`, before listening starts; the ONNX runtime is used, so
`onnxruntime` must be installed — it comes with `openwakeword`.

If the model can't be loaded, JARVIS says so at start-up and keeps listening
*without* the wake word rather than failing: the microphone button still works.

Choose "Whisper keyword spotting" in Settings for a different wake phrase — it
transcribes short bursts and matches the word, which costs more CPU but works
with any phrase.

**Audio format.** The microphone produces 16 kHz mono 16-bit PCM in 80 ms frames
(1280 samples — openWakeWord's native chunk size). Conversion to the array the
detector needs happens in `backend/jarvis/voice/audio.py`; see
[What's new in V1.1](#whats-new-in-v11) for why the representation matters.

**Speech recognition.** `faster-whisper` with `base.en` by default. `tiny.en` is
quicker and less accurate; `small.en` is better and slower. The model downloads
on first use.

**Speech output.** macOS `say`, with the British voice *Daniel* by default. Any
installed system voice works — Settings lists them. Off macOS, replies are
spoken by the browser.

**Kokoro (optional, higher-fidelity local voice).** Set `voice.tts_engine` to
`"kokoro"` for a neural voice via [`kokoro-onnx`](https://github.com/thewh1teagle/kokoro-onnx)
— CPU-friendly, no PyTorch. This is opt-in, not the default: unlike `say`,
it needs its model files downloaded manually first (there's no silent
multi-hundred-MB download on first use):

```
pip install -e ".[voice,kokoro]"
```

then download `kokoro-v1.0.onnx` and `voices-v1.0.bin` from the project's
releases and point `voice.kokoro_model_path`/`voice.kokoro_voices_path` at
them. `voice.tts_voice` selects the voice by Kokoro's short code — the
British-male voice is `bm_lewis`; check the release's own voice list, since
codes can change between releases. Playback goes through `sounddevice`
(already required for the microphone), so no separate audio library is
needed.

**Conversation window.** After answering, JARVIS keeps listening for about 12
seconds so a follow-up needs no wake word.

**Interruption.** Speaking while JARVIS is talking stops it immediately, and
"stop" cancels the current background task as well.

**If local voice can't install** (no `sounddevice`, no Whisper), the microphone
button still works: the browser captures the audio, the backend transcribes it
locally if it can, and nothing leaves your machine either way.

---

## Configuration

Everything lives in **`~/JARVIS/config/config.json`**, editable from the
Settings sheet or by hand. Environment variables (from the shell or a `.env`
file) override the file — see [`.env.example`](.env.example).

```jsonc
{
  "workspace": "~/JARVIS",
  "models": {
    "fast":       { "provider": "ollama", "model": "llama3.2:1b", "timeout_s": 20 },
    "general":    { "provider": "ollama", "model": "llama3.1:8b" },
    "reasoning":  { "provider": "ollama", "model": "" },   // empty → uses general
    "vision":     { "provider": "ollama", "model": "llava:7b" },
    "specialist": { "provider": "ollama", "model": "" }    // empty → uses reasoning
  },
  "intelligence": {
    "enabled": true,          // false restores V1.1: one route, one action
    "max_steps": 6,           // ceiling on tool calls in a single turn
    "recovery_budget": 2,     // repair attempts before reporting honestly
    "reasoning_slot": "reasoning",
    "trace": true             // publish the stage trace to the interface
  },
  "voice": {
    "wake_word": "jarvis",
    "wake_engine": "openwakeword",
    "stt_model": "base.en",
    "tts_voice": "Daniel",
    "barge_in": true
  },
  "security": {
    "always_confirm": ["high"],
    "readable_roots": ["~/Documents", "~/Downloads", "~/Desktop"],
    "allow_shell": true
  },
  "capabilities": { "email": true, "calendar": true, "research": true, "screen": true },
  "personality": { "address_user_as": "sir", "honorific_frequency": 0.35 }
}
```

API keys are **never written to the config file** — they are read from the
environment only.

---

## Security model

JARVIS has real access to a real Mac, so every tool declares a risk level and
the model is never the thing that decides whether to ask you.

| Risk | Examples | Default behaviour |
| --- | --- | --- |
| **LOW** | time, battery, storage, open an app, open a URL, read the clipboard, capture the screen | runs |
| **MEDIUM** | write outside the workspace, move files, quit an app, create a calendar event, draft an email | asks |
| **HIGH** | **send email**, delete files, unlisted shell commands, anything destructive | always asks |

* **Sending email always requires confirmation.** The model can only ever create
  a *draft*; sending is a separate HIGH-risk tool. Saying "send it" opens the
  confirmation dialog — it does not send.
* **The filesystem is a boundary, not a suggestion.** `~/JARVIS` is free;
  configured folders (`~/Documents`…) are readable and ask before writes;
  anywhere else in `$HOME` asks; `/System`, `/usr`, `/etc` and friends are
  refused outright. Symlinks are resolved before the check. Deletes inside the
  workspace move to `~/JARVIS/.trash`.
* **Shell is allowlisted.** Read-only diagnostic commands run; anything with
  pipes, redirects or a denylisted binary (`rm`, `sudo`, `curl`, `diskutil`…)
  needs explicit confirmation, and the model is pushed towards dedicated tools
  instead.
* **The clipboard is treated as sensitive.** Credential-shaped contents are
  shown on screen but never read aloud, and never sent to a remote provider
  while `clipboard_remote_guard` is on.
* **Screen capture is on demand by default.** Captures are written to
  `~/JARVIS/captures` and shown in the interface so you always see what
  JARVIS saw. A separate, off-by-default setting
  (`capabilities.screen_awareness`) enables a background watcher: a cheap
  constant poll of which app/window is frontmost that only ever gates
  occasional, cooldown-limited vision-model calls — never literally
  continuous inference, and never shown in the interface unless you ask or
  turn on its own narration toggle.
* **Confirmations time out.** No answer within 90 seconds means no.

Details: [`docs/security.md`](docs/security.md).

---

## The workspace

```
~/JARVIS/
├── config/config.json     configuration
├── memory/jarvis.db       preferences, facts, conversation (SQLite, local)
├── notes/                 notes JARVIS writes for you
├── tasks/                 research reports and task output
├── captures/              screenshots taken on request
├── logs/jarvis.log        rotating log
└── .trash/                deleted files, recoverable
```

Memory is inspectable and deletable:

```bash
sqlite3 ~/JARVIS/memory/jarvis.db "select text from facts;"
```

or ask: *"What do you remember about me?"* / *"Forget that."*

---

## Architecture

```
frontend/                      React + TypeScript, one WebSocket, zero polling
└── src/
    ├── components/            Core visualisation, conversation, activity, results
    ├── hooks/                 socket, browser speech, push-to-talk
    └── state/store.ts         a projection of the backend event stream

backend/jarvis/
├── core/
│   ├── app.py                 assembly: everything is built once, injected everywhere
│   ├── orchestrator.py        one turn: route → execute → respond → speak → remember
│   ├── events.py              the event bus the UI mirrors
│   ├── config.py              defaults ← config file ← environment
│   ├── context.py             layered prompt context (never the whole history)
│   ├── personality.py         the phrasebook and system prompt
│   └── telemetry.py           spans: router, model TTFT, tools, tasks
├── router/
│   ├── quick.py               deterministic command engine (the fast path)
│   └── router.py              heuristics and fast-model classification
├── intelligence/              the agent loop — understand, plan, act, verify, repair
│   ├── state.py               bounded conversational context
│   ├── understanding.py       sentence → objective
│   ├── entities.py            reference resolution ("it", "him", "the second one")
│   ├── catalog.py             tool cards and shortlisting
│   ├── planner.py             plans, for multi-step work only
│   ├── agent.py               the execution loop
│   ├── verify.py              did it actually work?
│   ├── recovery.py            what to do when it didn't
│   ├── schema.py              validated structured decisions
│   └── observability.py       the stage trace
├── models/                    ModelProvider → Ollama | OpenAI-compatible | Anthropic
├── capabilities/              conversation, system, apps, files, clipboard, screen,
│                              browser, research, diagnostics, email, calendar, memory
├── tools/
│   ├── macos/                 the only place that shells out: apps, URLs, clipboard,
│   │                          screenshots, volume, notifications, AppleScript
│   ├── system/                deterministic facts + diagnostics
│   ├── files/                 sandbox + workspace tools
│   ├── web/                   search and readable-content extraction
│   ├── browser/               Safari / Chromium drivers
│   ├── email/                 Apple Mail driver behind a MailBackend interface
│   ├── calendar/              Calendar.app driver behind a CalendarBackend interface
│   ├── screen/                capture + vision
│   └── interaction/           clicking, typing and keys, by accessibility label
├── voice/                     wake word, STT, TTS, the listening loop
├── tasks/                     background tasks with progress and cancellation
├── memory/                    SQLite: preferences, facts, conversation
└── security/                  risk levels and the confirmation broker
```

**One orchestrator and one agent — not a swarm.** The agent, the capabilities
and the fast path all share the model layer, tools, memory, permissions, task
manager and telemetry. Independent model contexts are used only where they
genuinely help (vision, research synthesis), because ten "agents" would add
latency and failure modes, not intelligence.

The V1.1 capability layer is still here and still used by the fast path. It is
also the fallback the whole system drops to when `intelligence.enabled` is off,
which is what makes the new layer measurable rather than merely asserted.

More: [`docs/architecture.md`](docs/architecture.md) and
[`docs/intelligence.md`](docs/intelligence.md).

---

## Development

```bash
./scripts/dev.sh        # backend with reload + Vite with HMR → http://127.0.0.1:5173
```

The Vite dev server proxies `/api` and `/ws` to the Python process, so the UI
hot-reloads while the backend restarts on save.

Useful things to know:

* The UI never calls a tool directly — it sends `{"type": "utterance"}` and
  renders events. If it isn't in the event stream, the UI can't know it.
* Adding an event type means adding it in `core/events.py` **and**
  `frontend/src/lib/events.ts`; the store switch is the contract.
* `jarvis ask "…" --json` is the fastest way to exercise a path without the UI.

---

## Testing

```bash
./scripts/test.sh                       # Python tests + typecheck + interface build
PYTHONPATH=backend ./.venv/bin/pytest tests/ -v
```

247 tests covering routing, tool schemas and validation, permission gating,
the file sandbox, the task manager and cancellation, clipboard handling and
secret detection, system information, the model provider abstraction and
substitution, configuration and memory, the research pipeline, email/calendar
parsing, the voice layer, the HTTP and WebSocket API, and error handling.

### The intelligence suite

`tests/test_intelligence.py` (new in V1.2) is about *behaviour*, not coverage.
It covers the twelve things worth measuring — intent understanding, entity
resolution, follow-up understanding, tool selection, argument extraction,
multi-step planning, result interpretation, verification, recovery, ambiguity
handling, confirmation handling and context retention — including the six
worked conversations:

| | Conversation | What must hold |
| --- | --- | --- |
| A | "Check my emails." → "Anything from my brother?" | the inbox is still context |
| B | "Open Safari." → "Go to the BBC." | the open browser is still context |
| C | "Research specialised cells." → "Which ones are used in medicine?" | the sources are still context |
| D | "What is on my screen?" → "Click the search bar." | the screen is context *and* actionable |
| E | "Open the BBC." → a 404 → "That's not right." | a correction, not a new request |
| F | "Email him." with three senders | a question, not a guess |

Example A crosses paths deliberately: the first turn is a quick match answered
by the V1.1 capability layer and the second is the agent, so the test fails if
context only flows through the agent's own calls.

The reasoning model is scripted, by the *purpose* of each prompt rather than by
call order — the number of model calls depends on what the tools return, which
is the point, so a positional script would be testing the wrong thing. Nothing
in the product special-cases any phrase in these tests: each expectation is met
by the general machinery or it is a genuine failure.

`tests/test_voice_audio.py` (new in V1.1) pins the audio boundary: PCM
conversion across every input shape and dtype, the exact V1.0 regression (bytes
must never reach `predict()`), and the wake loop's behaviour when a detector
fails. Its stub model reproduces openWakeWord's real contract — including the
part that *doesn't* raise — so a fix that silently deafens the detector fails
the suite rather than passing it.

No test requires a paid API, a network connection or a Mac: models, HTTP and
AppleScript are mocked at their boundaries.

---

## Adding a tool

```python
# backend/jarvis/tools/music/tools.py
from ...security.permissions import RiskLevel
from ..base import Tool, ToolContext, ToolResult, ToolSpec


class PlayPlaylistTool(Tool):
    spec = ToolSpec(
        name="play_playlist",
        description="Start a named playlist in Music",
        parameters={
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        },
        risk=RiskLevel.LOW,
        category="music",
        requires_macos=True,
        expected_ms=800,
    )

    def __init__(self, deps):
        self._deps = deps

    async def run(self, args: dict, ctx: ToolContext) -> ToolResult:
        ctx.report(f"Starting {args['name']}…")
        result = await self._deps.controller.osascript(
            f'tell application "Music" to play playlist "{args["name"]}"'
        )
        if not result.ok:
            return ToolResult.failure("Music didn't respond.", detail=result.output)
        return ToolResult(summary=f"Playing {args['name']}.")
```

Register it in `tools/registry.py`, and optionally add a quick-command pattern
in `router/quick.py` so "play my focus playlist" skips the model entirely.

---

## Adding a model provider

Implement three methods:

```python
class MyProvider(ModelProvider):
    name = "myprovider"
    local = False

    async def available(self) -> bool: ...
    async def list_models(self) -> list[str]: ...
    async def stream_chat(self, messages, model, **kwargs):   # async generator
        yield "text delta"
```

Add it to `ModelRouter._build_providers`, then point a slot at it in the
configuration. Nothing above the model layer changes — the router, capabilities
and UI never learn which provider answered.

---

## Troubleshooting

| Symptom | What it means | Fix |
| --- | --- | --- |
| "The local AI service isn't available." | Ollama isn't running | `ollama serve`, then check `jarvis doctor` |
| Conversation fails but commands work | no model installed for that slot | `./scripts/pull-models.sh` |
| "Microphone access is disabled." | macOS permission | System Settings → Privacy & Security → Microphone → allow your terminal |
| Wake word never fires | `openwakeword` missing, model not downloaded, or too much background noise | `pip install -e ".[voice]"`, check the log for `loading wake-word model`, raise `wake_sensitivity` in Settings |
| `must by a Numpy array` in the log | a pre-V1.1 build | update to V1.1 — this is the bug it fixes |
| "Safari didn't respond to the automation request." | Automation permission | Privacy & Security → Automation → allow Safari for your terminal |
| Screen capture fails | Screen Recording permission | Privacy & Security → Screen Recording (restart the terminal afterwards) |
| Mail returns nothing | Mail.app not running or not permitted | open Mail once, accept the automation prompt |
| Research returns nothing | no connectivity, or the search host is blocked | check the network; try `JARVIS_SEARXNG_URL` or a Brave key |
| Interface shows "The interface hasn't been built yet" | no `frontend/dist` | `cd frontend && npm install && npm run build` |
| Everything feels slow | the general model is doing work the fast path should | turn on `DEV` and look at the routing path |
| "I couldn't find a control called…" | the app doesn't label that control, or Accessibility isn't granted | Privacy & Security → Accessibility → allow the app that launched JARVIS |
| Follow-ups don't follow ("anything from my brother?" starts fresh) | the reasoning model isn't resolving references | `DEV` → Reasoning: check the `INTENT` line; a stronger `reasoning` model usually fixes it |
| The agent picks the wrong tool repeatedly | the shortlist or the model | `DEV` → Reasoning shows what it chose and why; try a larger `reasoning` model, or set `intelligence.enabled: false` to compare against V1.1 |
| A request stops after a few steps | the step budget | raise `intelligence.max_steps` (default 6) |

Logs: `~/JARVIS/logs/jarvis.log`. Raise detail with `JARVIS_LOG_LEVEL=DEBUG`.

More: [`docs/troubleshooting.md`](docs/troubleshooting.md).

---

## Known limitations

Stated plainly, so testing is aimed at the right things.

**Not yet verified with a live microphone.** The V1.1 fix is verified against the
real `openwakeword` package and model — the old crash reproduces, the new path
runs continuously, and the audio inside openWakeWord's buffer is bit-identical to
the captured frames. But no test in this repository has put a *human voice*
through the pipeline: saying "Jarvis" and getting a response is unverified, as is
the wake → Whisper → orchestrator → speech → back-to-listening round trip on
real hardware. If the wake word does not fire on your Mac, that is now a
detection/tuning question (`wake_sensitivity`, microphone gain, background noise)
rather than the crash V1.1 fixes — check `~/JARVIS/logs/jarvis.log` for
`wake-word detection failed`, which would indicate a software fault instead.

**V1.2 has not been run against a live local model.** The intelligence layer is
verified with a scripted reasoning model — that is what makes the tests measure
the architecture rather than the weather inside an 8B model — and with the whole
stack offline, where it degrades honestly ("the local AI service isn't
available") instead of inventing an answer. What has *not* been measured here is
how well `llama3.1:8b` actually plays the reasoning role: how often it picks the
right tool, how often its JSON parses first time, and what a turn really costs
in seconds on an M-series Mac. Expect to want a larger model in the `reasoning`
slot; that is one line of configuration (see [Model roles](#model-roles)).

**The UI interaction tools are unverified on hardware.** `click_element`,
`type_text` and `press_key` use System Events and the Accessibility API. They
are correct AppleScript and they fail with a clear message when Accessibility
permission is missing, but no test in this repository has clicked a real button
on a real Mac. Element lookup is bounded to the front window and matches by
accessibility name or description; applications that don't label their controls
won't be reachable this way.

**Other current limits**

* No Spotify, HomeKit, Reminders, Messages or Contacts yet — the tool interface
  is ready for them ([`docs/extending.md`](docs/extending.md)).
* Browser automation reads, opens and (via the interaction tools) clicks and
  types; it doesn't fill forms programmatically or drive JavaScript.
* The agent is bounded to six steps and two repairs per step by default. Work
  that genuinely needs more will stop and say what it got to, rather than
  looping.
* Reference resolution reads the conversation, not a contacts database: "my
  brother" resolves to a person who has appeared in context, and otherwise
  becomes a question.
* Email is Apple Mail only (the `MailBackend` interface is there for IMAP).
* Research reads static HTML; it doesn't run JavaScript-heavy pages.
* Vision is single-screenshot on demand by default; an off-by-default
  background watcher (`capabilities.screen_awareness`) adds throttled,
  change-gated screen awareness — see the screen-capture note above.
* The interface is a local web app served by the backend, not a signed `.app`.
* `openwakeword` only ships pretrained models for a few phrases. "Jarvis" is one
  of them; a custom wake word needs the Whisper wake engine or a trained model.

---

## Licence

Personal project. Use it on your own machine.
