"""App tools: see and operate native Mac apps through the Accessibility tree.

The native counterpart of the page tools (``tools/browser/page_tools.py``)
and addressed the same way: ``read_window`` lists a window's controls with
``[axN]`` handles; the other tools act on a handle. Every control is
reachable however deeply it's nested, menus are chosen by path
(``["File", "Export…"]``), and when a window exposes nothing to
Accessibility, ``mark_screen`` reads its text off a screenshot and numbers
what's clickable instead.

Safety is the same as everywhere: each action states what it will really
touch (:meth:`Tool.inspect` reads the control's own label and role, never
the model's description), so "Move to Trash", "Empty Trash…", "Send" or
"Buy" is confirmed individually whatever the model called it; password
fields are refused.
"""

from __future__ import annotations

import base64
import datetime as dt
from typing import Any

from ...models.base import ChatMessage
from ...models.registry import Slot
from ...security.permissions import RiskLevel
from ...surfaces.native import NativeError
from ...surfaces.native.marks import parse_pick
from ..base import Tool, ToolContext, ToolResult, ToolSpec

_HANDLE = {"type": "string", "description": "the [axN] handle shown by read_window"}


def _failure(exc: NativeError) -> ToolResult:
    return ToolResult.failure(exc.message, detail=exc.detail, wrong_tool=exc.wrong_tool)


class _NativeTool(Tool):
    def __init__(self, deps):
        self._deps = deps

    @property
    def native(self):
        return self._deps.native

    async def _target(self, handle: str) -> dict[str, Any] | None:
        try:
            return await self.native.describe(handle)
        except NativeError:
            return None
        except Exception:  # pragma: no cover - inspection is best effort
            return None


class ReadWindowTool(_NativeTool):
    spec = ToolSpec(
        name="read_window",
        description=("List the controls in an app's front window — buttons, fields, rows, tabs, "
                     "sheets — each with a [handle] to act on, plus its menus"),
        parameters={"type": "object", "properties": {
            "app": {"type": "string", "default": "", "description": "defaults to the app in front"},
            "offset": {"type": "integer", "default": 0,
                       "description": "skip this many controls, to see further down a long window"},
        }},
        risk=RiskLevel.LOW,
        category="screen",
        requires_macos=True,
        expected_ms=900,
        returns="the window's controls with handles, open sheets, text on screen and menus",
        examples=["what's in this window", "what can I click in Notes"],
    )

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        try:
            snap, listing = await self.native.read(str(args.get("app") or ""),
                                                   offset=max(0, int(args.get("offset") or 0)))
        except NativeError as exc:
            return _failure(exc)
        where = f"{snap.app}'s window “{snap.title}”" if snap.title else f"{snap.app}'s window"
        return ToolResult(
            data={"application": snap.app, "window": snap.title, "controls": snap.total,
                  "blocked": bool(snap.blockers)},
            summary=f"{snap.total} controls in {where}.",
            observation=listing,
        )


class ClickControlTool(_NativeTool):
    spec = ToolSpec(
        name="click_control",
        description="Click (press) a control in an app window by its [handle] from read_window",
        parameters={"type": "object", "properties": {
            "handle": _HANDLE,
            "label": {"type": "string", "default": "", "description": "what the control says, for the log"},
            "clicks": {"type": "integer", "default": 1, "description": "2 to double-click (open)"},
            "button": {"type": "string", "enum": ["left", "right"], "default": "left"},
        }, "required": ["handle"]},
        risk=RiskLevel.MEDIUM,
        category="screen",
        requires_macos=True,
        expected_ms=700,
        returns="what was pressed",
        mutates=True,
        retryable=False,
        confirmation_template="Click “{label}”?",
        examples=["click New Note", "open that file", "press the Share button"],
    )

    async def inspect(self, args: dict[str, Any], ctx: ToolContext) -> dict[str, Any] | None:
        return await self._target(str(args["handle"]))

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        clicks = max(1, min(int(args.get("clicks") or 1), 3))
        button = "right" if str(args.get("button") or "").lower() == "right" else "left"
        try:
            summary = await self.native.press(str(args["handle"]), clicks=clicks, button=button)
        except NativeError as exc:
            return _failure(exc)
        return ToolResult(data={"handle": args["handle"], "clicks": clicks, "button": button,
                                "application": self.native.last_app}, summary=summary)


