"""Apple Mail driver.

Mail is addressed through AppleScript, which is the supported automation
surface on macOS and avoids touching the mail store directly. The driver is
deliberately behind an interface (:class:`MailBackend`) so an IMAP or
EventKit-style backend can be added without changing the tools above it.

Sending is *not* implemented as a fire-and-forget call: the driver can only
create a draft, and a separate, explicitly confirmed method sends it.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Any

from ...core.errors import CapabilityUnavailable, ToolError

#: ASCII unit/record separators keep AppleScript output parseable even when a
#: subject line contains commas, quotes or newlines.
FS = "\x1f"
RS = "\x1e"


@dataclass(slots=True)
class MailMessage:
    id: str = ""
    subject: str = ""
    sender: str = ""
    date: str = ""
    preview: str = ""
    mailbox: str = "INBOX"
    unread: bool = True
    body: str = ""
    urgency: str = ""
    reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "subject": self.subject, "sender": self.sender, "date": self.date,
            "preview": self.preview, "mailbox": self.mailbox, "unread": self.unread,
            "urgency": self.urgency, "reason": self.reason,
        }


@dataclass(slots=True)
class Draft:
    to: list[str] = field(default_factory=list)
    subject: str = ""
    body: str = ""
    cc: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {"to": self.to, "cc": self.cc, "subject": self.subject, "body": self.body}


class MailBackend(abc.ABC):
    name = "mail"

    @abc.abstractmethod
    async def available(self) -> tuple[bool, str]: ...

    @abc.abstractmethod
    async def unread_count(self) -> int: ...

    @abc.abstractmethod
    async def recent(self, limit: int = 8, unread_only: bool = True) -> list[MailMessage]: ...

    @abc.abstractmethod
    async def search(self, query: str, limit: int = 10) -> list[MailMessage]: ...

    @abc.abstractmethod
    async def body(self, message_id: str) -> str: ...

    @abc.abstractmethod
    async def create_draft(self, draft: Draft) -> bool: ...

    @abc.abstractmethod
    async def send(self, draft: Draft) -> bool: ...


class AppleMailBackend(MailBackend):
    name = "Apple Mail"

    def __init__(self, controller):
        self._c = controller

    async def available(self) -> tuple[bool, str]:
        if not self._c.is_macos:
            return False, "Apple Mail is only available on macOS"
        result = await self._c.osascript('tell application "System Events" to return '
                                         '(exists application process "Mail")', timeout=10.0)
        if not result.ok:
            return False, "automation permission for Mail appears to be missing"
        return True, "ok"

    async def _script(self, script: str, timeout: float = 45.0) -> str:
        result = await self._c.osascript(script, timeout=timeout)
        if not result.ok:
            detail = result.output.lower()
            if "not authorized" in detail or "not allowed" in detail or "-1743" in detail:
                raise CapabilityUnavailable(
                    "Mail automation isn't permitted yet. Allow JARVIS to control Mail in "
                    "System Settings → Privacy & Security → Automation.",
                    detail=result.output,
                )
            raise ToolError("Mail didn't respond to the automation request.",
                            detail=result.output[:300])
        return result.stdout

    async def unread_count(self) -> int:
        out = await self._script(
            'tell application "Mail" to return (count of (messages of inbox whose read status '
            "is false))",
            timeout=30.0,
        )
        try:
            return int(out.strip())
        except ValueError:
            return 0

    async def recent(self, limit: int = 8, unread_only: bool = True) -> list[MailMessage]:
        filter_clause = "whose read status is false" if unread_only else ""
        script = f"""
        set fs to (ASCII character 31)
        set rs to (ASCII character 30)
        set output to ""
        tell application "Mail"
            set msgs to (messages of inbox {filter_clause})
            set total to count of msgs
            if total > {limit} then set total to {limit}
            repeat with i from 1 to total
                set m to item i of msgs
                try
                    set theSender to sender of m
                on error
                    set theSender to "unknown"
                end try
                try
                    set theSubject to subject of m
                on error
                    set theSubject to "(no subject)"
                end try
                try
                    set theBody to content of m
                    if (length of theBody) > 400 then set theBody to text 1 thru 400 of theBody
                on error
                    set theBody to ""
                end try
                set output to output & (id of m as string) & fs & theSubject & fs & theSender & ¬
                    fs & (date received of m as string) & fs & theBody & fs & ¬
                    (read status of m as string) & rs
            end repeat
        end tell
        return output
        """
        return _parse_messages(await self._script(script))

    async def search(self, query: str, limit: int = 10) -> list[MailMessage]:
        safe = query.replace('"', '\\"')
        script = f"""
        set fs to (ASCII character 31)
        set rs to (ASCII character 30)
        set output to ""
        tell application "Mail"
            set matches to (messages of inbox whose subject contains "{safe}")
            set total to count of matches
            if total > {limit} then set total to {limit}
            repeat with i from 1 to total
                set m to item i of matches
                set output to output & (id of m as string) & fs & (subject of m) & fs & ¬
                    (sender of m) & fs & (date received of m as string) & fs & "" & fs & ¬
                    (read status of m as string) & rs
            end repeat
        end tell
        return output
        """
        return _parse_messages(await self._script(script))

    async def body(self, message_id: str) -> str:
        script = f"""
        tell application "Mail"
            set msgs to (messages of inbox whose id is {int(message_id)})
            if (count of msgs) is 0 then return ""
            return content of item 1 of msgs
        end tell
        """
        return (await self._script(script)).strip()

    async def create_draft(self, draft: Draft) -> bool:
        await self._script(_draft_script(draft, send=False))
        return True

    async def send(self, draft: Draft) -> bool:
        await self._script(_draft_script(draft, send=True))
        return True


def _draft_script(draft: Draft, send: bool) -> str:
    def esc(text: str) -> str:
        return text.replace("\\", "\\\\").replace('"', '\\"')

    recipients = "\n".join(
        f'                make new to recipient at end of to recipients with properties '
        f'{{address:"{esc(address)}"}}'
        for address in draft.to
    )
    ccs = "\n".join(
        f'                make new cc recipient at end of cc recipients with properties '
        f'{{address:"{esc(address)}"}}'
        for address in draft.cc
    )
    body = esc(draft.body).replace("\n", "\\n")
    action = "send newMessage" if send else "save newMessage"
    return f"""
    tell application "Mail"
        set newMessage to make new outgoing message with properties {{subject:"{esc(draft.subject)}", content:"{body}", visible:true}}
        tell newMessage
{recipients}
{ccs}
        end tell
        {action}
    end tell
    return "ok"
    """


def _parse_messages(raw: str) -> list[MailMessage]:
    messages: list[MailMessage] = []
    for record in raw.split(RS):
        record = record.strip("\n\r ")
        if not record:
            continue
        parts = record.split(FS)
        if len(parts) < 4:
            continue
        preview = parts[4].strip() if len(parts) > 4 else ""
        unread = parts[5].strip().lower() == "false" if len(parts) > 5 else True
        messages.append(
            MailMessage(
                id=parts[0].strip(),
                subject=parts[1].strip() or "(no subject)",
                sender=parts[2].strip(),
                date=parts[3].strip(),
                preview=" ".join(preview.split())[:300],
                unread=unread,
            )
        )
    return messages


def person_name(sender: str) -> str:
    """"Ada Lovelace <ada@example.com>" → "Ada Lovelace"."""
    sender = (sender or "").strip()
    if "<" in sender:
        return sender.split("<")[0].strip().strip('"') or sender
    return sender.split("@")[0]


URGENT_HINTS = (
    "urgent", "asap", "immediately", "deadline", "today", "overdue", "invoice", "payment",
    "security", "verify", "action required", "reminder", "expires", "final notice",
)


def triage(messages: list[MailMessage]) -> list[MailMessage]:
    """Cheap, deterministic urgency scoring — no model required."""
    for message in messages:
        haystack = f"{message.subject} {message.preview}".lower()
        hits = [hint for hint in URGENT_HINTS if hint in haystack]
        if hits:
            message.urgency = "high" if len(hits) > 1 else "medium"
            message.reason = ", ".join(hits[:3])
        else:
            message.urgency = "normal"
    return messages
