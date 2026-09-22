"""Contacts tools. Read-only — JARVIS never creates or edits a contact."""

from __future__ import annotations

from typing import Any

from ...security.permissions import RiskLevel
from ..base import Tool, ToolContext, ToolResult, ToolSpec
from .contacts_app import AppleContactsBackend


class SearchContactsTool(Tool):
    spec = ToolSpec(
        name="search_contacts",
        description="Look up a contact by name — their email addresses, phone numbers "
                    "and company",
        parameters={
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
        risk=RiskLevel.LOW,
        category="contacts",
        requires_macos=True,
        expected_ms=2500,
        examples=["What's Tom's number?", "Do I have Ada's email?"],
        returns="matching contacts with their emails and phone numbers",
    )

    def __init__(self, deps):
        self._deps = deps
        self.backend = AppleContactsBackend(deps.controller)

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        query = args["query"]
        ctx.report(f"Looking up {query}…", tool="search_contacts")
        contacts = await self.backend.search(query)
        if not contacts:
            return ToolResult(data={"contacts": []},
                              summary=f"I don't have a contact called “{query}”.")
        if len(contacts) == 1:
            return ToolResult(
                data={"contacts": [contacts[0].as_dict()]},
                summary=_spoken_one(contacts[0]),
                display={"kind": "contacts", "title": contacts[0].name,
                         "contacts": [contacts[0].as_dict()]},
            )
        return ToolResult(
            data={"contacts": [c.as_dict() for c in contacts]},
            summary=f"{len(contacts)} contacts match “{query}”: "
                    f"{', '.join(c.name for c in contacts[:5])}.",
            display={"kind": "contacts", "title": f"Contacts: {query}",
                     "contacts": [c.as_dict() for c in contacts]},
        )


def _spoken_one(contact) -> str:
    bits = []
    if contact.emails:
        bits.append(contact.emails[0])
    if contact.phones:
        bits.append(contact.phones[0])
    detail = f" — {', '.join(bits)}" if bits else ""
    return f"{contact.name}{detail}."


def contacts_tools(deps) -> list[Tool]:
    return [SearchContactsTool(deps)]