class TypeIntoTool(_NativeTool):
    spec = ToolSpec(
        name="type_into",
        description=("Type into a field in an app window by its [handle]; replaces what's there "
                     "unless replace is false. Never a password"),
        parameters={"type": "object", "properties": {
            "handle": _HANDLE,
            "text": {"type": "string"},
            "replace": {"type": "boolean", "default": True},
            "submit": {"type": "boolean", "default": False,
                       "description": "press Return afterwards"},
        }, "required": ["handle", "text"]},
        risk=RiskLevel.MEDIUM,
        category="screen",
        requires_macos=True,
        expected_ms=900,
        returns="what the field says afterwards",
        mutates=True,
        retryable=False,
        confirmation_template="Type “{text}” into “{label}”?",
        examples=["type milk into the search field", "put the subject in"],
    )

    async def inspect(self, args: dict[str, Any], ctx: ToolContext) -> dict[str, Any] | None:
        return await self._target(str(args["handle"]))

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        text = str(args["text"])
        try:
            summary, value = await self.native.type_into(
                str(args["handle"]), text, replace=args.get("replace") is not False,
                submit=bool(args.get("submit")))
        except NativeError as exc:
            return _failure(exc)
        shown = f" The field now reads “{value[:160]}”." if value else ""
        return ToolResult(data={"handle": args["handle"], "value": value,
                                "application": self.native.last_app},
                          summary=summary, observation=summary + shown)


class ChooseOptionTool(_NativeTool):
    spec = ToolSpec(
        name="choose_option",
        description="Choose an option from a pop-up menu or menu button in an app window, by its [handle]",
        parameters={"type": "object", "properties": {
            "handle": _HANDLE,
            "option": {"type": "string", "description": "the option's text"},
        }, "required": ["handle", "option"]},
        risk=RiskLevel.MEDIUM,
        category="screen",
        requires_macos=True,
        expected_ms=900,
        returns="the option chosen, or the options there are",
        mutates=True,
        confirmation_template="Choose “{option}” in “{label}”?",
        examples=["set the format to PDF", "choose A4"],
    )

    async def inspect(self, args: dict[str, Any], ctx: ToolContext) -> dict[str, Any] | None:
        target = await self._target(str(args["handle"]))
        if target is not None:
            target = {**target, "option": str(args.get("option") or "")}
        return target

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        try:
            summary = await self.native.choose_option(str(args["handle"]), str(args["option"]))
        except NativeError as exc:
            return _failure(exc)
        # A single-element re-read (not a full read_window) — cheap enough
        # to do on every call, and gives the verifier real evidence that
        # the control now actually shows what was chosen, rather than
        # having to trust the action alone (intelligence/verify.py).
        after = await self._target(str(args["handle"]))
        current_value = str(after.get("value") or "") if after else ""
        return ToolResult(data={"handle": args["handle"], "option": args["option"],
                                "application": self.native.last_app, "current_value": current_value},
                          summary=summary)


