"""Tool registry: discovery, validation, permission enforcement, telemetry."""

from __future__ import annotations

import platform
import time
from collections.abc import Callable, Iterable
from typing import Any

from ..core.errors import Cancelled, ConfirmationDeclined, JarvisError, NetworkUnavailable
from ..core.events import EventType
from ..core.logging import get_logger
from ..core.tracing import current_turn_id
from ..models.base import ToolDef
from ..security import consequence
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
        """Tool listing for prompting: a signature, what it's for, and the
        per-argument notes that change how it must be called (e.g. "the
        handle shown in the page listing") — terse, but never so terse the
        model has to guess what an argument means."""
        lines = []
        for name in sorted(names or self._tools):
            tool = self._tools.get(name)
            if tool is None:
                continue
            spec = tool.spec
            props = spec.parameters.get("properties", {})
            required = set(spec.parameters.get("required", []))
            args = ", ".join(
                f"{k}:{v.get('type', 'string')}" if k in required else f"[{k}:{v.get('type', 'string')}]"
                for k, v in props.items()
            )
            line = f"- {spec.name}({args}) — {spec.description}"
            notes = [f"{k}: {v['description']}" for k, v in props.items() if v.get("description")]
            if notes:
                line += " (" + "; ".join(notes) + ")"
            if spec.returns:
                line += f" → {spec.returns}"
            lines.append(line)
        return "\n".join(lines)

    def tool_defs(self, names: Iterable[str]) -> list[ToolDef]:
        """Tools as offered to a model for native tool calling.

        Compact on purpose: the schemas are part of every request, so every
        character is paid for on every step. Argument descriptions stay —
        they carry the "use the handle shown in the page listing" kind of
        guidance — while defaults and examples go.
        """
        defs = []
        for name in names:
            tool = self._tools.get(name)
            if tool is None:
                continue
            spec = tool.spec
            description = spec.description.strip()
            if spec.returns and len(description) + len(spec.returns) <= 190:
                description = f"{description.rstrip('.')}. Returns {spec.returns}."
            defs.append(ToolDef(name=spec.name, description=description,
                                parameters=_model_schema(spec.parameters)))
        return defs

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
        if spec.category != "browser":
            # System-level input, an app action or a link opened elsewhere can
            # change a web page too: the next look at one waits for it in full.
            from .browser.observe import acted

            acted()
        try:
            # What the call will really touch, read before it runs — the gate
            # judges that, not the model's description of it.
            target = None
            if spec.risk != "low":
                try:
                    target = await tool.inspect(cleaned, ctx)
                except Exception:  # pragma: no cover - inspection is best effort
                    log.debug("inspect failed for %s", name, exc_info=True)
            consequential = consequence.classify(name, cleaned, spec, target)
            # A low-risk tool can still be pointed at something consequential
            # (opening a checkout URL directly); that call is gated like any
            # other consequential one.
            if spec.risk != "low" or consequential:
                await ctx.permissions.require(
                    action=name,
                    risk=spec.risk if spec.risk != "low" else "medium",
                    summary=_confirmation_text(spec, cleaned, target),
                    details={"tool": name, "args": _redact(cleaned), "category": spec.category,
                             **({"target": target} if target else {})},
                    consequential=consequential,
                    task_id=ctx.task_id,
                )
            result = await tool.run(cleaned, ctx)
        except Cancelled:
            result = ToolResult.failure("Stopped.", detail="cancelled")
        except ConfirmationDeclined as exc:
            # A distinct branch (not folded into the generic JarvisError
            # catch below) so recovery.py can tell "the user said no" apart
            # from every other failure by a stable machine-readable prefix
            # instead of sniffing the human-facing wording, which differs
            # between a timeout and an explicit decline.
            result = ToolResult.failure(
                exc.user_message, detail=f"confirmation_declined:{exc.detail or ''}"
            )
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
        ctx.telemetry.record(f"tool.{name}", result.duration_ms, ok=result.ok, category=spec.category,
                             started=time.time() - result.duration_ms / 1000.0)
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


#: JSON-schema keys a model doesn't need to call a tool correctly.
_SCHEMA_NOISE = {"default", "examples", "example", "title", "$schema"}


