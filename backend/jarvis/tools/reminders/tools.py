"""Reminders tools."""

from __future__ import annotations

from typing import Any

from ...security.permissions import RiskLevel
from ..base import Tool, ToolContext, ToolResult, ToolSpec
from .reminders_app import AppleRemindersBackend, Reminder


class _RemindersTool(Tool):
    def __init__(self, deps):
        self._deps = deps
        self.backend = AppleRemindersBackend(deps.controller)


class ListRemindersTool(_RemindersTool):
    spec = ToolSpec(
        name="list_reminders",
        description="List reminders, optionally from a named list, optionally including completed",
        parameters={
            "type": "object",
            "properties": {
                "list": {"type": "string", "default": ""},
                "include_completed": {"type": "boolean", "default": False},
            },
        },
        risk=RiskLevel.LOW,
        category="reminders",
        requires_macos=True,
        expected_ms=3000,
        examples=["What are my reminders?", "What's on my grocery list?"],
    )

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        list_name = args.get("list", "")
        ctx.report("Reading your reminders…", tool="list_reminders")
        reminders = await self.backend.list_reminders(
            list_name, include_completed=bool(args.get("include_completed"))
        )
        label = f" in {list_name}" if list_name else ""
        if not reminders:
            return ToolResult(
                data={"reminders": []},
                summary=f"Nothing in your reminders{label}.",
                display={"kind": "reminders", "title": f"Reminders{label}", "reminders": []},
            )
        return ToolResult(
            data={"reminders": [r.as_dict() for r in reminders]},
            summary=_spoken_summary(reminders, label),
            display={"kind": "reminders", "title": f"Reminders{label}",
                     "reminders": [r.as_dict() for r in reminders]},
        )


class SearchRemindersTool(_RemindersTool):
    spec = ToolSpec(
        name="search_reminders",
        description="Find reminders matching a phrase",
        parameters={
            "type": "object",
            "properties": {"query": {"type": "string"}, "list": {"type": "string", "default": ""}},
            "required": ["query"],
        },
        risk=RiskLevel.LOW,
        category="reminders",
        requires_macos=True,
        expected_ms=3000,
    )

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        reminders = await self.backend.list_reminders(args.get("list", ""),
                                                       include_completed=True, limit=200)
        needle = args["query"].lower()
        hits = [r for r in reminders if needle in r.title.lower() or needle in r.notes.lower()]
        if not hits:
            return ToolResult(data={"reminders": []},
                              summary=f"Nothing matching “{args['query']}” in your reminders.")
        return ToolResult(
            data={"reminders": [r.as_dict() for r in hits]},
            summary=f"{len(hits)} matching reminder{'s' if len(hits) != 1 else ''}. "
                    f"The first is {hits[0].title}.",
            display={"kind": "reminders", "title": f"Reminders: {args['query']}",
                     "reminders": [r.as_dict() for r in hits]},
        )


class CreateReminderTool(_RemindersTool):
    spec = ToolSpec(
        name="create_reminder",
        description="Create a reminder, optionally with a due date and a named list "
                    "(requires confirmation)",
        parameters={
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "due": {"type": "string", "default": "", "description": "ISO 8601 due time"},
                "notes": {"type": "string", "default": ""},
                "list": {"type": "string", "default": ""},
            },
            "required": ["title"],
        },
        risk=RiskLevel.MEDIUM,
        category="reminders",
        requires_macos=True,
        expected_ms=3000,
    )

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        reminder = Reminder(title=args["title"], due=args.get("due", ""),
                            notes=args.get("notes", ""))
        await self.backend.create_reminder(reminder, args.get("list", ""))
        when = f" for {reminder.due_dt.strftime('%A at %-I:%M %p').replace(':00', '')}" \
            if reminder.due_dt else ""
        return ToolResult(
            data=reminder.as_dict(),
            summary=f"Added “{reminder.title}”{when}.",
            display={"kind": "reminders", "title": "Reminder created",
                     "reminders": [reminder.as_dict()]},
        )


class CompleteReminderTool(_RemindersTool):
    spec = ToolSpec(
        name="complete_reminder",
        description="Mark a reminder as done, by its title (requires confirmation)",
        parameters={
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "list": {"type": "string", "default": ""},
            },
            "required": ["title"],
        },
        risk=RiskLevel.MEDIUM,
        category="reminders",
        requires_macos=True,
        expected_ms=3000,
    )

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        found = await self.backend.complete_reminder(args["title"], args.get("list", ""))
        if not found:
            return ToolResult.failure(f"I couldn't find a reminder called “{args['title']}”.")
        return ToolResult(summary=f"Marked “{args['title']}” as done.")


def _spoken_summary(reminders: list[Reminder], label: str) -> str:
    count = len(reminders)
    first = reminders[0]
    if count == 1:
        return f"One reminder{label}: {first.title}."
    return f"{count} reminders{label}. First is {first.title}."


def reminders_tools(deps) -> list[Tool]:
    return [ListRemindersTool(deps), SearchRemindersTool(deps), CreateReminderTool(deps),
            CompleteReminderTool(deps)]