class ChooseMenuItemTool(_NativeTool):
    spec = ToolSpec(
        name="choose_menu_item",
        description=("Choose an item from an app's menu bar by its path, like "
                     "[\"File\", \"Export as PDF…\"] or [\"Format\", \"Font\", \"Bold\"]"),
        parameters={"type": "object", "properties": {
            "path": {"type": "array", "items": {"type": "string"},
                     "description": "the menu, then the item (and any submenu in between)"},
            "app": {"type": "string", "default": "", "description": "defaults to the app in front"},
        }, "required": ["path"]},
        risk=RiskLevel.MEDIUM,
        category="screen",
        requires_macos=True,
        expected_ms=900,
        returns="the menu item chosen, or the items there are",
        mutates=True,
        retryable=False,
        confirmation_template="Choose “{label}” from the menu?",
        examples=["save it as a PDF", "make it bold", "open a new window"],
    )

    async def inspect(self, args: dict[str, Any], ctx: ToolContext) -> dict[str, Any] | None:
        path = [str(p) for p in args.get("path") or []]
        app = str(args.get("app") or "").strip() or await self._deps.controller.frontmost_app()
        return {"role": "menu item", "text": path[-1] if path else "", "application": app}

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        path = [str(p) for p in args.get("path") or []]
        try:
            summary = await self.native.choose_menu(path, str(args.get("app") or ""))
        except NativeError as exc:
            return _failure(exc)
        return ToolResult(data={"path": path, "application": self.native.last_app}, summary=summary)


class DragControlTool(_NativeTool):
    spec = ToolSpec(
        name="drag_control",
        description="Drag one control onto another in an app window (a file onto a folder), by handles",
        parameters={"type": "object", "properties": {
            "handle": {"type": "string", "description": "the [axN] handle of what to drag"},
            "onto": {"type": "string", "description": "the [axN] handle of where to drop it"},
        }, "required": ["handle", "onto"]},
        risk=RiskLevel.MEDIUM,
        category="screen",
        requires_macos=True,
        expected_ms=1200,
        returns="what was dragged where",
        mutates=True,
        retryable=False,
        confirmation_template="Drag it onto “{label}”?",
        examples=["drag the report into the Archive folder"],
    )

    async def inspect(self, args: dict[str, Any], ctx: ToolContext) -> dict[str, Any] | None:
        # Where it's dropped decides: onto the Trash is a delete.
        return await self._target(str(args["onto"]))

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        try:
            summary = await self.native.drag(str(args["handle"]), str(args["onto"]))
        except NativeError as exc:
            return _failure(exc)
        return ToolResult(data={"handle": args["handle"], "onto": args["onto"],
                                "application": self.native.last_app}, summary=summary)


class MarkScreenTool(_NativeTool):
    spec = ToolSpec(
        name="mark_screen",
        description=("Photograph an app's window, read its text, and number everything worth "
                     "clicking — for windows read_window can't see into (canvases, games, some apps)"),
        parameters={"type": "object", "properties": {
            "app": {"type": "string", "default": "", "description": "defaults to the app in front"},
        }},
        risk=RiskLevel.LOW,
        category="screen",
        requires_macos=True,
        expected_ms=2500,
        returns="numbered marks [mN] with the text or control at each",
    )

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        if not self._deps.config.security.allow_screen_capture:
            return ToolResult.failure("Screen capture is disabled in the configuration.")
        captures = self._deps.config.captures_dir
        captures.mkdir(parents=True, exist_ok=True)

        async def capture(pid: int, window_number: int):
            path = captures / f"window-{dt.datetime.now():%Y%m%d-%H%M%S-%f}.png"
            result = await self._deps.controller.run(
                ["/usr/sbin/screencapture", "-x", "-o", f"-l{window_number}", str(path)], timeout=20.0)
            if not result.ok or not path.exists():
                raise NativeError("I couldn't photograph that window — Screen Recording permission "
                                  "may be off.", detail=result.output)
            return path

        try:
            marks, listing, overlay = await self.native.mark(capture, str(args.get("app") or ""),
                                                             overlay_dir=captures)
        except NativeError as exc:
            return _failure(exc)
        display = None
        if overlay is not None:
            encoded = base64.b64encode(overlay.read_bytes()).decode("ascii")
            display = {"kind": "image", "title": "What JARVIS can point at",
                       "image": f"data:image/png;base64,{encoded}", "path": str(overlay)}
        return ToolResult(data={"marks": len(marks), "overlay": str(overlay) if overlay else ""},
                          summary=f"Marked {len(marks)} things on screen.", observation=listing,
                          display=display)


