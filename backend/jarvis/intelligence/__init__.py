"""The V1.2 intelligence layer.

V1.1 routed a sentence to a capability label and ran one tool. V1.2 puts a
proper agent loop above that:

    understand → resolve references → decide → act → observe → verify →
    repair or continue → respond

The deterministic quick path from V1.1 is untouched and still answers simple
requests in milliseconds; this layer only runs when the quick path declines.

Submodules are imported lazily so that importing the package (for the state or
schema types alone) doesn't pull in the whole agent.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "AgentDecision",
    "AgentOutcome",
    "ConversationState",
    "IntelligenceAgent",
    "Objective",
    "Plan",
    "ReferenceResolver",
    "attach",
]

_EXPORTS = {
    "AgentDecision": ".schema",
    "Objective": ".schema",
    "Plan": ".schema",
    "ConversationState": ".state",
    "attach": ".state",
    "ReferenceResolver": ".entities",
    "AgentOutcome": ".agent",
    "IntelligenceAgent": ".agent",
}


def __getattr__(name: str) -> Any:
    module_name = _EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from importlib import import_module

    return getattr(import_module(module_name, __name__), name)
