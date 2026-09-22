"""Contacts driver (Contacts.app via AppleScript).

Read-only, mirroring ``tools/calendar/calendar_app.py``'s shape. JARVIS never
creates or edits a contact — only looks one up, which is enough to answer
"what's Tom's number" and, via ``intelligence/state.py``'s context
absorption, to let a relationship word ("my brother") resolve to whoever was
actually found once a lookup has happened in the conversation.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Any

from ...core.errors import CapabilityUnavailable, ToolError

FS = "\x1f"
RS = "\x1e"


@dataclass(slots=True)
class Contact:
    name: str = ""
    emails: list[str] = field(default_factory=list)
    phones: list[str] = field(default_factory=list)
    company: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "emails": self.emails, "phones": self.phones,
                "company": self.company}


class ContactsBackend(abc.ABC):
    @abc.abstractmethod
    async def available(self) -> tuple[bool, str]: ...

    @abc.abstractmethod
    async def search(self, query: str, limit: int = 10) -> list[Contact]: ...


class AppleContactsBackend(ContactsBackend):
    name = "Contacts"

    def __init__(self, controller):
        self._c = controller

    async def available(self) -> tuple[bool, str]:
        if not self._c.is_macos:
            return False, "Contacts is only available on macOS"
        result = await self._c.osascript('tell application "Contacts" to return name',
                                         timeout=20.0)
        return result.ok, result.output[:200] or "ok"

    async def _script(self, script: str, timeout: float = 30.0) -> str:
        result = await self._c.osascript(script, timeout=timeout)
        if not result.ok:
            detail = result.output.lower()
            if "not authorized" in detail or "not allowed" in detail or "-1743" in detail:
                raise CapabilityUnavailable(
                    "Contacts access isn't permitted yet. Allow it in System Settings → "
                    "Privacy & Security → Contacts.",
                    detail=result.output,
                )
            raise ToolError("Contacts didn't respond to the automation request.",
                            detail=result.output[:300])
        return result.stdout

    async def search(self, query: str, limit: int = 10) -> list[Contact]:
        needle = _esc(query)
        script = f"""
        set fs to (ASCII character 31)
        set rs to (ASCII character 30)
        set output to ""
        tell application "Contacts"
            set matches to (every person whose name contains "{needle}")
            repeat with p in matches
                set emailList to ""
                repeat with e in emails of p
                    set emailList to emailList & (value of e) & ","
                end repeat
                set phoneList to ""
                repeat with ph in phones of p
                    set phoneList to phoneList & (value of ph) & ","
                end repeat
                set theCompany to ""
                try
                    set theCompany to (organization of p)
                end try
                set output to output & (name of p) & fs & emailList & fs & phoneList & ¬
                    fs & theCompany & rs
            end repeat
        end tell
        return output
        """
        contacts = _parse_contacts(await self._script(script))
        return contacts[:limit]


def _esc(text: str) -> str:
    return (text or "").replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


def _parse_contacts(raw: str) -> list[Contact]:
    contacts: list[Contact] = []
    for record in raw.split(RS):
        record = record.strip("\n\r ")
        if not record:
            continue
        parts = record.split(FS)
        if not parts or not parts[0].strip():
            continue
        emails = [e for e in (parts[1].strip() if len(parts) > 1 else "").split(",") if e]
        phones = [p for p in (parts[2].strip() if len(parts) > 2 else "").split(",") if p]
        contacts.append(Contact(
            name=parts[0].strip(),
            emails=emails,
            phones=phones,
            company=parts[3].strip() if len(parts) > 3 else "",
        ))
    return contacts
