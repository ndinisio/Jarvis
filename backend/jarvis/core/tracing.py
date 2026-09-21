"""Per-turn correlation IDs for cross-layer tracing.

A ``contextvars.ContextVar``, not a parameter threaded through every
function signature, and not a plain instance attribute on the orchestrator:
turns are concurrent by design — a backgrounded task keeps running after the
synchronous part of a turn has already returned, and a new turn can start
while it does. An instance attribute would be overwritten by the second
turn while the first is still mid-flight, silently mislabelling whatever it
logs from then on. ``asyncio.create_task()`` copies the *current* context at
creation time, so a task spawned partway through a turn keeps logging under
that turn's id even once the turn that started it has moved on — which is
exactly the property needed to tell "one logical request, seen at several
layers" apart from "several independent requests" when reading logs.

This exists to answer one question precisely, raised by a real macOS
runtime report of responses being spoken twice and a browser action
appearing to run several times for what the route log showed as a single
decision: *is this actually the same request reaching a stage more than
once, or several different requests?* Every stage that matters — received,
routed, dispatched, tool started/finished, verified, response emitted, TTS
queued/spoken — logs the same id, so that question is answerable by
grepping one value instead of guessing from timestamps.
"""

from __future__ import annotations

import contextvars
import uuid

_current: contextvars.ContextVar[str] = contextvars.ContextVar("jarvis_turn_id", default="-")


def new_turn_id() -> str:
    """Start a new turn: generate an id, make it current, and return it."""
    turn_id = uuid.uuid4().hex[:8]
    _current.set(turn_id)
    return turn_id


def current_turn_id() -> str:
    """The id of whichever turn's context this code is running under.

    ``"-"`` outside of any turn (start-up, a direct unit-test call) — a
    valid, greppable value, not a crash.
    """
    return _current.get()
