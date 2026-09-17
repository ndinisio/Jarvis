# JARVIS V1.1

A local-first AI operating layer for macOS. You speak; it listens, decides the
cheapest competent way to answer, and either answers instantly or goes away and
does the work while you carry on.

```
You:     "Jarvis."
JARVIS:  "Yes, sir?"
You:     "How much storage do I have?"
JARVIS:  "You have 412 gigabytes available."          ← 40 ms, no model involved

You:     "Research the best current MacBook deals and compare them."
JARVIS:  "I'll look into it, sir."                    ← immediate
         RESEARCH  Searching · Opening 5 results · Comparing prices · Preparing summary
JARVIS:  "I've finished the comparison."              ← spoken summary, detail on screen
```

Everything that can run on your machine does: wake word, speech recognition,
reasoning, memory and speech output. **No paid API is required.**

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

- [What's new in V1.1](#whats-new-in-v11)
- [What it does](#what-it-does)
- [The idea: latency proportional to complexity](#the-idea-latency-proportional-to-complexity)
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
| **Control** | "Stop." · "Mute." · "Turn the volume down." |

45 tools across 9 categories, 12 capabilities, one orchestrator.

---

## The idea: latency proportional to complexity

Most assistants send *everything* to one large model, so "hello" costs the same
as "analyse this codebase". JARVIS routes each request down the cheapest path
that can competently answer it:

```
 request
    │
    ├─ 1. quick commands  ── deterministic patterns ........ ~0.05 ms, no model
    │     "open Safari", "what time is it", "mute", "stop"
    │
    ├─ 2. heuristics ────── weighted keyword scoring ....... ~0.1 ms, no model
    │     "check my email", "why is my mac slow"
    │
    ├─ 3. fast model ────── 1B-class classification ........ ~300–800 ms
    │     anything ambiguous → capability + arguments
    │
    └─ 4. general model ─── conversation and synthesis ..... seconds
          explanations, comparisons, drafting, research reports
```

Two consequences you can feel:

* **Deterministic answers stay deterministic.** "How much storage do I have?"
  is `shutil.disk_usage()` plus a sentence template. The model is never asked.
* **Slow work never blocks you.** Anything long-running is acknowledged in under
  a second, becomes a background task with visible progress, and reports back
  when it's done — while you keep talking.

Developer mode (the `DEV` button) shows the path, the model and the latency of
every request, so a slow path is obvious rather than mysterious.

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

Three slots, each configurable:

| Slot | Default | Used for |
| --- | --- | --- |
| **fast** | `llama3.2:1b` | routing, classification, short rewrites, spoken summaries |
| **general** | `llama3.1:8b` | conversation, reasoning, research synthesis, drafting |
| **vision** | `llava:7b` | screen understanding |

If a configured model isn't installed, JARVIS substitutes a sensible one from
what *is* installed (smallest for fast, largest for general, any vision-capable
model for vision) and says so in `jarvis doctor`. With no model at all, every
deterministic capability still works.

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
| **Accessibility** | window titles, focusing apps | Privacy & Security → Accessibility |
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
    "fast":    { "provider": "ollama", "model": "llama3.2:1b", "timeout_s": 20 },
    "general": { "provider": "ollama", "model": "llama3.1:8b" },
    "vision":  { "provider": "ollama", "model": "llava:7b" }
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
* **Screen capture is on demand only.** There is no polling loop and no
  background capture. Captures are written to `~/JARVIS/captures` and shown in
  the interface so you always see what JARVIS saw.
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
│   ├── quick.py               deterministic command engine (stage 1)
│   └── router.py              heuristics (2) and fast-model classification (3)
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
│   └── screen/                capture + vision
├── voice/                     wake word, STT, TTS, the listening loop
├── tasks/                     background tasks with progress and cancellation
├── memory/                    SQLite: preferences, facts, conversation
└── security/                  risk levels and the confirmation broker
```

**One orchestrator, several capabilities — not a swarm of agents.** Capabilities
share the model layer, tools, memory, permissions, task manager and telemetry.
Independent model contexts are used only where they genuinely help (vision,
research synthesis), because ten "agents" would add latency and failure modes,
not intelligence.

More: [`docs/architecture.md`](docs/architecture.md).

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

219 tests covering routing, tool schemas and validation, permission gating,
the file sandbox, the task manager and cancellation, clipboard handling and
secret detection, system information, the model provider abstraction and
substitution, configuration and memory, the research pipeline, email/calendar
parsing, the voice layer, the HTTP and WebSocket API, and error handling.

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

**Other current limits**

* No Spotify, HomeKit, Reminders, Messages or Contacts yet — the tool interface
  is ready for them ([`docs/extending.md`](docs/extending.md)).
* Browser automation reads and opens; it doesn't fill forms or click.
* Email is Apple Mail only (the `MailBackend` interface is there for IMAP).
* Research reads static HTML; it doesn't run JavaScript-heavy pages.
* Vision is single-screenshot; no continuous monitoring (by design).
* The interface is a local web app served by the backend, not a signed `.app`.
* `openwakeword` only ships pretrained models for a few phrases. "Jarvis" is one
  of them; a custom wake word needs the Whisper wake engine or a trained model.

---

## Licence

Personal project. Use it on your own machine.
