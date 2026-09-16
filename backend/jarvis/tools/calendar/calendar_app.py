"""Calendar driver (Calendar.app via AppleScript).

"What's on today?" is answered by *querying the calendar*, not by asking a
language model to reason about dates. The backend returns structured events;
phrasing happens afterwards, deterministically where possible.
"""

from __future__ import annotations

import abc
import datetime as dt
import re
from dataclasses import dataclass
from typing import Any

from ...core.errors import CapabilityUnavailable, ToolError

FS = "\x1f"
RS = "\x1e"


@dataclass(slots=True)
class CalendarEvent:
    title: str = ""
    start: str = ""
    end: str = ""
    location: str = ""
    calendar: str = ""
    all_day: bool = False
    notes: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "title": self.title, "start": self.start, "end": self.end,
            "location": self.location, "calendar": self.calendar, "all_day": self.all_day,
        }

    @property
    def start_dt(self) -> dt.datetime | None:
        return _parse_applescript_date(self.start)

    def spoken_time(self) -> str:
        if self.all_day:
            return "all day"
        moment = self.start_dt
        if not moment:
            return self.start
        return moment.strftime("%-I:%M %p").lower().replace(":00", "")


class CalendarBackend(abc.ABC):
    @abc.abstractmethod
    async def available(self) -> tuple[bool, str]: ...

    @abc.abstractmethod
    async def events_between(self, start: dt.datetime, end: dt.datetime,
                             limit: int = 25) -> list[CalendarEvent]: ...

    @abc.abstractmethod
    async def create_event(self, event: CalendarEvent, calendar: str = "") -> bool: ...


class AppleCalendarBackend(CalendarBackend):
    name = "Calendar"

    def __init__(self, controller):
        self._c = controller

    async def available(self) -> tuple[bool, str]:
        if not self._c.is_macos:
            return False, "Calendar is only available on macOS"
        result = await self._c.osascript('tell application "Calendar" to return name', timeout=20.0)
        return result.ok, result.output[:200] or "ok"

    async def _script(self, script: str, timeout: float = 60.0) -> str:
        result = await self._c.osascript(script, timeout=timeout)
        if not result.ok:
            detail = result.output.lower()
            if "not authorized" in detail or "not allowed" in detail or "-1743" in detail:
                raise CapabilityUnavailable(
                    "Calendar access isn't permitted yet. Allow it in System Settings → "
                    "Privacy & Security → Calendars.",
                    detail=result.output,
                )
            raise ToolError("Calendar didn't respond to the automation request.",
                            detail=result.output[:300])
        return result.stdout

    async def events_between(self, start: dt.datetime, end: dt.datetime,
                             limit: int = 25) -> list[CalendarEvent]:
        script = f"""
        set fs to (ASCII character 31)
        set rs to (ASCII character 30)
        set startDate to (current date)
        set time of startDate to 0
        set startDate to startDate + ({_offset_days(start)}) * days
        set endDate to (current date)
        set time of endDate to 0
        set endDate to endDate + ({_offset_days(end)}) * days
        set output to ""
        tell application "Calendar"
            repeat with cal in calendars
                try
                    set matches to (every event of cal whose start date ≥ startDate and start date < endDate)
                    repeat with evt in matches
                        set theLocation to ""
                        try
                            set theLocation to location of evt
                        end try
                        set output to output & (summary of evt) & fs & (start date of evt as string) & ¬
                            fs & (end date of evt as string) & fs & theLocation & fs & (name of cal) & ¬
                            fs & (allday event of evt as string) & rs
                    end repeat
                end try
            end repeat
        end tell
        return output
        """
        events = _parse_events(await self._script(script))
        events.sort(key=lambda e: e.start_dt or dt.datetime.max)
        return events[:limit]

    async def create_event(self, event: CalendarEvent, calendar: str = "") -> bool:
        start = _parse_iso(event.start)
        end = _parse_iso(event.end) or (start + dt.timedelta(hours=1) if start else None)
        if not start or not end:
            raise ToolError("I need a valid start time for that event.")
        target = calendar or "Calendar"
        script = f"""
        set startDate to (current date)
        set year of startDate to {start.year}
        set month of startDate to {start.month}
        set day of startDate to {start.day}
        set time of startDate to {start.hour * 3600 + start.minute * 60}
        set endDate to (current date)
        set year of endDate to {end.year}
        set month of endDate to {end.month}
        set day of endDate to {end.day}
        set time of endDate to {end.hour * 3600 + end.minute * 60}
        tell application "Calendar"
            set targetCal to missing value
            repeat with cal in calendars
                if (name of cal) is "{target}" then set targetCal to cal
            end repeat
            if targetCal is missing value then set targetCal to item 1 of calendars
            tell targetCal
                make new event with properties {{summary:"{_esc(event.title)}", start date:startDate, end date:endDate, location:"{_esc(event.location)}"}}
            end tell
        end tell
        return "ok"
        """
        await self._script(script)
        return True


def _esc(text: str) -> str:
    return (text or "").replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


def _offset_days(moment: dt.datetime) -> int:
    today = dt.datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    return (moment.replace(hour=0, minute=0, second=0, microsecond=0) - today).days


def _parse_events(raw: str) -> list[CalendarEvent]:
    events: list[CalendarEvent] = []
    for record in raw.split(RS):
        record = record.strip("\n\r ")
        if not record:
            continue
        parts = record.split(FS)
        if len(parts) < 3:
            continue
        events.append(
            CalendarEvent(
                title=parts[0].strip() or "(untitled)",
                start=parts[1].strip(),
                end=parts[2].strip(),
                location=parts[3].strip() if len(parts) > 3 else "",
                calendar=parts[4].strip() if len(parts) > 4 else "",
                all_day=(parts[5].strip().lower() == "true") if len(parts) > 5 else False,
            )
        )
    return events


_APPLESCRIPT_DATE = re.compile(
    r"(\w+),?\s+(\d{1,2})\s+(\w+)\s+(\d{4})\s+at\s+(\d{1,2}):(\d{2}):(\d{2})\s*([AaPp][Mm])?"
)
_MONTHS = {m.lower(): i for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July", "August", "September",
     "October", "November", "December"], start=1)}


def _parse_applescript_date(value: str) -> dt.datetime | None:
    """AppleScript dates are locale-formatted; parse the common variants."""
    if not value:
        return None
    match = _APPLESCRIPT_DATE.search(value)
    if match:
        _, day, month_name, year, hour, minute, second, meridiem = match.groups()
        month = _MONTHS.get(month_name.lower()[:3] and _month_key(month_name), 0)
        if month:
            hour_i = int(hour)
            if meridiem:
                if meridiem.lower() == "pm" and hour_i != 12:
                    hour_i += 12
                elif meridiem.lower() == "am" and hour_i == 12:
                    hour_i = 0
            try:
                return dt.datetime(int(year), month, int(day), hour_i, int(minute), int(second))
            except ValueError:
                return None
    for fmt in ("%A, %d %B %Y at %H:%M:%S", "%d/%m/%Y, %H:%M:%S", "%m/%d/%Y, %I:%M:%S %p",
                "%Y-%m-%d %H:%M:%S"):
        try:
            return dt.datetime.strptime(value.strip(), fmt)
        except ValueError:
            continue
    return None


def _month_key(name: str) -> str:
    lowered = name.lower()
    for full in _MONTHS:
        if full.startswith(lowered[:3]):
            return full
    return lowered


def _parse_iso(value: str) -> dt.datetime | None:
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return _parse_applescript_date(value)
