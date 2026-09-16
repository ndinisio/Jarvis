# Troubleshooting

Start with `jarvis doctor` — it checks the platform, workspace, providers,
model slots, voice components and macOS permissions in one pass.

## Models

**"The local AI service isn't available."**
Ollama isn't reachable. `ollama serve`, then `curl http://127.0.0.1:11434/api/tags`.
If it runs on another host or port, set `JARVIS_OLLAMA_HOST`.

**Commands work but conversation doesn't.**
That is the design working: deterministic paths don't need a model. Install one:
`./scripts/pull-models.sh`.

**Answers are slow.**
Turn on `DEV` and look at the routing path. `quick` and `heuristic` should
dominate everyday use. If simple requests show `model`, the phrasing probably
isn't covered by a quick command — add one. If `model.ttft` is seconds, the
general model is too large for the machine: try `./scripts/pull-models.sh --small`.

**"…isn't installed."**
`ollama list` shows what you have; `jarvis models` shows what each slot resolved
to and whether it was substituted.

## Voice

**The wake word never fires.**
`pip install -e ".[voice]"`. `openwakeword` only ships pretrained models for a
few phrases (including "jarvis"); for a custom phrase switch the wake engine to
`whisper` in Settings. Raise `wake_sensitivity` in a noisy room.

**"Microphone access is disabled."**
System Settings → Privacy & Security → Microphone. Grant it to the *terminal or
app that launched JARVIS*, then restart it.

**Speech recognition is inaccurate.**
Use a better model (`small.en`), and set `stt_language` if you aren't speaking
English. First use downloads the model — that pause is the download.

**No speech output.**
Off macOS, replies are spoken by the browser (the tab must be open and allowed to
play audio). On macOS, check `say -v Daniel "test"` in a terminal and that the
voice in Settings is installed.

**It talks over me.**
`barge_in` is on by default; speaking should cut it off. If local capture isn't
running, use the microphone button or say "stop".

## macOS permissions

**"Safari didn't respond to the automation request."**
Privacy & Security → Automation → your terminal → allow Safari. The first attempt
raises the prompt; if you dismissed it, remove the entry with
`tccutil reset AppleEvents` and try again.

**Screen capture fails.**
Privacy & Security → Screen Recording, then fully quit and restart the terminal —
macOS only applies this permission to newly launched processes.

**Mail or Calendar returns nothing.**
Open the app once so it is running, accept the automation prompt, and confirm
the account is actually syncing. AppleScript against a large mailbox is slow; the
tools cap how much they read.

## Research

**"I couldn't reach a search service."**
Check connectivity. Corporate networks and some VPNs block the DuckDuckGo HTML
endpoint — point JARVIS at a self-hosted SearXNG (`JARVIS_SEARXNG_URL`) or set
`JARVIS_BRAVE_API_KEY`.

**Pages come back empty.**
JavaScript-rendered pages don't extract; the search snippets are still used, and
the report says what it could and couldn't read.

## Interface

**"The interface hasn't been built yet."**
`cd frontend && npm install && npm run build`, or use `./scripts/dev.sh`.

**It says "reconnecting".**
The backend stopped or restarted. The socket retries with backoff; check the
terminal and `~/JARVIS/logs/jarvis.log`.

**Nothing happens when I type.**
Look for an open confirmation dialog — a pending high-risk action blocks its own
turn (everything else keeps working).

## Data

**Reset the memory**
```bash
rm ~/JARVIS/memory/jarvis.db      # preferences, facts and conversation
```

**Reset the configuration**
```bash
rm ~/JARVIS/config/config.json    # defaults are rewritten on next start
```

**See what is stored**
```bash
sqlite3 ~/JARVIS/memory/jarvis.db ".tables"
sqlite3 ~/JARVIS/memory/jarvis.db "select * from facts;"
```

## Logs

```bash
tail -f ~/JARVIS/logs/jarvis.log
JARVIS_LOG_LEVEL=DEBUG ./scripts/start.sh
```

Developer mode in the UI shows routing decisions, tool calls, model latency and
time-to-first-token without touching the log.
