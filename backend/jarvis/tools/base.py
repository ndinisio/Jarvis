"""The formal tool interface.

Every capability JARVIS has is expressed as a tool with a name, a description,
a JSON-schema input, a declared risk level and a cancellation story. The model
selects *tools*; it never fabricates shell commands when a dedicated tool
exists.
"""

from __future__ import annotations

import abc
import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ..core.config import Config
from ..core.events import EventBus, EventType
from ..core.telemetry import Telemetry
from ..security.permissions import RiskLevel


@dataclass(slots=True)
class ToolSpec:
    name: str
    description: str
    #: JSON Schema (object) describing the arguments.
    parameters: dict[str, Any] = field(default_factory=lambda: {"type": "object", "properties": {}})
    risk: str = RiskLevel.LOW
    #: Grouping used by the router and the UI activity panel.
    category: str = "general"
    #: Cooperative cancellation supported?
    cancellable: bool = True
    #: Needs an internet connection?
    requires_network: bool = False
    #: Only meaningful on macOS?
    requires_macos: bool = False
    #: Typical execution budget, used for UI expectations and timeouts.
    expected_ms: int = 200
    examples: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
            "risk": self.risk,
            "category": self.category,
            "cancellable": self.cancellable,
            "requires_network": self.requires_network,
            "requires_macos": self.requires_macos,
            "expected_ms": self.expected_ms,
            "examples": self.examples,
        }

    def required_args(self) -> list[str]:
        return list(self.parameters.get("required", []))


@dataclass(slots=True)
class ToolResult:
    ok: bool = True
    #: Structured payload for downstream reasoning.
    data: Any = None
    #: One short line suitable for speech.
    summary: str = ""
    #: Rich payload rendered in the UI result panel.
    display: dict[str, Any] | None = None
    error: str | None = None
    duration_ms: float = 0.0

    @classmethod
    def failure(cls, message: str, detail: str | None = None) -> ToolResult:
        return cls(ok=False, summary=message, error=detail or message)

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "summary": self.summary,
            "data": self.data,
            "display": self.display,
            "error": self.error,
            "duration_ms": round(self.duration_ms, 2),
        }


@dataclass
class ToolContext:
    """Everything a tool is allowed to reach."""

    config: Config
    bus: EventBus
    telemetry: Telemetry
    permissions: Any  # PermissionBroker — typed loosely to avoid a cycle
    #: Set when the tool runs inside a background task.
    task_id: str | None = None
    cancel_event: asyncio.Event | None = None
    #: Report intermediate progress ("Opening result 2 of 5…").
    progress: Callable[[str, dict[str, Any] | None], None] | None = None
    models: Any = None
    memory: Any = None

    def cancelled(self) -> bool:
        return bool(self.cancel_event and self.cancel_event.is_set())

    def raise_if_cancelled(self) -> None:
        from ..core.errors import Cancelled

        if self.cancelled():
            raise Cancelled()

    def report(self, message: str, **meta: Any) -> None:
        if self.progress:
            self.progress(message, meta or None)
        self.bus.publish(
            EventType.ACTIVITY, message=message, task_id=self.task_id, **meta
        )


class Tool(abc.ABC):
    """Base class for all tools."""

    spec: ToolSpec

    @abc.abstractmethod
    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        """Execute. Must not raise for ordinary failure — return a failed result."""

    # Optional hook: tools that can't run here (missing dependency, wrong OS)
    async def health(self) -> tuple[bool, str]:
        return True, "ok"

    def validate(self, args: dict[str, Any]) -> dict[str, Any]:
        """Light JSON-schema validation: required keys, types, defaults, enums."""
        schema = self.spec.parameters or {}
        props: dict[str, Any] = schema.get("properties", {})
        cleaned: dict[str, Any] = {}
        for key, definition in props.items():
            if key in args and args[key] is not None:
                cleaned[key] = _coerce(args[key], definition)
            elif "default" in definition:
                cleaned[key] = definition["default"]
        missing = [k for k in schema.get("required", []) if k not in cleaned or cleaned[k] == ""]
        if missing:
            raise ValueError(f"missing required argument(s): {', '.join(missing)}")
        for key, definition in props.items():
            if key in cleaned and "enum" in definition and cleaned[key] not in definition["enum"]:
                raise ValueError(
                    f"{key} must be one of {', '.join(map(str, definition['enum']))}"
                )
        return cleaned


def _coerce(value: Any, definition: dict[str, Any]) -> Any:
    expected = definition.get("type")
    try:
        if expected == "string" and not isinstance(value, str):
            return str(value)
        if expected == "integer" and not isinstance(value, int):
            return int(value)
        if expected == "number" and not isinstance(value, (int, float)):
            return float(value)
        if expected == "boolean" and not isinstance(value, bool):
            if isinstance(value, str):
                return value.strip().lower() in {"true", "yes", "1", "on"}
            return bool(value)
        if expected == "array" and isinstance(value, str):
            return [part.strip() for part in value.split(",") if part.strip()]
    except (TypeError, ValueError):
        return value
    return value


class FunctionTool(Tool):
    """Adapter that turns a coroutine into a tool — used for small handlers."""

    def __init__(self, spec: ToolSpec, fn: Callable[[dict[str, Any], ToolContext], Any]):
        self.spec = spec
        self._fn = fn

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        result = await self._fn(args, ctx)
        if isinstance(result, ToolResult):
            return result
        if isinstance(result, str):
            return ToolResult(summary=result, data=result)
        return ToolResult(data=result, summary="")
