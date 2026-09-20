# Extending JARVIS

The infrastructure for the obvious next features already exists. This is how to
add them cleanly.

## The shape of a feature

Since V1.2 a feature is usually **just a tool**. The agent selects tools from
the registry by description, examples and category, so a well-described tool is
reachable the moment it is registered — no capability, no classifier entry, no
prompt to edit.

Add the other two pieces only when they earn their place:

1. **Tool** — the thing that actually happens, with a schema, a risk level and
   an honest description. *Always.*
2. **Quick command** — a regular expression, when one phrasing is so common and
   so unambiguous that paying for a model would be waste. *Often.*
3. **Capability** — when a sentence needs a multi-step flow of its own that the
   agent shouldn't have to re-derive each time (research, diagnostics).
   *Rarely.*

### Writing a tool the agent can actually use

Four `ToolSpec` fields exist for the agent's benefit. They cost a line each and
they are the difference between a tool being chosen correctly and being chosen
at random:

```python
spec = ToolSpec(
    name="play_music",
    description="Play music in Spotify, optionally a named playlist or artist",
    examples=["play something", "put on the Bowie playlist"],  # feeds shortlisting
    returns="what started playing",     # so the model knows if this answers it
    mutates=True,                       # default: derived from the risk level
    retryable=False,                    # default: derived from `mutates`
    ...
)
```

And one on the result, for the fast path:

```python
# "this request wasn't mine to carry out" — not "I tried and failed".
# The orchestrator reconsiders the turn instead of ending it.
return ToolResult.failure(f"There's no playlist called {name}.", wrong_tool=True)
```

Use `wrong_tool=True` only when the tool could not identify its target at all.
A refusal, a permission error or a genuine failure is the honest answer and
should be reported as one.

## Worked example: Spotify

```python
# backend/jarvis/tools/music/tools.py
class PlayTool(Tool):
    spec = ToolSpec(
        name="play_music",
        description="Play music in Spotify, optionally a named playlist or artist",
        parameters={"type": "object", "properties": {"query": {"type": "string", "default": ""}}},
        risk=RiskLevel.LOW, category="music", requires_macos=True, expected_ms=900,
    )

    def __init__(self, deps):
        self._deps = deps

    async def run(self, args, ctx):
        script = ('tell application "Spotify" to play'
                  if not args.get("query")
                  else f'tell application "Spotify" to play track "{args["query"]}"')
        result = await self._deps.controller.osascript(script)
        return (ToolResult(summary="Playing.") if result.ok
                else ToolResult.failure("Spotify didn't respond.", detail=result.output))
```

```python
# backend/jarvis/capabilities/simple.py
class MusicCapability(ToolPlanCapability):
    name = "music"
    description = "Playback control in Spotify or Music."
    tools = ("play_music", "pause_music", "skip_track", "now_playing")
    default_tool = "now_playing"
```

```python
# backend/jarvis/router/quick.py
_c(r"^(?:play|resume)(?: some)? music$", RouteKind.TOOL, "play_music", {}),
_c(r"^(?:pause|stop) (?:the )?music$", RouteKind.TOOL, "pause_music", {}),
_c(r"^what'?s playing\??$", RouteKind.TOOL, "now_playing", {}),
```

Then register the tools in `tools/registry.py`. That alone makes them reachable
by the agent. The capability and the `router/router.py` capability-table entry
are only needed if you also want the quick path and the V1.1 fallback to know
about them.

**Context for free.** If your tool's category is one `ConversationState._absorb`
already understands (`browser`, `email`, `research`, `screen`, `files`, `macos`,
`system`), its results become conversational context with no further work — and
follow-up questions about them work. A genuinely new kind of context is a small
addition there, dispatching on payload shape rather than on your tool's name.

## Where each planned feature belongs

| Feature | Approach |
| --- | --- |
| Reminders, Notes, Messages, Contacts | AppleScript drivers like `tools/email/mail_app.py`, behind an interface |
| HomeKit | a shortcut-runner tool (`shortcuts run …`) or the Home AppleScript dictionary |
| PDF / document analysis | a tool that extracts text, then the existing research synthesis |
| GitHub, Docker, dev environment | allowlisted CLI tools with MEDIUM risk and structured output |
| Annotated screenshots | extend `tools/screen` — the capture pipeline already downscales and encodes |
| Scheduled workflows | a scheduler that calls `orchestrator.handle()` on a cron trigger |
| A different browser | implement `BrowserDriver` in `tools/browser/tools.py` |
| IMAP mail | implement `MailBackend`; the email tools and capability are unchanged |
| A neural TTS voice | implement `TTSEngine`; the voice manager is unchanged |

## Rules of thumb

* **Deterministic beats clever.** If the answer exists in an API, fetch it and
  template the sentence. Save the model for language, not lookup.
* **Declare risk honestly.** The gate is only as good as the declaration.
* **Report progress.** Anything over a second should call `ctx.report(...)` so
  the activity panel shows it.
* **Support cancellation.** Poll `ctx.cancelled()` in any loop.
* **Fail in a sentence.** Return `ToolResult.failure("A calm explanation.",
  detail="the technical bit")` — the detail only ever reaches developer mode.
* **Test at the boundary.** Mock the AppleScript or the HTTP call, not your own
  code.
* **Describe it for a stranger.** The agent's only knowledge of your tool is its
  description, examples and `returns`. If a colleague couldn't tell from those
  three lines when to use it, neither can an 8B model.
* **Return structured data, not just prose.** `ToolResult.data` is what becomes
  context and what the next decision reads. `summary` is for speech.
