"""Messages driver (Messages.app for sending, chat.db for reading).

Unlike Mail/Calendar/Reminders/Contacts, Messages.app's AppleScript
dictionary has no way to read message history — it can only send. Every
practical Messages-reading tool (this one included) instead queries the
same local SQLite database Messages.app itself reads and writes,
``~/Library/Messages/chat.db``, directly and **read-only**. That is why
this driver needs a different macOS permission than every other one in
this package: Full Disk Access, not Automation — the database lives under
TCC protection and a plain AppleScript "Automation" grant does not cover
it.

Sending stays on the AppleScript path, exactly like every other driver
here: JARVIS never writes to the database directly, which would be
unsupported and fragile. Only ``send()`` can change anything a user sees.
"""

from __future__ import annotations

import abc
import asyncio
import datetime as dt
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ...core.errors import CapabilityUnavailable, ToolError

#: Messages stores timestamps as an offset from this epoch (Apple's "Mac
#: Absolute Time" reference date), not Unix time.
_MAC_EPOCH = dt.datetime(2001, 1, 1, tzinfo=dt.timezone.utc)


@dataclass(slots=True)
class Message:
    id: str = ""
    sender: str = ""
    text: str = ""
    date: str = ""
    is_from_me: bool = False
    chat: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"id": self.id, "sender": self.sender, "text": self.text, "date": self.date,
                "is_from_me": self.is_from_me, "chat": self.chat}


class MessagesBackend(abc.ABC):
    @abc.abstractmethod
    async def available(self) -> tuple[bool, str]: ...

    @abc.abstractmethod
    async def recent(self, limit: int = 10) -> list[Message]: ...

    @abc.abstractmethod
    async def search(self, query: str, limit: int = 10) -> list[Message]: ...

    @abc.abstractmethod
    async def send(self, recipient: str, body: str) -> bool: ...


class AppleMessagesBackend(MessagesBackend):
    name = "Messages"

    def __init__(self, controller, db_path: Path | None = None):
        self._c = controller
        self._db_path = db_path or (Path.home() / "Library" / "Messages" / "chat.db")

    async def available(self) -> tuple[bool, str]:
        if not self._c.is_macos:
            return False, "Messages is only available on macOS"
        result = await self._c.osascript('tell application "Messages" to return name',
                                         timeout=20.0)
        return result.ok, result.output[:200] or "ok"

    # -- reading: chat.db, read-only, off the event loop ---------------------
    def _query(self, sql: str, params: tuple = ()) -> list[tuple]:
        try:
            uri = f"file:{self._db_path}?mode=ro"
            with sqlite3.connect(uri, uri=True, timeout=5.0) as conn:
                return conn.execute(sql, params).fetchall()
        except (sqlite3.OperationalError, OSError) as exc:
            # Covers both an outright denied open() (PermissionError, an
            # OSError subclass) and sqlite3's own "unable to open database
            # file" wrapping of the same underlying TCC denial — which one
            # actually surfaces isn't guaranteed, so both are treated the
            # same honest way rather than one crashing past the message.
            raise CapabilityUnavailable(
                "Reading Messages isn't permitted yet. Allow it in System Settings → "
                "Privacy & Security → Full Disk Access — this is a different permission "
                "from the one that lets JARVIS send a message.",
                detail=str(exc),
            ) from exc

    async def recent(self, limit: int = 10) -> list[Message]:
        rows = await asyncio.to_thread(self._query, _SELECT + " ORDER BY message.date DESC LIMIT ?",
                                       (limit,))
        return [_row_to_message(row) for row in rows]

    async def search(self, query: str, limit: int = 10) -> list[Message]:
        rows = await asyncio.to_thread(
            self._query,
            _SELECT + " AND message.text LIKE ? ESCAPE '\\' ORDER BY message.date DESC LIMIT ?",
            (f"%{_escape_like(query)}%", limit),
        )
        return [_row_to_message(row) for row in rows]

    # -- sending: Messages.app itself, never the database ---------------------
    async def send(self, recipient: str, body: str) -> bool:
        script = f"""
        tell application "Messages"
            set targetBuddy to missing value
            try
                set targetService to 1st service whose service type = iMessage
                set targetBuddy to buddy "{_esc(recipient)}" of targetService
            on error
                set targetService to 1st service whose service type = SMS
                set targetBuddy to buddy "{_esc(recipient)}" of targetService
            end try
            send "{_esc(body)}" to targetBuddy
        end tell
        return "ok"
        """
        result = await self._c.osascript(script, timeout=30.0)
        if not result.ok:
            detail = result.output.lower()
            if "not authorized" in detail or "not allowed" in detail or "-1743" in detail:
                raise CapabilityUnavailable(
                    "Messages access isn't permitted yet. Allow it in System Settings → "
                    "Privacy & Security → Automation.",
                    detail=result.output,
                )
            raise ToolError(f"Messages couldn't reach {recipient}.", detail=result.output[:300])
        return True


_SELECT = """
SELECT message.ROWID, message.text, message.date, message.is_from_me,
       handle.id, COALESCE(chat.display_name, chat.chat_identifier)
FROM message
LEFT JOIN handle ON message.handle_id = handle.ROWID
LEFT JOIN chat_message_join ON message.ROWID = chat_message_join.message_id
LEFT JOIN chat ON chat_message_join.chat_id = chat.ROWID
WHERE message.text IS NOT NULL AND message.text != ''
"""


def _esc(text: str) -> str:
    return (text or "").replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


def _escape_like(text: str) -> str:
    """Reminders/Calendar filter matches in Python, so a plain substring
    check is enough there; search() filters in SQL instead (chat.db can be
    far larger than a reminders list), which means "%" and "_" in the
    user's own search phrase would otherwise be read as SQL LIKE wildcards
    rather than literal characters — "100% done" silently becoming a much
    broader, wrong match instead of the phrase actually typed."""
    return (text or "").replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _row_to_message(row: tuple) -> Message:
    rowid, text, raw_date, is_from_me, sender, chat = row
    return Message(
        id=str(rowid), text=text or "", date=_mac_time_to_iso(raw_date),
        is_from_me=bool(is_from_me), sender="me" if is_from_me else (sender or ""),
        chat=chat or "",
    )


def _mac_time_to_iso(raw: int | None) -> str:
    """Newer macOS stores this column in nanoseconds since the Mac epoch;
    older databases stored plain seconds. A nanosecond value is many orders
    of magnitude larger for any realistic date, so the magnitude alone tells
    the two apart reliably."""
    if not raw:
        return ""
    seconds = raw / 1_000_000_000 if raw > 10**11 else raw
    try:
        return (_MAC_EPOCH + dt.timedelta(seconds=seconds)).isoformat()
    except (OverflowError, OSError, ValueError):
        return ""
