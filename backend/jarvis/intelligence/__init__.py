"""The intelligence layer.

    understand → resolve references → converse, or operate → respond

The deterministic quick path still answers simple requests in milliseconds;
this layer only runs when the quick path declines. Actions — a one-step
question or a fifty-step errand — run on the operator (:mod:`.operator`):
native tool calling, full observations, a checklist that must be proven
before "done", and stuck detection.

Submodules are imported lazily so that importing the package (for the state or
schema types alone) doesn't pull in the whole agent.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "AgentOutcome",
    "ConversationState",
    "IntelligenceAgent",
    "Objective",
    "Operator",
    "ReferenceResolver",
    "attach",
]

_EXPORTS = {
    "Objective": ".schema",
    "Operator": ".operator",
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