class ClickMarkTool(_NativeTool):
    spec = ToolSpec(
        name="click_mark",
        description="Click the thing at a numbered mark [mN] from mark_screen",
        parameters={"type": "object", "properties": {
            "mark": {"type": "integer", "description": "the number of the mark"},
            "clicks": {"type": "integer", "default": 1},
            "button": {"type": "string", "enum": ["left", "right"], "default": "left"},
        }, "required": ["mark"]},
        risk=RiskLevel.MEDIUM,
        category="screen",
        requires_macos=True,
        expected_ms=600,
        returns="what was clicked",
        mutates=True,
        retryable=False,
        confirmation_template="Click “{label}”?",
    )

    async def inspect(self, args: dict[str, Any], ctx: ToolContext) -> dict[str, Any] | None:
        number = _mark_number(args.get("mark"))
        mark = next((m for m in self.native.marks() if m.number == number), None)
        if mark is None:
            return None
        app = await self._deps.controller.frontmost_app()
        return {"role": mark.kind, "text": mark.label, "application": app}

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        clicks = max(1, min(int(args.get("clicks") or 1), 3))
        button = "right" if str(args.get("button") or "").lower() == "right" else "left"
        try:
            mark = await self.native.click_mark(_mark_number(args.get("mark")), clicks=clicks,
                                                button=button)
        except NativeError as exc:
            return _failure(exc)
        what = f"“{mark.label}”" if mark.label else f"mark {mark.number}"
        return ToolResult(data={"mark": mark.number, "label": mark.label},
                          summary=f"Clicked {what}.")


class FindOnScreenTool(_NativeTool):
    spec = ToolSpec(
        name="find_on_screen",
        description=("Ask the vision model which numbered mark is something with no words on it "
                     "(an icon, a colour, a picture) — after mark_screen"),
        parameters={"type": "object", "properties": {
            "description": {"type": "string", "description": "what to find, e.g. 'the gear icon'"},
        }, "required": ["description"]},
        risk=RiskLevel.LOW,
        category="screen",
        requires_macos=True,
        expected_ms=6000,
        returns="the mark that matches, if any",
    )

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        marks = self.native.marks()
        overlay = self.native.last_overlay()
        if not marks or overlay is None:
            return ToolResult.failure("There are no marks to choose from — call mark_screen first.")
        description = str(args["description"]).strip()
        image = base64.b64encode(overlay.read_bytes()).decode("ascii")
        prompt = (f"This screenshot has numbered boxes drawn on it. Which number is on: {description}? "
                  "Reply with the number only, or 0 if it isn't there.")
        try:
            completion = await self._deps.models.complete(
                Slot.VISION, [ChatMessage("user", prompt, images=[image])], max_tokens=10,
                temperature=0.0)
        except Exception as exc:
            return ToolResult.failure("No vision model is available to look at the screen.",
                                      detail=str(exc))
        number = parse_pick(completion.text, len(marks))
        if number is None:
            return ToolResult.failure(f"I couldn't see {description} on screen.")
        mark = next(m for m in marks if m.number == number)
        return ToolResult(data={"mark": number, "label": mark.label},
                          summary=f"That's mark {number}.", observation=f"{description}: {mark.line()}")


def _mark_number(value: Any) -> int:
    try:
        return int(str(value).strip().lstrip("[").lstrip("mM").rstrip("]"))
    except (TypeError, ValueError):
        return 0


def native_tools(deps) -> list[Tool]:
    return [ReadWindowTool(deps), ClickControlTool(deps), TypeIntoTool(deps), ChooseOptionTool(deps),
            ChooseMenuItemTool(deps), DragControlTool(deps), MarkScreenTool(deps), ClickMarkTool(deps),
            FindOnScreenTool(deps)]
