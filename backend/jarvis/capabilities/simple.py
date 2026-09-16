"""Capabilities that map a sentence onto one of a handful of tools.

They share :class:`ToolPlanCapability`; each one adds only the small amount of
special behaviour that genuinely differs — a deterministic shortcut here, a
different phrasing there.
"""

from __future__ import annotations

from typing import Any

from ..models.registry import Slot
from .base import Request, Response, ToolPlanCapability


class SystemCapability(ToolPlanCapability):
    name = "system"
    description = "Facts about this Mac and simple system controls."
    tools = ("get_system_info", "get_battery", "get_storage", "get_memory", "get_cpu",
             "get_network", "get_processes", "get_time", "set_volume", "check_permissions")
    default_tool = "get_system_info"

    async def handle(self, request: Request) -> Response:
        intent = request.args.get("intent")
        if intent == "volume_step":
            return await self._volume_step(request)
        return await super().handle(request)

    async def _volume_step(self, request: Request) -> Response:
        current = await self.deps.controller.get_volume()
        if current is None:
            return Response(text="I couldn't read the current volume.")
        delta = 15 if request.args.get("direction") == "up" else -15
        target = max(0, min(100, current + delta))
        result = await self.call_tool("set_volume", {"level": target, "action": "set"}, request.ctx)
        return self.respond(result, request)


class AppsCapability(ToolPlanCapability):
    name = "apps"
    description = "Opening, closing and switching between applications."
    tools = ("open_application", "close_application", "activate_application", "list_applications")
    default_tool = "open_application"


class ClipboardCapability(ToolPlanCapability):
    name = "clipboard"
    description = "Reading and writing the clipboard."
    tools = ("read_clipboard", "write_clipboard", "append_clipboard")
    default_tool = "read_clipboard"


class BrowserCapability(ToolPlanCapability):
    name = "browser"
    description = "Opening web pages and reading the page in front of you."
    tools = ("browse_to", "get_current_page", "list_browser_tabs", "open_url")
    default_tool = "browse_to"

    async def handle(self, request: Request) -> Response:
        response = await super().handle(request)
        # "Summarise this page" — the tool returns the text; the model condenses it.
        wants_summary = any(
            word in request.text.lower()
            for word in ("summarise", "summarize", "summary", "what does it say", "tl;dr")
        )
        if wants_summary and response.data and isinstance(response.data, dict):
            text = (response.data.get("text") or "")[:8000]
            if text:
                summary = await self.phrase(
                    "Summarise this web page for the user in three sentences.",
                    f"Title: {response.data.get('title','')}\n\n{text}",
                    request,
                    slot=Slot.GENERAL,
                    max_tokens=320,
                )
                response.text = summary
                response.display = response.display or {}
        return response


class FilesCapability(ToolPlanCapability):
    name = "files"
    description = "Files and notes inside the JARVIS workspace."
    tools = ("list_files", "read_file", "write_file", "create_note", "search_files",
             "move_file", "delete_file", "workspace_info")
    default_tool = "list_files"


class ScreenCapability(ToolPlanCapability):
    name = "screen"
    description = "Looking at the screen and explaining what is on it."
    tools = ("analyse_screen", "capture_screen")
    default_tool = "analyse_screen"
    long_running = True

    async def handle(self, request: Request) -> Response:
        question = request.args.get("question") or request.text
        args: dict[str, Any] = {"question": question}
        if request.args.get("mode"):
            args["mode"] = request.args["mode"]
        result = await self.call_tool("analyse_screen", args, request.ctx)
        response = self.respond(result, request)
        if result.ok and len(response.text) > 400:
            response.spoken = await self.phrase(
                "Condense this screen description into two spoken sentences.",
                response.text, request, slot=Slot.FAST, max_tokens=120,
            )
        return response


class CalendarCapability(ToolPlanCapability):
    name = "calendar"
    description = "Calendar events: today, upcoming, searching and creating."
    tools = ("read_calendar", "search_calendar", "create_calendar_event")
    default_tool = "read_calendar"
    long_running = True

    async def plan(self, request: Request) -> dict[str, Any]:
        lowered = request.text.lower()
        # Deterministic shortcuts first — these are the common cases and they
        # don't deserve a model round-trip.
        if any(word in lowered for word in ("today", "this morning", "this afternoon")):
            return {"tool": "read_calendar", "args": {"range": "today"}}
        if "tomorrow" in lowered:
            return {"tool": "read_calendar", "args": {"range": "tomorrow"}}
        if "week" in lowered:
            return {"tool": "read_calendar", "args": {"range": "week"}}
        if any(word in lowered for word in ("add", "create", "schedule", "book", "put in")):
            return await self._plan_event(request)
        return await super().plan(request)

    async def _plan_event(self, request: Request) -> dict[str, Any]:
        import datetime as dt

        now = dt.datetime.now()
        prompt = (
            f"Today is {now:%A %d %B %Y}, the time is {now:%H:%M}. "
            "Extract a calendar event from the request.\n"
            f'Request: "{request.text}"\n\n'
            'Reply with JSON only: {"title": "...", "start": "YYYY-MM-DDTHH:MM", '
            '"end": "YYYY-MM-DDTHH:MM", "location": ""}'
        )
        from ..models.base import ChatMessage

        try:
            data = await self.models.complete_json(
                Slot.GENERAL,
                [ChatMessage("system", "You extract structured events. JSON only."),
                 ChatMessage("user", prompt)],
                max_tokens=160,
            )
        except Exception:
            data = None
        if not data or not data.get("title") or not data.get("start"):
            return {"tool": "read_calendar", "args": {"range": "today"}}
        return {"tool": "create_calendar_event", "args": data}
