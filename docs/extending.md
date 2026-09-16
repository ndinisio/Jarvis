# Extending JARVIS

The infrastructure for the obvious next features already exists. This is how to
add them cleanly.

## The shape of a feature

Most features are **a tool plus a capability plus (optionally) a quick command**:

1. **Tool** — the thing that actually happens, with a schema and a risk level.
2. **Capability** — maps a sentence to one of a few tools (usually two dozen
   lines subclassing `ToolPlanCapability`).
3. **Quick command** — a regular expression so the common phrasing skips the
   model entirely.

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

Then register the tools in `tools/registry.py`, the capability in
`capabilities/registry.py`, and add `music` to the capability table in
`router/router.py` so the classifier knows it exists.

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
