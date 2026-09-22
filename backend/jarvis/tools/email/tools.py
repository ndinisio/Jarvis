"""Email tools.

Reading is low risk. *Sending is not.* A draft is always created and shown
first; sending is a separate HIGH-risk tool that cannot execute without an
explicit confirmation from the user — no model output can bypass that.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from ...security.permissions import RiskLevel
from ..base import Tool, ToolContext, ToolResult, ToolSpec
from .imap_backend import ImapMailBackend
from .mail_app import AppleMailBackend, Draft, person_name, triage


class _MailTool(Tool):
    def __init__(self, deps):
        self._deps = deps
        if deps.config.email.provider == "imap":
            self.backend = ImapMailBackend(deps.config.email)
            # Unlike Mail.app, an IMAP/SMTP server has nothing to do with
            # macOS — this must not stay True or every email tool would be
            # refused outright on any other platform (tools/registry.py:
            # ToolRegistry.call() rejects a requires_macos tool on sight).
            self.spec = replace(self.spec, requires_macos=False)
        else:
            self.backend = AppleMailBackend(deps.controller)


class CheckEmailTool(_MailTool):
    spec = ToolSpec(
        name="check_email",
        description="Check for new mail and summarise what has arrived",
        parameters={
            "type": "object",
            "properties": {"limit": {"type": "integer", "default": 8}},
        },
        risk=RiskLevel.LOW,
        category="email",
        requires_macos=True,
        expected_ms=4000,
        examples=["Check my emails", "Any new mail?"],
    )

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        ctx.report("Checking your mail…", tool="check_email")
        count = await self.backend.unread_count()
        if count == 0:
            return ToolResult(data={"unread": 0, "messages": []},
                              summary="No new mail.")
        ctx.report(f"Reading {min(count, int(args.get('limit', 8)))} messages…", tool="check_email")
        messages = triage(await self.backend.recent(int(args.get("limit", 8)), unread_only=True))
        important = [m for m in messages if m.urgency in {"high", "medium"}]
        summary = f"You have {count} new message{'s' if count != 1 else ''}."
        if important:
            names = ", ".join(person_name(m.sender) for m in important[:2])
            summary += f" {len(important)} look important — from {names}."
        return ToolResult(
            data={"unread": count, "messages": [m.as_dict() for m in messages]},
            summary=summary,
            display={
                "kind": "email",
                "title": f"{count} unread",
                "messages": [m.as_dict() for m in messages],
            },
        )


class ReadEmailTool(_MailTool):
    spec = ToolSpec(
        name="read_email",
        description="Read the full body of a specific message",
        parameters={
            "type": "object",
            "properties": {"id": {"type": "string"}},
            "required": ["id"],
        },
        risk=RiskLevel.LOW,
        category="email",
        requires_macos=True,
        expected_ms=2500,
    )

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        body = await self.backend.body(args["id"])
        if not body:
            return ToolResult.failure("I couldn't find that message.")
        return ToolResult(
            data={"id": args["id"], "body": body[:20000]},
            summary=f"Read the message — {len(body)} characters.",
            display={"kind": "text", "title": "Message", "text": body[:6000]},
        )


class SearchEmailTool(_MailTool):
    spec = ToolSpec(
        name="search_email",
        description="Search the inbox by subject",
        parameters={
            "type": "object",
            "properties": {"query": {"type": "string"}, "limit": {"type": "integer", "default": 10}},
            "required": ["query"],
        },
        risk=RiskLevel.LOW,
        category="email",
        requires_macos=True,
        expected_ms=5000,
    )

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        ctx.report(f"Searching mail for “{args['query']}”…", tool="search_email")
        messages = await self.backend.search(args["query"], int(args.get("limit", 10)))
        if not messages:
            return ToolResult(data={"messages": []},
                              summary=f"Nothing matching “{args['query']}”.")
        return ToolResult(
            data={"messages": [m.as_dict() for m in messages]},
            summary=f"{len(messages)} message{'s' if len(messages) != 1 else ''} match.",
            display={"kind": "email", "title": f"Search: {args['query']}",
                     "messages": [m.as_dict() for m in messages]},
        )


class DraftEmailTool(_MailTool):
    spec = ToolSpec(
        name="draft_email",
        description="Compose a message and save it as a draft in Mail (never sends)",
        parameters={
            "type": "object",
            "properties": {
                "to": {"type": "array"},
                "subject": {"type": "string"},
                "body": {"type": "string"},
                "cc": {"type": "array", "default": []},
            },
            "required": ["to", "subject", "body"],
        },
        risk=RiskLevel.MEDIUM,
        category="email",
        requires_macos=True,
        expected_ms=2000,
    )

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        draft = Draft(
            to=_addresses(args["to"]), subject=args["subject"], body=args["body"],
            cc=_addresses(args.get("cc") or []),
        )
        await self.backend.create_draft(draft)
        return ToolResult(
            data=draft.as_dict(),
            summary=f"Draft to {', '.join(draft.to)} is ready in Mail. "
                    "Say “send it” if you're happy with it.",
            display={"kind": "draft", "title": "Draft", **draft.as_dict()},
        )


class SendEmailTool(_MailTool):
    spec = ToolSpec(
        name="send_email",
        description="Send an email. Always requires explicit confirmation",
        parameters={
            "type": "object",
            "properties": {
                "to": {"type": "array"},
                "subject": {"type": "string"},
                "body": {"type": "string"},
                "cc": {"type": "array", "default": []},
            },
            "required": ["to", "subject", "body"],
        },
        risk=RiskLevel.HIGH,
        category="email",
        requires_macos=True,
        expected_ms=2500,
    )

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        # The registry has already obtained confirmation for this HIGH-risk tool.
        draft = Draft(
            to=_addresses(args["to"]), subject=args["subject"], body=args["body"],
            cc=_addresses(args.get("cc") or []),
        )
        await self.backend.send(draft)
        return ToolResult(
            data=draft.as_dict(),
            summary=f"Sent to {', '.join(draft.to)}.",
            display={"kind": "draft", "title": "Sent", **draft.as_dict()},
        )


def _addresses(value: Any) -> list[str]:
    if isinstance(value, str):
        return [part.strip() for part in value.replace(";", ",").split(",") if part.strip()]
    return [str(v).strip() for v in (value or []) if str(v).strip()]


def email_tools(deps) -> list[Tool]:
    return [
        CheckEmailTool(deps),
        ReadEmailTool(deps),
        SearchEmailTool(deps),
        DraftEmailTool(deps),
        SendEmailTool(deps),
    ]
