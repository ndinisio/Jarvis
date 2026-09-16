"""Routing types."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


class RouteKind:
    #: Answered by a deterministic handler: greeting, cancel, time-of-day…
    CONTROL = "control"
    #: A single tool call answers it outright.
    TOOL = "tool"
    #: Hand to a capability module (may use several tools and a model).
    CAPABILITY = "capability"
    #: Ordinary conversation through a language model.
    CHAT = "chat"


class RoutePath:
    QUICK = "quick"           # deterministic pattern match, ~0 ms
    HEURISTIC = "heuristic"   # keyword scoring, ~0 ms
    MODEL = "model"           # fast model classification
    FALLBACK = "fallback"


@dataclass
class RouteDecision:
    kind: str
    name: str
    args: dict[str, Any] = field(default_factory=dict)
    confidence: float = 1.0
    path: str = RoutePath.QUICK
    reason: str = ""
    #: Should the user get an immediate acknowledgement while this runs?
    long_running: bool = False
    #: Speak the tool's own summary rather than paraphrasing it with a model.
    speak_result_directly: bool = True
    latency_ms: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "name": self.name,
            "args": self.args,
            "confidence": round(self.confidence, 2),
            "path": self.path,
            "reason": self.reason,
            "long_running": self.long_running,
            "latency_ms": round(self.latency_ms, 2),
        }
