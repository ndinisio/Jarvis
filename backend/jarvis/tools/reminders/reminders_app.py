"""Reminders driver (Reminders.app via AppleScript).

Same shape as ``tools/calendar/calendar_app.py``: a structured backend
interface, one AppleScript-driven implementation, FS/RS-delimited records
parsed into dataclasses. Date parsing is genuinely generic (an AppleScript
date string looks the same whichever app produced it), so it's reused from
the calendar driver rather than duplicated.
"""

from __future__ import annotations

import abc
import datetime as dt
from dataclasses import dataclass
from typing import Any

from ...core.errors import CapabilityUnavailable, ToolError
from ..calendar.calendar_app import _parse_applescript_date, _parse_iso

FS = "\x1f"
RS = "\x1e"


@dataclass(slots=True)
class Reminder:
    title: str = ""
    due: str = ""
    notes: str = ""
    list_name: str = ""
    completed: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "title": self.title, "due": self.due, "notes": self.notes,
            "list": self.list_name, "completed": self.completed,
        }

    @property
    def due_dt(self) -> dt.datetime | None:
        return _parse_applescript_date(self.due)


class RemindersBackend(abc.ABC):
    @abc.abstractmethod
    async def available(self) -> tuple[bool, str]: ...

    @abc.abstractmethod
    async def list_reminders(self, list_name: str = "", include_completed: bool = False,
                             limit: int = 25) -> list[Reminder]: ...

    @abc.abstractmethod
    async def create_reminder(self, reminder: Reminder, list_name: str = "") -> bool: ...

    @abc.abstractmethod
    async def complete_reminder(self, title: str, list_name: str = "") -> bool: ...


class AppleRemindersBackend(RemindersBackend):
    name = "Reminders"

    def __init__(self, controller):
        self._c = controller

    async def available(self) -> tuple[bool, str]:
        if not self._c.is_macos:
            return False, "Reminders is only available on macOS"
        result = await self._c.osascript('tell application "Reminders" to return name',
                                         timeout=20.0)
        return result.ok, result.output[:200] or "ok"

    async def _script(self, script: str, timeout: float = 60.0) -> str:
        result = await self._c.osascript(script, timeout=timeout)
        if not result.ok:
            detail = result.output.lower()
            if "not authorized" in detail or "not allowed" in detail or "-1743" in detail:
                raise CapabilityUnavailable(
                    "Reminders access isn't permitted yet. Allow it in System Settings → "
                    "Privacy & Security → Reminders.",
                    detail=result.output,
                )
            raise ToolError("Reminders didn't respond to the automation request.",
                            detail=result.output[:300])
        return result.stdout

    async def list_reminders(self, list_name: str = "", include_completed: bool = False,
                             limit: int = 25) -> list[Reminder]:
        scope = (f'{{list "{_esc(list_name)}"}}' if list_name else "lists")
        completed_check = "" if include_completed else "if not (completed of rem) then"
        end_check = "" if include_completed else "end if"
        script = f"""
        set fs to (ASCII character 31)
        set rs to (ASCII character 30)
        set output to ""
        tell application "Reminders"
            set targetLists to {scope}
            repeat with lst in targetLists
                try
                    repeat with rem in reminders of lst
                        {completed_check}
                        set dueStr to ""
                        try
                            set dueStr to (due date of rem) as string
                        end try
                        set theNotes to ""
                        try
                            set theNotes to (body of rem)
                        end try
                        set output to output & (name of rem) & fs & dueStr & fs & theNotes & ¬
                            fs & (name of lst) & fs & (completed of rem as string) & rs
                        {end_check}
                    end repeat
                end try
            end repeat
        end tell
        return output
        """
        reminders = _parse_reminders(await self._script(script))
        reminders.sort(key=lambda r: r.due_dt or dt.datetime.max)
        return reminders[:limit]

    async def create_reminder(self, reminder: Reminder, list_name: str = "") -> bool:
        due = _parse_iso(reminder.due) if reminder.due else None
        due_clause = ""
        due_props = ""
        if due:
            due_clause = f"""
        set dueDate to (current date)
        set year of dueDate to {due.year}
        set month of dueDate to {due.month}
        set day of dueDate to {due.day}
        set time of dueDate to {due.hour * 3600 + due.minute * 60}
        """
            due_props = ", due date:dueDate"
        target = list_name or ""
        script = f"""
        {due_clause}
        tell application "Reminders"
            set targetList to missing value
            repeat with lst in lists
                if (name of lst) is "{_esc(target)}" then set targetList to lst
            end repeat
            if targetList is missing value then set targetList to list 1
            tell targetList
                make new reminder with properties {{name:"{_esc(reminder.title)}", ¬
                    body:"{_esc(reminder.notes)}"{due_props}}}
            end tell
        end tell
        return "ok"
        """
        await self._script(script)
        return True

    async def complete_reminder(self, title: str, list_name: str = "") -> bool:
        scope = (f'{{list "{_esc(list_name)}"}}' if list_name else "lists")
        script = f"""
        tell application "Reminders"
            set targetLists to {scope}
            repeat with lst in targetLists
                try
                    repeat with rem in reminders of lst
                        if (name of rem) is "{_esc(title)}" and not (completed of rem) then
                            set completed of rem to true
                            return "ok"
                        end if
                    end repeat
                end try
            end repeat
        end tell
        return "notfound"
        """
        return await self._script(script) == "ok"


def _esc(text: str) -> str:
    return (text or "").replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


def _parse_reminders(raw: str) -> list[Reminder]:
    reminders: list[Reminder] = []
    for record in raw.split(RS):
        record = record.strip("\n\r ")
        if not record:
            continue
        parts = record.split(FS)
        if len(parts) < 2:
            continue
        reminders.append(
            Reminder(
                title=parts[0].strip() or "(untitled)",
                due=parts[1].strip(),
                notes=parts[2].strip() if len(parts) > 2 else "",
                list_name=parts[3].strip() if len(parts) > 3 else "",
                completed=(parts[4].strip().lower() == "true") if len(parts) > 4 else False,
            )
        )
    return reminders
