"""UI interaction.

Screen understanding is only half of "click the search bar and type this" — the
other half is being able to act on what was seen. These tools drive macOS
through Accessibility and System Events, semantically wherever possible:
elements are found by their accessibility label, not by clicking at guessed
coordinates, which is brittle and breaks the moment a window moves.

All of them change state, so they are MEDIUM risk and pass through the normal
confirmation policy. None of them can run without Accessibility permission, and
they say so plainly when it is missing.

**Disambiguation.** ``click_element`` never guesses among several matches: it
reports every control whose name or description contains the given label, and
a follow-up call with ``index`` picks one deterministically. There is no
cross-call handle for a native element (the deliberate "no coordinate
clicking" design applies here too — a stale reference would be worse than a
fresh search), so an index-addressed click re-runs the same search and clicks
whatever is now at that position; the trade-off is a small window in which the
tree could reorder between the search and the click, which is the same trade
every one-shot click here already made.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from ...security.permissions import RiskLevel
from ..base import Tool, ToolContext, ToolResult, ToolSpec
from ..macos.controller import MacOSController, _esc

#: Named keys mapped to AppleScript key codes.
KEY_CODES = {
    "return": 36, "enter": 36, "tab": 48, "space": 49, "delete": 51, "backspace": 51,
    "escape": 53, "esc": 53, "left": 123, "right": 124, "down": 125, "up": 126,
    "home": 115, "end": 119, "pageup": 116, "pagedown": 121,
}

MODIFIERS = {"command": "command down", "cmd": "command down", "shift": "shift down",
             "option": "option down", "alt": "option down", "control": "control down",
             "ctrl": "control down"}

#: Accessibility "role description" values ``click_element``/``wait_for_element``
#: search by. Deliberately broader than the minimum needed for a text field or
#: a button, so tabs, menu items and table rows are reachable too.
CLICKABLE_ROLES: tuple[str, ...] = (
    "text field", "search field", "button", "link", "checkbox", "pop up button",
    "radio button", "slider", "tab", "menu item", "table row", "static text",
    "disclosure triangle", "stepper", "combo box",
)

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
        result = await self._deps.controller.osascript(_FRONTMOST_SCRIPT, timeout=12.0)
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
        # Already needed for the summary below — Verifier reuses this same
        # field as its (free) evidence that the keystrokes landed somewhere
        # real, rather than issuing a second probe of its own.
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
        description=(
            "Click a named control (button, field, link…) in a window, addressed by its "
            "accessibility name or description. Reports every match instead of guessing when "
            "there's more than one — pass index to pick one"
        ),
        parameters={
            "type": "object",
            "properties": {
                "label": {"type": "string",
                          "description": "the visible name of the control, e.g. 'Search'"},
                "index": {"type": "integer",
                          "description": "which match to click, when a previous call reported more than one"},
                "app": {"type": "string", "default": "",
                       "description": "defaults to the frontmost application"},
                "window_index": {"type": "integer",
                                 "description": "defaults to that application's front window"},
            },
            "required": ["label"],
        },
        risk=RiskLevel.MEDIUM,
        category="screen",
        requires_macos=True,
        expected_ms=1500,
        returns="whether a matching control was found and clicked, or the candidates if more than one matched",
        mutates=True,
        retryable=True,
        examples=["click the search bar", "press the send button", "click that link"],
    )

    def __init__(self, deps):
        self._deps = deps

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        label = str(args["label"]).strip()
        app = (args.get("app") or "").strip() or None
        window_index = args.get("window_index")
        ctx.report(f"Looking for “{label}”…", tool="click_element")

        index = args.get("index")
        if index is not None:
            output = await _click_at_index(self._deps.controller, label, int(index),
                                           app=app, window_index=window_index)
            return _click_outcome(output, label)

        status, candidates = await _find_elements(self._deps.controller, label,
                                                   app=app, window_index=window_index)
        if status == "error":
            return ToolResult.failure(_PERMISSION_HINT)
        if status == "noapp":
            return ToolResult.failure(f"{app} doesn't appear to be running.")
        if status == "nowindow":
            return ToolResult.failure("That application has no window open.")
        if not candidates:
            return ToolResult.failure(
                f"I couldn't find a control called “{label}” in the front window.",
                detail="no accessibility element matched by name or description",
                wrong_tool=True)
        if len(candidates) > 1:
            listing = "; ".join(
                f'{i}: {c["role"] or "control"} “{c["name"] or c["description"] or "(unnamed)"}”'
                for i, c in enumerate(candidates)
            )
            return ToolResult(
                ok=False,
                data={"candidates": candidates},
                summary=f"I found {len(candidates)} controls matching “{label}” — {listing}. "
                        "Call again with an index to pick one.",
            )
        # Exactly one match: re-locate and click it. A second round trip
        # rather than clicking during the search above, so the same search
        # primitive serves wait_for_element (read-only) without ever risking
        # a side effect.
        output = await _click_at_index(self._deps.controller, label, 0, app=app,
                                       window_index=window_index)
        return _click_outcome(output, label)


class WaitForElementTool(Tool):
    spec = ToolSpec(
        name="wait_for_element",
        description=(
            "Poll a window until a named control appears (or stops being ambiguous), instead "
            "of guessing a fixed delay before clicking it"
        ),
        parameters={
            "type": "object",
            "properties": {
                "label": {"type": "string"},
                "timeout_s": {"type": "number", "default": 10.0},
                "app": {"type": "string", "default": ""},
                "window_index": {"type": "integer"},
            },
            "required": ["label"],
        },
        risk=RiskLevel.LOW,
        category="screen",
        requires_macos=True,
        expected_ms=2000,
        returns="whether the control appeared, how long it took, and the matches found",
        examples=["wait for the page to finish loading", "wait until the confirmation dialog appears"],
    )

    def __init__(self, deps):
        self._deps = deps

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        label = str(args["label"]).strip()
        app = (args.get("app") or "").strip() or None
        window_index = args.get("window_index")
        timeout_s = min(max(float(args.get("timeout_s") or 10.0), 1.0), 60.0)
        started = time.monotonic()
        poll_interval = 0.5

        while True:
            ctx.raise_if_cancelled()
            status, candidates = await _find_elements(self._deps.controller, label,
                                                       app=app, window_index=window_index)
            if status == "error":
                return ToolResult.failure(_PERMISSION_HINT)
            if status == "noapp":
                return ToolResult.failure(f"{app} doesn't appear to be running.")
            if candidates:
                waited = round(time.monotonic() - started, 1)
                return ToolResult(
                    data={"label": label, "found": True, "waited_s": waited, "candidates": candidates},
                    summary=f"“{label}” appeared after {waited}s.",
                )
            elapsed = time.monotonic() - started
            if elapsed >= timeout_s:
                return ToolResult.failure(
                    f"“{label}” didn't appear within {timeout_s:.0f}s.",
                    detail="wait_for_element timed out")
            await asyncio.sleep(min(poll_interval, timeout_s - elapsed))


class ScrollTool(Tool):
    spec = ToolSpec(
        name="scroll",
        description="Scroll the frontmost window up, down, or to the very top or bottom",
        parameters={
            "type": "object",
            "properties": {
                "direction": {"type": "string", "enum": ["up", "down", "top", "bottom"], "default": "down"},
                "amount": {"type": "integer", "default": 1,
                          "description": "repeats for up/down; ignored for top/bottom"},
            },
        },
        risk=RiskLevel.LOW,
        category="screen",
        requires_macos=True,
        expected_ms=500,
        mutates=False,
        examples=["scroll down", "scroll to the top of the page", "page down"],
    )

    #: v1 is key-based (reuses the existing Page Up/Down/Home/End codes).
    #: Genuine scroll-wheel events need CGEventCreateScrollWheelEvent, a
    #: PyObjC/Quartz dependency this repo doesn't have yet — a reasonable
    #: follow-up if key-based scrolling proves insufficient against a real
    #: app, not a v1 blocker.
    _DIRECTIONS = {"up": "pageup", "down": "pagedown", "top": "home", "bottom": "end"}

    def __init__(self, deps):
        self._deps = deps

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        direction = (args.get("direction") or "down").lower()
        key = self._DIRECTIONS.get(direction)
        if key is None:
            return ToolResult.failure(f"I don't know the scroll direction “{direction}”.")
        repeats = 1 if direction in ("top", "bottom") else max(1, min(int(args.get("amount") or 1), 20))
        code = KEY_CODES[key]
        for _ in range(repeats):
            result = await self._deps.controller.osascript(
                f'tell application "System Events" to key code {code}', timeout=10.0)
            if not result.ok:
                return ToolResult.failure(_PERMISSION_HINT, detail=result.output)
        return ToolResult(data={"direction": direction, "amount": repeats},
                          summary=f"Scrolled {direction}.")


class ListWindowsTool(Tool):
    spec = ToolSpec(
        name="list_windows",
        description="List an application's open windows, so a specific one can be addressed by index",
        parameters={
            "type": "object",
            "properties": {
                "app": {"type": "string", "default": "", "description": "defaults to the frontmost application"},
            },
        },
        risk=RiskLevel.LOW,
        category="screen",
        requires_macos=True,
        expected_ms=800,
        examples=["what windows does Safari have open", "list Finder's windows"],
    )

    def __init__(self, deps):
        self._deps = deps

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        app_name = (args.get("app") or "").strip()
        if not app_name:
            app_name = await self._deps.controller.frontmost_app()
        if not app_name:
            return ToolResult.failure("I couldn't tell which application to look at.")
        script = f"""
        tell application "System Events"
            if not (exists (application process "{_esc(app_name)}")) then return "NOAPP"
            tell application process "{_esc(app_name)}"
                set out to ""
                set i to 0
                repeat with w in windows
                    set i to i + 1
                    set wName to ""
                    try
                        set wName to (name of w) as string
                    end try
                    if out is not "" then set out to out & "\\n"
                    set out to out & i & "|" & wName
                end repeat
                return out
            end tell
        end tell
        """
        result = await self._deps.controller.osascript(script, timeout=12.0)
        if not result.ok:
            return ToolResult.failure(_PERMISSION_HINT, detail=result.output)
        output = result.stdout.strip()
        if output == "NOAPP":
            return ToolResult.failure(f"{app_name} doesn't appear to be running.")
        windows = []
        if output:
            for line in output.split("\n"):
                idx, _, wname = line.partition("|")
                windows.append({"index": int(idx) if idx.isdigit() else 0, "title": wname})
        return ToolResult(
            data={"application": app_name, "windows": windows},
            summary=(f"{app_name} has {len(windows)} window(s) open." if windows
                    else f"{app_name} has no windows open."),
            display={"kind": "list", "title": f"{app_name} windows",
                     "items": [w["title"] or f"Window {w['index']}" for w in windows]},
        )


# -- shared AppleScript building blocks --------------------------------------

_FRONTMOST_SCRIPT = (
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


def _roles_list(roles: tuple[str, ...]) -> str:
    return "{" + ", ".join(f'"{r}"' for r in roles) + "}"


def _process_and_window(app: str | None, window_index: int | None) -> tuple[str, str]:
    process_expr = (f'application process "{_esc(app)}"' if app
                    else "first application process whose frontmost is true")
    window_expr = f"window {int(window_index)}" if window_index else "front window"
    return process_expr, window_expr


async def _find_elements(
    controller: MacOSController, label: str, *, roles: tuple[str, ...] = CLICKABLE_ROLES,
    app: str | None = None, window_index: int | None = None,
) -> tuple[str, list[dict[str, str]]]:
    """Search a window's controls for *label*, without clicking anything.

    Returns ``(status, candidates)``: status is ``"ok"`` (candidates may be
    empty), ``"noapp"``, ``"nowindow"`` or ``"error"``. Candidates are in the
    same order an index-addressed click below will use.
    """
    process_expr, window_expr = _process_and_window(app, window_index)
    script = f"""
    tell application "System Events"
        if not (exists ({process_expr})) then return "NOAPP"
        tell {process_expr}
            if (count of windows) is 0 then return "NOWINDOW"
            set theWindow to {window_expr}
            set candidates to {{}}
            repeat with roleName in {_roles_list(roles)}
                try
                    set found to (every UI element of theWindow whose role description is roleName)
                    set candidates to candidates & found
                end try
            end repeat
            set out to ""
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
                    set elementRole to ""
                    try
                        set elementRole to (role description of anElement) as string
                    end try
                    if out is not "" then set out to out & "\\n"
                    set out to out & elementRole & "|" & elementName & "|" & elementDescription
                end if
            end repeat
            if out is "" then return "NOTFOUND"
            return "FOUND:" & out
        end tell
    end tell
    """
    result = await controller.osascript(script, timeout=25.0)
    if not result.ok:
        return "error", []
    output = result.stdout.strip()
    if output == "NOAPP":
        return "noapp", []
    if output == "NOWINDOW":
        return "nowindow", []
    if output == "NOTFOUND":
        return "ok", []
    if output.startswith("FOUND:"):
        candidates = []
        for line in output[len("FOUND:"):].split("\n"):
            role, _, rest = line.partition("|")
            name, _, description = rest.partition("|")
            candidates.append({"role": role, "name": name, "description": description})
        return "ok", candidates
    return "error", []  # pragma: no cover - AppleScript returned something unexpected


async def _click_at_index(
    controller: MacOSController, label: str, index: int, *,
    roles: tuple[str, ...] = CLICKABLE_ROLES, app: str | None = None,
    window_index: int | None = None,
) -> str:
    """Re-locate *label*'s matches (same ordering :func:`_find_elements` uses)
    and click the one at *index*. A fresh search rather than a cached handle
    — see the module docstring for why."""
    process_expr, window_expr = _process_and_window(app, window_index)
    script = f"""
    tell application "System Events"
        if not (exists ({process_expr})) then return "NOAPP"
        tell {process_expr}
            if (count of windows) is 0 then return "NOWINDOW"
            set theWindow to {window_expr}
            set candidates to {{}}
            repeat with roleName in {_roles_list(roles)}
                try
                    set found to (every UI element of theWindow whose role description is roleName)
                    set candidates to candidates & found
                end try
            end repeat
            set matches to {{}}
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
                    set matches to matches & {{anElement}}
                end if
            end repeat
            if (count of matches) is 0 then return "NOTFOUND"
            if {int(index)} >= (count of matches) then return "BADINDEX:" & (count of matches)
            set target to item ({int(index)} + 1) of matches
            set tName to ""
            try
                set tName to (name of target) as string
            end try
            set tDesc to ""
            try
                set tDesc to (description of target) as string
            end try
            click target
            return "CLICKED:" & tName & "|" & tDesc
        end tell
    end tell
    """
    result = await controller.osascript(script, timeout=25.0)
    if not result.ok:
        return f"ERROR:{result.output}"
    return result.stdout.strip()


def _click_outcome(output: str, label: str) -> ToolResult:
    if output.startswith("CLICKED:"):
        matched = output[len("CLICKED:"):].strip(" |")
        return ToolResult(data={"label": label, "matched": matched},
                          summary=f"Clicked {matched or label}.")
    if output == "NOAPP":
        return ToolResult.failure("That application doesn't appear to be running.")
    if output == "NOWINDOW":
        return ToolResult.failure("That application has no window open.")
    if output.startswith("BADINDEX:"):
        count = output[len("BADINDEX:"):].strip()
        return ToolResult.failure(
            f"There are only {count} matches for “{label}” now — the page may have changed. "
            "Read it again before picking an index.")
    if output == "NOTFOUND":
        return ToolResult.failure(
            f"I couldn't find a control called “{label}” anymore — the window may have changed.",
            wrong_tool=True)
    if output.startswith("ERROR:"):
        return ToolResult.failure(_PERMISSION_HINT, detail=output[len("ERROR:"):])
    return ToolResult.failure("The click didn't complete as expected.", detail=output)  # pragma: no cover


def interaction_tools(deps) -> list[Tool]:
    return [FrontmostAppTool(deps), TypeTextTool(deps), PressKeyTool(deps),
            ClickElementTool(deps), WaitForElementTool(deps), ScrollTool(deps),
            ListWindowsTool(deps)]
