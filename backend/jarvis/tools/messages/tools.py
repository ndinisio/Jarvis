"""Messages tools."""

from __future__ import annotations

from typing import Any

from ...security.permissions import RiskLevel
from ..base import Tool, ToolContext, ToolResult, ToolSpec
from .messages_app import AppleMessagesBackend


class _MessagesTool(Tool):
    def __init__(self, deps):
        self._deps = deps
        self.backend = AppleMessagesBackend(deps.controller)


class ReadMessagesTool(_MessagesTool):
    spec = ToolSpec(
        name="read_messages",
        description="Read the most recent text messages / iMessages across all conversations",
        parameters={
            "type": "object",
            "properties": {"limit": {"type": "integer", "default": 10}},
        },
        risk=RiskLevel.LOW,
        category="messages",
        requires_macos=True,
        expected_ms=1500,
        examples=["Check my messages", "Any new texts?"],
    )

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        ctx.report("Reading your messages…", tool="read_messages")
        messages = await self.backend.recent(limit=int(args.get("limit", 10)))
        if not messages:
            return ToolResult(data={"messages": []}, summary="No recent messages.")
        return ToolResult(
            data={"messages": [m.as_dict() for m in messages]},
            summary=_spoken_summary(messages),
            display={"kind": "messages", "title": "Messages",
                     "messages": [m.as_dict() for m in messages]},
        )


class SearchMessagesTool(_MessagesTool):
    spec = ToolSpec(
        name="search_messages",
        description="Find text messages / iMessages matching a phrase",
        parameters={
            "type": "object",
            "properties": {"query": {"type": "string"}, "limit": {"type": "integer", "default": 10}},
            "required": ["query"],
        },
        risk=RiskLevel.LOW,
        category="messages",
        requires_macos=True,
        expected_ms=1500,
    )

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        messages = await self.backend.search(args["query"], limit=int(args.get("limit", 10)))
        if not messages:
            return ToolResult(data={"messages": []},
                              summary=f"Nothing matching “{args['query']}” in your messages.")
        return ToolResult(
            data={"messages": [m.as_dict() for m in messages]},
            summary=f"{len(messages)} matching message{'s' if len(messages) != 1 else ''}. "
                    f"{_one_line(messages[0])}",
            display={"kind": "messages", "title": f"Messages: {args['query']}",
                     "messages": [m.as_dict() for m in messages]},
        )


class SendMessageTool(_MessagesTool):
    spec = ToolSpec(
        name="send_message",
        description="Send a text message or iMessage to a recipient (always confirms first, "
                    "individually, every time)",
        parameters={
            "type": "object",
            "properties": {
                "recipient": {"type": "string",
                             "description": "phone number or email/Apple ID"},
                "body": {"type": "string"},
            },
            "required": ["recipient", "body"],
        },
        risk=RiskLevel.HIGH,
        category="messages",
        requires_macos=True,
        expected_ms=3000,
        always_confirm_individually=True,
        confirmation_template='Send this to {recipient}: "{body}"?',
    )

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        recipient, body = args["recipient"], args["body"]
        await self.backend.send(recipient, body)
        return ToolResult(
            data={"recipient": recipient, "body": body},
            summary=f"Sent to {recipient}.",
        )


def _one_line(message) -> str:
    who = "you" if message.is_from_me else (message.sender or "someone")
    text = message.text[:80] + ("…" if len(message.text) > 80 else "")
    return f"From {who}: {text}"


def _spoken_summary(messages) -> str:
    count = len(messages)
    if count == 1:
        return f"One recent message. {_one_line(messages[0])}"
    return f"{count} recent messages. Latest — {_one_line(messages[0])}"


def messages_tools(deps) -> list[Tool]:
    return [ReadMessagesTool(deps), SearchMessagesTool(deps), SendMessageTool(deps)]
