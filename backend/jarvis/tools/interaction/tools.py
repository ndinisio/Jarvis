"""UI interaction.

Screen understanding is only half of "click the search bar and type this" — the
other half is being able to act on what was seen. These tools drive macOS
through Accessibility and System Events, semantically wherever possible:
elements are found by their accessibility label, not by clicking at guessed
coordinates, which is brittle and breaks the moment a window moves.

All of them change state, so they are MEDIUM risk and pass through the normal
confirmation policy. None of them can run without Accessibility permission, and
they say so plainly when it is missing.
"""

from __future__ import annotations

from typing import Any

from ...security.permissions import RiskLevel
from ..base import Tool, ToolContext, ToolResult, ToolSpec
from ..macos.controller import _esc

#: Named keys mapped to AppleScript key codes.
KEY_CODES = {
    "return": 36, "enter": 36, "tab": 48, "space": 49, "delete": 51, "backspace": 51,
    "escape": 53, "esc": 53, "left": 123, "right": 124, "down": 125, "up": 126,
    "home": 115, "end": 119, "pageup": 116, "pagedown": 121,
}

MODIFIERS = {"command": "command down", "cmd": "command down", "shift": "shift down",
             "option": "option down", "alt": "option down", "control": "control down",
             "ctrl": "control down"}

_PERMISSION_HINT = (
    "Accessibility permission is required. Allow it in System Settings → "
    "Privacy & Security → Accessibility for whatever launched JARVIS."
)


class FrontmostAppTool(Tool):
    spec = ToolSpec(
        name="get_frontmost_app",
        description="Find out which application and window is currently in front",
        parameters={"type": "object", "properties": {}},
        risk=RiskLevel.LOW,
        category="screen",
        requires_macos=True,
        expected_ms=400,
        returns="the frontmost application and window title",
        examples=["what am I looking at", "which app is open", "what window is in front"],
    )

    def __init__(self, deps):
        self._deps = deps

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        script = (
            'tell application "System Events"\n'
            '  set frontApp to first application process whose frontmost is true\n'
            '  set appName to name of frontApp\n'
            '  set windowTitle to ""\n'
            '  try\n'
            '    set windowTitle to name of front window of frontApp\n'
            '  end try\n'
            '  return appName & "\\n" & windowTitle\n'
            'end tell'
        )
        result = await self._deps.controller.osascript(script, timeout=12.0)
        if not result.ok:
            return ToolResult.failure(_PERMISSION_HINT, detail=result.output)
        parts = result.stdout.strip().split("\n", 1)
        app = parts[0].strip()
        window = parts[1].strip() if len(parts) > 1 else ""
        return ToolResult(
            data={"application": app, "window": window},
            summary=f"{app} is at the front" + (f", showing “{window}”" if window else "."),
            display={"kind": "facts", "title": "Frontmost",
                     "facts": [("Application", app), ("Window", window or "—")]},
        )


class TypeTextTool(Tool):
    spec = ToolSpec(
        name="type_text",
        description="Type text into whatever currently has keyboard focus",
        parameters={
            "type": "object",
            "properties": {
                "text": {"type": "string"},
                "press_return": {"type": "boolean", "default": False},
            },
            "required": ["text"],
        },
        risk=RiskLevel.MEDIUM,
        category="screen",
        requires_macos=True,
        expected_ms=700,
        returns="confirmation of what was typed",
        mutates=True,
        retryable=False,
        examples=["type specialised cells", "write this in the search bar", "enter that text"],
    )

    def __init__(self, deps):
        self._deps = deps

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        text = args["text"]
        ctx.report(f"Typing “{text[:40]}”…", tool="type_text")
        script = f'tell application "System Events" to keystroke "{_esc(text)}"'
        result = await self._deps.controller.osascript(script, timeout=20.0)
        if not result.ok:
            return ToolResult.failure(_PERMISSION_HINT, detail=result.output)
        if args.get("press_return"):
            await self._deps.controller.osascript(
                'tell application "System Events" to key code 36', timeout=10.0)
        target = await self._deps.controller.frontmost_app()
        return ToolResult(
            data={"text": text, "application": target, "submitted": bool(args.get("press_return"))},
            summary=f"Typed “{text[:60]}”" + (" and submitted it." if args.get("press_return")
                                              else f" into {target}." if target else "."),
        )


