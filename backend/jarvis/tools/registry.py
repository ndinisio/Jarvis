"""Tool registry: discovery, validation, permission enforcement, telemetry."""

from __future__ import annotations

import platform
import time
from collections.abc import Callable, Iterable
from typing import Any

from ..core.errors import Cancelled, JarvisError, NetworkUnavailable
from ..core.events import EventType
from ..core.logging import get_logger
from ..core.tracing import current_turn_id
from .base import Tool, ToolContext, ToolResult

log = get_logger("jarvis.tools")

IS_MACOS = platform.system() == "Darwin"


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}
        self._observers: list[Callable[[str, dict[str, Any], ToolResult, str], None]] = []

    def observe(self, callback: Callable[[str, dict[str, Any], ToolResult, str], None]) -> None:
        """Watch every call with its *structured* result.

        The event bus carries summaries, which is all the UI needs. Conversation
        context needs the payload — the actual messages, the actual URL — and it
        needs it for every call whatever made it, so that "reply to the second
        one" still means something when the previous turn took the fast path.
        Observers are notified after the result is complete and must not raise.
        """
        self._observers.append(callback)

    def register(self, tool: Tool) -> Tool:
        if tool.spec.name in self._tools:
            raise ValueError(f"duplicate tool: {tool.spec.name}")
        self._tools[tool.spec.name] = tool
        return tool

    def register_all(self, tools: Iterable[Tool]) -> None:
        for tool in tools:
            self.register(tool)

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return sorted(self._tools)

    def specs(self, category: str | None = None) -> list[dict[str, Any]]:
        return [
            t.spec.as_dict()
            for t in self._tools.values()
            if category is None or t.spec.category == category
        ]

    def by_category(self) -> dict[str, list[str]]:
        grouped: dict[str, list[str]] = {}
        for tool in self._tools.values():
            grouped.setdefault(tool.spec.category, []).append(tool.spec.name)
        return {k: sorted(v) for k, v in sorted(grouped.items())}

    def describe_for_model(self, names: Iterable[str] | None = None) -> str:
        """Compact tool listing for prompting — deliberately terse to keep
        local-model prompts small."""
        lines = []
        for name in sorted(names or self._tools):
            tool = self._tools.get(name)
            if tool is None:
                continue
            props = tool.spec.parameters.get("properties", {})
            args = ", ".join(
                f"{k}:{v.get('type', 'string')}" for k, v in props.items()
            )
            lines.append(f"- {tool.spec.name}({args}) — {tool.spec.description}")
        return "\n".join(lines)

    # -- execution ---------------------------------------------------------
    async def call(self, name: str, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        tool = self._tools.get(name)
        if tool is None:
            return ToolResult.failure(f"I don't have a tool called {name}.")

        spec = tool.spec
        if spec.requires_macos and not IS_MACOS:
            return ToolResult.failure(
                "That only works on macOS.", detail=f"{name} requires Darwin, host is {platform.system()}"
            )

        try:
            cleaned = tool.validate(args or {})
        except ValueError as exc:
            return ToolResult.failure("I'm missing something for that request.", detail=str(exc))

        ctx.bus.publish(
            EventType.TOOL_CALL, tool=name, args=_redact(cleaned), category=spec.category,
            risk=spec.risk, task_id=ctx.task_id,
        )

        if ctx.cancelled():
            return ToolResult.failure("Cancelled.", detail="cancelled before execution")

        t0 = time.perf_counter()
        turn_id = current_turn_id()
        # This is the one place in the whole codebase that actually performs
        # a tool's side effect — every dispatch path (quick, agent,
        # capability, background) funnels through here. Bracketing exactly
        # this call, under the turn id that started the request, is what
        # turns "the browser opened several times" from an observation into
        # a fact: either tool_start for this turn_id appears more than once
        # (the side effect really did run repeatedly) or it doesn't (the
        # duplication is somewhere else — voice capture, TTS, the UI).
        log.info("turn_id=%s stage=tool_start tool=%s", turn_id, name)
        try:
            if spec.risk != "low":
                await ctx.permissions.require(
                    action=name,
                    risk=spec.risk,
                    summary=_confirmation_text(spec, cleaned),
                    details={"tool": name, "args": _redact(cleaned), "category": spec.category},
                )
            result = await tool.run(cleaned, ctx)
        except Cancelled:
            result = ToolResult.failure("Stopped.", detail="cancelled")
        except NetworkUnavailable as exc:
            result = ToolResult.failure(exc.user_message, detail=exc.detail)
        except JarvisError as exc:
            result = ToolResult.failure(exc.user_message, detail=exc.detail)
        except Exception as exc:  # pragma: no cover - last-resort guard
            log.exception("tool %s crashed", name)
            result = ToolResult.failure(
                "That operation failed unexpectedly.", detail=f"{type(exc).__name__}: {exc}"
            )

        result.duration_ms = (time.perf_counter() - t0) * 1000.0
        log.info("turn_id=%s stage=tool_end tool=%s ok=%s duration_ms=%.1f",
                 turn_id, name, result.ok, result.duration_ms)
        ctx.telemetry.record(f"tool.{name}", result.duration_ms, ok=result.ok, category=spec.category)
        ctx.bus.publish(
            EventType.TOOL_RESULT,
            tool=name,
            ok=result.ok,
            summary=result.summary,
            error=result.error,
            duration_ms=round(result.duration_ms, 2),
            task_id=ctx.task_id,
            display=result.display,
        )
        for observer in self._observers:
            try:
                observer(name, cleaned, result, spec.category)
            except Exception:  # pragma: no cover - an observer must never break a tool
                log.exception("tool observer failed for %s", name)
        return result


_SENSITIVE_KEYS = {"password", "token", "api_key", "secret", "passphrase"}


def _redact(args: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in args.items():
        if key.lower() in _SENSITIVE_KEYS:
            out[key] = "••••"
        elif isinstance(value, str) and len(value) > 400:
            out[key] = value[:400] + "…"
        else:
            out[key] = value
    return out


def _confirmation_text(spec, args: dict[str, Any]) -> str:
    detail = ", ".join(f"{k}={v}" for k, v in list(args.items())[:3])
    return f"{spec.description} ({detail})" if detail else spec.description


def build_registry(deps) -> ToolRegistry:
    """Construct the full tool set. Imported lazily to keep start-up light."""
    from .browser.tools import browser_tools
    from .calendar.tools import calendar_tools
    from .clipboard.tools import clipboard_tools
    from .email.tools import email_tools
    from .files.tools import file_tools
    from .interaction.tools import interaction_tools
    from .macos.tools import macos_tools
    from .screen.tools import screen_tools
    from .system.tools import system_tools
    from .web.tools import web_tools

    registry = ToolRegistry()
    caps = deps.config.capabilities
    registry.register_all(macos_tools(deps))
    registry.register_all(system_tools(deps))
    if caps.clipboard:
        registry.register_all(clipboard_tools(deps))
    if caps.files:
        registry.register_all(file_tools(deps))
    if caps.screen:
        registry.register_all(screen_tools(deps))
        # Seeing the screen is only useful if JARVIS can also act on it.
        registry.register_all(interaction_tools(deps))
    if caps.browser:
        registry.register_all(browser_tools(deps))
    if caps.research:
        registry.register_all(web_tools(deps))
    if caps.email:
        registry.register_all(email_tools(deps))
    if caps.calendar:
        registry.register_all(calendar_tools(deps))
    log.info("registered %d tools: %s", len(registry.names()), ", ".join(registry.names()))
    return registry