#: Arguments a tool still accepts but the model is never offered: escape
#: hatches (which browser, which window by number) and paging knobs whose
#: defaults are right. Each costs characters on every step of every task.
_HIDDEN_ARGUMENTS = {"browser", "window_index", "roles", "limit"}


def _compact_schema(schema: Any) -> Any:
    if isinstance(schema, dict):
        return {key: _compact_schema(value) for key, value in schema.items()
                if key not in _SCHEMA_NOISE}
    if isinstance(schema, list):
        return [_compact_schema(value) for value in schema]
    return schema


def _model_schema(parameters: dict[str, Any]) -> dict[str, Any]:
    compact = _compact_schema(parameters)
    properties = compact.get("properties")
    if isinstance(properties, dict):
        compact["properties"] = {k: v for k, v in properties.items() if k not in _HIDDEN_ARGUMENTS}
        if isinstance(compact.get("required"), list):
            compact["required"] = [r for r in compact["required"] if r not in _HIDDEN_ARGUMENTS]
    return compact


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


def _confirmation_text(spec, args: dict[str, Any], target: dict[str, Any] | None = None) -> str:
    if spec.confirmation_template:
        # The user is asked about the element that will really be clicked,
        # in its own words — not the label the model supplied.
        real = (target or {}).get("text")
        values = {**args, **({"label": real} if real else {})}
        try:
            return spec.confirmation_template.format(**values)
        except (KeyError, IndexError):
            pass  # fall through to the generic phrasing below
    detail = ", ".join(f"{k}={v}" for k, v in list(args.items())[:3])
    return f"{spec.description} ({detail})" if detail else spec.description


def build_registry(deps) -> ToolRegistry:
    """Construct the full tool set. Imported lazily to keep start-up light."""
    from .browser.page_tools import page_tools
    from .browser.tools import browser_tools
    from .calendar.tools import calendar_tools
    from .clipboard.tools import clipboard_tools
    from .contacts.tools import contacts_tools
    from .downloads.installer import installer_tools
    from .downloads.tools import download_tools
    from .email.tools import email_tools
    from .files.tools import file_tools
    from .homekit.tools import homekit_tools
    from .interaction.tools import interaction_tools
    from .macos.everyday import everyday_tools
    from .macos.tools import macos_tools
    from .messages.tools import messages_tools
    from .native.tools import native_tools
    from .reminders.tools import reminders_tools
    from .screen.tools import screen_tools
    from .system.tools import system_tools
    from .web.tools import web_tools

    registry = ToolRegistry()
    caps = deps.config.capabilities
    registry.register_all(macos_tools(deps))
    registry.register_all(everyday_tools(deps))
    registry.register_all(system_tools(deps))
    if caps.clipboard:
        registry.register_all(clipboard_tools(deps))
    if caps.files:
        registry.register_all(file_tools(deps))
        if caps.automation:
            registry.register_all(download_tools(deps))
            registry.register_all(installer_tools(deps))
    if caps.screen:
        registry.register_all(screen_tools(deps))
        # Seeing the screen is only useful if JARVIS can also act on it.
        registry.register_all(interaction_tools(deps))
        registry.register_all(native_tools(deps))
    if caps.browser:
        registry.register_all(browser_tools(deps))
        if caps.automation:
            # Write-capable page interaction (click/fill/submit) builds on
            # the same driver browser_tools already uses; gated separately
            # so the read-only trio above can stay on without exposing it.
            registry.register_all(page_tools(deps))
    if caps.research:
        registry.register_all(web_tools(deps))
    if caps.email:
        registry.register_all(email_tools(deps))
    if caps.calendar:
        registry.register_all(calendar_tools(deps))
    if caps.reminders:
        registry.register_all(reminders_tools(deps))
    if caps.contacts:
        registry.register_all(contacts_tools(deps))
    if caps.messages:
        registry.register_all(messages_tools(deps))
    if caps.homekit:
        registry.register_all(homekit_tools(deps))
    log.info("registered %d tools: %s", len(registry.names()), ", ".join(registry.names()))
    return registry
