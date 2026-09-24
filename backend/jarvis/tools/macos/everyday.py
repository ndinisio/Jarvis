"""The small, constant requests: tabs, music, dark mode, locking the screen.

Each is a single deterministic action with no model involved — "open a new
tab", "pause the music", "skip this track", "turn on dark mode", "lock my
Mac" — reached directly by the fast path, or by the interpreter restating a
colloquial version ("could you pop a new tab open") as the plain command.
"""

from __future__ import annotations

from typing import Any

from ...security.permissions import RiskLevel
from ..base import Tool, ToolContext, ToolResult, ToolSpec

#: Browsers the tab shortcuts apply to, as their applications are named.
BROWSERS = ("Safari", "Google Chrome", "Arc", "Brave Browser", "Microsoft Edge", "Firefox", "Chromium")

#: Tab action → the standard shortcut every one of those browsers shares.
_TAB_KEYS = {
    "new": ("t", "command down"),
    "close": ("w", "command down"),
    "reopen": ("t", "{command down, shift down}"),
    "back": ("[", "command down"),
    "forward": ("]", "command down"),
    "reload": ("r", "command down"),
}

_TAB_WORDS = {
    "new": "Opened a new tab", "close": "Closed the tab", "reopen": "Reopened the last tab",
    "back": "Went back a page", "forward": "Went forward a page", "reload": "Reloaded the page",
}


def _browser_name(raw: str) -> str:
    lowered = (raw or "").strip().lower()
    for name in BROWSERS:
        if lowered and (lowered in name.lower() or name.lower().startswith(lowered)):
            return name
    return ""


class BrowserTabTool(Tool):
    spec = ToolSpec(
        name="browser_tab",
        description="Open a new tab, close the current tab, reopen the last one, go back or forward, or reload",
        parameters={
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": list(_TAB_KEYS)},
                "browser": {"type": "string", "default": "",
                            "description": "defaults to the browser in front, else Safari"},
            },
            "required": ["action"],
        },
        risk=RiskLevel.LOW,
        category="browser",
        requires_macos=True,
        expected_ms=500,
        examples=["open a new tab", "close this tab", "go back a page", "reload the page"],
    )

    def __init__(self, deps):
        self._deps = deps

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        action = args["action"]
        browser = _browser_name(args.get("browser", ""))
        if not browser:
            front = await self._deps.controller.frontmost_app()
            browser = _browser_name(front) or "Safari"
        key, modifiers = _TAB_KEYS[action]
        result = await self._deps.controller.osascript(
            f'tell application "{browser}" to activate\n'
            "delay 0.15\n"
            f'tell application "System Events" to keystroke "{key}" using {modifiers}'
        )
        if not result.ok:
            return ToolResult.failure(f"{browser} didn't respond.", detail=result.output)
        return ToolResult(data={"browser": browser, "action": action},
                          summary=f"{_TAB_WORDS[action]} in {browser}.")


class MediaControlTool(Tool):
    spec = ToolSpec(
        name="media_control",
        description="Play, pause, skip or go back a track in Spotify (if running) or Music",
        parameters={
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["play", "pause", "playpause", "next", "previous"]},
                "app": {"type": "string", "default": "", "description": "Spotify or Music"},
            },
            "required": ["action"],
        },
        risk=RiskLevel.LOW,
        category="macos",
        requires_macos=True,
        expected_ms=400,
        examples=["pause the music", "next song", "play some music"],
    )

    _VERBS = {"play": "play", "pause": "pause", "playpause": "playpause", "next": "next track",
              "previous": "previous track"}
    _WORDS = {"play": "Playing", "pause": "Paused", "playpause": "Toggled playback",
              "next": "Skipped to the next track", "previous": "Back to the previous track"}

    def __init__(self, deps):
        self._deps = deps

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        app = (args.get("app") or "").strip()
        if app.lower() not in {"spotify", "music"}:
            app = "Spotify" if await self._deps.controller.is_app_running("Spotify") else "Music"
        app = "Spotify" if app.lower() == "spotify" else "Music"
        action = args["action"]
        result = await self._deps.controller.osascript(f'tell application "{app}" to {self._VERBS[action]}')
        if not result.ok:
            return ToolResult.failure(f"{app} didn't respond.", detail=result.output)
        return ToolResult(data={"app": app, "action": action}, summary=f"{self._WORDS[action]} in {app}.")


class AppearanceTool(Tool):
    spec = ToolSpec(
        name="set_appearance",
        description="Switch macOS to dark mode or light mode, or toggle between them",
        parameters={
            "type": "object",
            "properties": {"mode": {"type": "string", "enum": ["dark", "light", "toggle"]}},
            "required": ["mode"],
        },
        risk=RiskLevel.LOW,
        category="macos",
        requires_macos=True,
        expected_ms=500,
        examples=["turn on dark mode", "switch to light mode"],
    )

    def __init__(self, deps):
        self._deps = deps

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        mode = args["mode"]
        value = {"dark": "true", "light": "false", "toggle": "not dark mode"}[mode]
        result = await self._deps.controller.osascript(
            'tell application "System Events" to tell appearance preferences to '
            f"set dark mode to {value}"
        )
        if not result.ok:
            return ToolResult.failure("macOS didn't change its appearance.", detail=result.output)
        word = {"dark": "Dark mode is on", "light": "Light mode is on", "toggle": "Switched appearance"}[mode]
        return ToolResult(data={"mode": mode}, summary=f"{word}.")


class LockScreenTool(Tool):
    spec = ToolSpec(
        name="lock_screen",
        description="Lock the Mac's screen",
        parameters={"type": "object", "properties": {}},
        risk=RiskLevel.LOW,
        category="macos",
        requires_macos=True,
        expected_ms=400,
        examples=["lock my Mac", "lock the screen"],
    )

    def __init__(self, deps):
        self._deps = deps

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        result = await self._deps.controller.osascript(
            'tell application "System Events" to keystroke "q" using {control down, command down}'
        )
        if not result.ok:
            return ToolResult.failure("The screen didn't lock.", detail=result.output)
        return ToolResult(data={"locked": True}, summary="Locked.")


def everyday_tools(deps) -> list[Tool]:
    return [BrowserTabTool(deps), MediaControlTool(deps), AppearanceTool(deps), LockScreenTool(deps)]
