"""Clipboard tools.

Clipboards hold passwords, tokens and private correspondence, so the contents
are treated as sensitive: they are never sent to a *remote* model provider
while ``security.clipboard_remote_guard`` is on, and the UI only ever shows a
preview unless the user asks for the whole thing.
"""

from __future__ import annotations

import re
from typing import Any

from ...security.permissions import RiskLevel
from ..base import Tool, ToolContext, ToolResult, ToolSpec

_SECRET_PATTERNS = (
    # key/token assignments: "api_key = sk-…", "token: ghp_…", "Bearer eyJ…"
    re.compile(r"\b(sk|pk|api[_-]?key|access[_-]?token|token|secret|bearer)\b\s*[-_:=]?\s+?"
               r"[A-Za-z0-9._\-]{12,}", re.I),
    # provider-issued key shapes
    re.compile(r"\b(?:sk|pk)-[A-Za-z0-9_\-]{16,}|\bgh[pousr]_[A-Za-z0-9]{20,}|"
               r"\bxox[baprs]-[A-Za-z0-9-]{10,}|\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\b\d{4}[ -]?\d{4}[ -]?\d{4}[ -]?\d{4}\b"),  # card-shaped
    re.compile(r"\b(?:password|passphrase|passwd)\b\s*[:=]?\s*\S+", re.I),
)


def looks_sensitive(text: str) -> bool:
    return any(pattern.search(text or "") for pattern in _SECRET_PATTERNS)


class ReadClipboardTool(Tool):
    spec = ToolSpec(
        name="read_clipboard",
        description="Read the current clipboard contents",
        parameters={"type": "object", "properties": {
            "full": {"type": "boolean", "default": False}
        }},
        risk=RiskLevel.LOW,
        category="clipboard",
        expected_ms=80,
        examples=["What did I copy?", "Read my clipboard"],
    )

    def __init__(self, deps):
        self._deps = deps

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        kind = await self._deps.controller.clipboard_kind()
        if kind == "image":
            return ToolResult(data={"kind": "image"},
                              summary="There's an image on the clipboard.")
        text = await self._deps.controller.read_clipboard()
        if not text.strip():
            return ToolResult(data={"text": "", "kind": kind}, summary="The clipboard is empty.")
        sensitive = looks_sensitive(text)
        preview = text if args.get("full") else text[:600]
        spoken = (
            "There's something on the clipboard that looks like a credential — "
            "I'll show it on screen rather than read it aloud."
            if sensitive
            else _speakable(text)
        )
        return ToolResult(
            data={"text": text, "kind": kind, "sensitive": sensitive, "length": len(text)},
            summary=spoken,
            display={"kind": "text", "title": "Clipboard", "text": preview,
                     "sensitive": sensitive},
        )


class WriteClipboardTool(Tool):
    spec = ToolSpec(
        name="write_clipboard",
        description="Replace the clipboard contents with the given text",
        parameters={
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
        risk=RiskLevel.LOW,
        category="clipboard",
        expected_ms=80,
        examples=["Copy that to my clipboard"],
    )

    def __init__(self, deps):
        self._deps = deps

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        text = args["text"]
        ok = await self._deps.controller.write_clipboard(text)
        if not ok:
            return ToolResult.failure("I couldn't write to the clipboard.")
        return ToolResult(data={"length": len(text)}, summary="Copied to your clipboard.",
                          display={"kind": "text", "title": "Copied", "text": text[:400]})


class AppendClipboardTool(Tool):
    spec = ToolSpec(
        name="append_clipboard",
        description="Append text to what is already on the clipboard",
        parameters={
            "type": "object",
            "properties": {"text": {"type": "string"},
                           "separator": {"type": "string", "default": "\n"}},
            "required": ["text"],
        },
        risk=RiskLevel.LOW,
        category="clipboard",
        expected_ms=120,
    )

    def __init__(self, deps):
        self._deps = deps

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        existing = await self._deps.controller.read_clipboard()
        combined = (existing + args.get("separator", "\n") + args["text"]) if existing else args["text"]
        ok = await self._deps.controller.write_clipboard(combined)
        if not ok:
            return ToolResult.failure("I couldn't write to the clipboard.")
        return ToolResult(data={"length": len(combined)}, summary="Appended to your clipboard.")


def _speakable(text: str) -> str:
    condensed = " ".join(text.split())
    if len(condensed) <= 180:
        return f"Your clipboard reads: {condensed}"
    return f"Your clipboard holds about {len(text)} characters, beginning: {condensed[:160]}…"


def clipboard_tools(deps) -> list[Tool]:
    return [ReadClipboardTool(deps), WriteClipboardTool(deps), AppendClipboardTool(deps)]