class PressKeyTool(Tool):
    spec = ToolSpec(
        name="press_key",
        description="Press a key, optionally with modifiers, in the frontmost application",
        parameters={
            "type": "object",
            "properties": {
                "key": {"type": "string", "description": "return, tab, escape, a letter…"},
                "modifiers": {"type": "array", "default": [],
                              "description": "command, shift, option, control"},
            },
            "required": ["key"],
        },
        risk=RiskLevel.MEDIUM,
        category="screen",
        requires_macos=True,
        expected_ms=400,
        returns="confirmation the key was sent",
        mutates=True,
        retryable=False,
        examples=["press enter", "hit escape", "search it", "submit that"],
    )

    def __init__(self, deps):
        self._deps = deps

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        key = str(args["key"]).strip().lower()
        modifiers = [MODIFIERS[m.strip().lower()] for m in (args.get("modifiers") or [])
                     if m.strip().lower() in MODIFIERS]
        using = f" using {{{', '.join(modifiers)}}}" if modifiers else ""

        if key in KEY_CODES:
            action = f"key code {KEY_CODES[key]}{using}"
        elif len(key) == 1:
            action = f'keystroke "{_esc(key)}"{using}'
        else:
            return ToolResult.failure(
                f"I don't know the key “{key}”.",
                detail=f"known keys: {', '.join(sorted(KEY_CODES))} or a single character")

        result = await self._deps.controller.osascript(
            f'tell application "System Events" to {action}', timeout=12.0)
        if not result.ok:
            return ToolResult.failure(_PERMISSION_HINT, detail=result.output)
        pressed = "+".join([*(args.get("modifiers") or []), key])
        return ToolResult(data={"key": key, "modifiers": args.get("modifiers") or []},
                          summary=f"Pressed {pressed}.")


class ClickElementTool(Tool):
    spec = ToolSpec(
        name="click_element",
        description="Click a named control (button, field, link) in the frontmost window",
        parameters={
            "type": "object",
            "properties": {
                "label": {"type": "string",
                          "description": "the visible name of the control, e.g. 'Search'"},
            },
            "required": ["label"],
        },
        risk=RiskLevel.MEDIUM,
        category="screen",
        requires_macos=True,
        expected_ms=1500,
        returns="whether a matching control was found and clicked",
        mutates=True,
        retryable=True,
        examples=["click the search bar", "press the send button", "click that link"],
    )

    def __init__(self, deps):
        self._deps = deps

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        label = str(args["label"]).strip()
        ctx.report(f"Looking for “{label}”…", tool="click_element")
        # Search the front window's controls by accessibility name/description
        # rather than clicking coordinates. Bounded to the front window so the
        # search stays fast on complex applications.
        script = f"""
        tell application "System Events"
            set frontApp to first application process whose frontmost is true
            tell frontApp
                if (count of windows) is 0 then return "NOWINDOW"
                set theWindow to front window
                set candidates to {{}}
                repeat with roleName in {{"text field", "search field", "button", "link", "checkbox", "pop up button"}}
                    try
                        set found to (every UI element of theWindow whose role description is roleName)
                        set candidates to candidates & found
                    end try
                end repeat
                repeat with anElement in candidates
                    set elementName to ""
                    try
                        set elementName to (name of anElement) as string
                    end try
                    set elementDescription to ""
                    try
                        set elementDescription to (description of anElement) as string
                    end try
                    if elementName contains "{_esc(label)}" or elementDescription contains "{_esc(label)}" then
                        click anElement
                        return "CLICKED:" & elementName & "|" & elementDescription
                    end if
                end repeat
                return "NOTFOUND"
            end tell
        end tell
        """
        result = await self._deps.controller.osascript(script, timeout=25.0)
        if not result.ok:
            return ToolResult.failure(_PERMISSION_HINT, detail=result.output)

        output = result.stdout.strip()
        if output.startswith("CLICKED:"):
            matched = output[len("CLICKED:"):].strip(" |")
            return ToolResult(data={"label": label, "matched": matched},
                              summary=f"Clicked {matched or label}.")
        if output == "NOWINDOW":
            return ToolResult.failure("That application has no window open.")
        return ToolResult.failure(
            f"I couldn't find a control called “{label}” in the front window.",
            detail="no accessibility element matched by name or description",
            wrong_tool=True)


def interaction_tools(deps) -> list[Tool]:
    return [FrontmostAppTool(deps), TypeTextTool(deps), PressKeyTool(deps),
            ClickElementTool(deps)]
