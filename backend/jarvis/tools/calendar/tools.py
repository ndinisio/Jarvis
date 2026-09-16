"""Calendar tools."""

from __future__ import annotations

import datetime as dt
from typing import Any

from ...security.permissions import RiskLevel
from ..base import Tool, ToolContext, ToolResult, ToolSpec
from .calendar_app import AppleCalendarBackend, CalendarEvent


class _CalendarTool(Tool):
    def __init__(self, deps):
        self._deps = deps
        self.backend = AppleCalendarBackend(deps.controller)


class ReadCalendarTool(_CalendarTool):
    spec = ToolSpec(
        name="read_calendar",
        description="Read calendar events for today, tomorrow or the next N days",
        parameters={
            "type": "object",
            "properties": {
                "range": {"type": "string", "enum": ["today", "tomorrow", "week", "custom"],
                          "default": "today"},
                "days": {"type": "integer", "default": 1},
            },
        },
        risk=RiskLevel.LOW,
        category="calendar",
        requires_macos=True,
        expected_ms=4000,
        examples=["What's on today?", "What does my week look like?"],
    )

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        window = args.get("range", "today")
        today = dt.datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        if window == "today":
            start, end, label = today, today + dt.timedelta(days=1), "today"
        elif window == "tomorrow":
            start, end, label = (today + dt.timedelta(days=1), today + dt.timedelta(days=2),
                                 "tomorrow")
        elif window == "week":
            start, end, label = today, today + dt.timedelta(days=7), "this week"
        else:
            days = max(1, int(args.get("days", 1)))
            start, end, label = today, today + dt.timedelta(days=days), f"the next {days} days"

        ctx.report(f"Reading your calendar for {label}…", tool="read_calendar")
        events = await self.backend.events_between(start, end)
        if not events:
            return ToolResult(
                data={"events": [], "range": label},
                summary=f"Nothing in the calendar {label}.",
                display={"kind": "calendar", "title": f"Calendar — {label}", "events": []},
            )
        return ToolResult(
            data={"events": [e.as_dict() for e in events], "range": label},
            summary=_spoken_summary(events, label),
            display={"kind": "calendar", "title": f"Calendar — {label}",
                     "events": [e.as_dict() for e in events]},
        )


class SearchCalendarTool(_CalendarTool):
    spec = ToolSpec(
        name="search_calendar",
        description="Find calendar events matching a phrase in the coming weeks",
        parameters={
            "type": "object",
            "properties": {"query": {"type": "string"}, "days": {"type": "integer", "default": 30}},
            "required": ["query"],
        },
        risk=RiskLevel.LOW,
        category="calendar",
        requires_macos=True,
        expected_ms=6000,
    )

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        today = dt.datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        days = max(1, int(args.get("days", 30)))
        events = await self.backend.events_between(today, today + dt.timedelta(days=days), limit=200)
        needle = args["query"].lower()
        hits = [e for e in events if needle in e.title.lower() or needle in e.location.lower()]
        if not hits:
            return ToolResult(data={"events": []},
                              summary=f"Nothing matching “{args['query']}” in the next {days} days.")
        return ToolResult(
            data={"events": [e.as_dict() for e in hits]},
            summary=f"{len(hits)} matching event{'s' if len(hits) != 1 else ''}. "
                    f"The next is {hits[0].title} on "
                    f"{(hits[0].start_dt or today).strftime('%A')}.",
            display={"kind": "calendar", "title": f"Events: {args['query']}",
                     "events": [e.as_dict() for e in hits]},
        )


class CreateEventTool(_CalendarTool):
    spec = ToolSpec(
        name="create_calendar_event",
        description="Create a calendar event (requires confirmation)",
        parameters={
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "start": {"type": "string", "description": "ISO 8601 start time"},
                "end": {"type": "string", "default": ""},
                "location": {"type": "string", "default": ""},
                "calendar": {"type": "string", "default": ""},
            },
            "required": ["title", "start"],
        },
        risk=RiskLevel.MEDIUM,
        category="calendar",
        requires_macos=True,
        expected_ms=3000,
    )

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        event = CalendarEvent(
            title=args["title"], start=args["start"], end=args.get("end", ""),
            location=args.get("location", ""),
        )
        await self.backend.create_event(event, args.get("calendar", ""))
        from .calendar_app import _parse_iso

        moment = _parse_iso(event.start)
        when = moment.strftime("%A at %-I:%M %p").replace(":00", "") if moment else event.start
        return ToolResult(
            data=event.as_dict(),
            summary=f"Added “{event.title}” on {when}.",
            display={"kind": "calendar", "title": "Event created", "events": [event.as_dict()]},
        )


def _spoken_summary(events: list[CalendarEvent], label: str) -> str:
    count = len(events)
    first = events[0]
    if count == 1:
        return f"One thing {label}: {first.title} at {first.spoken_time()}."
    second = events[1]
    return (
        f"{count} events {label}. First is {first.title} at {first.spoken_time()}, "
        f"then {second.title} at {second.spoken_time()}."
    )


def calendar_tools(deps) -> list[Tool]:
    return [ReadCalendarTool(deps), SearchCalendarTool(deps), CreateEventTool(deps)]
