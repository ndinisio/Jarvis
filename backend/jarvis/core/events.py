"""The event bus.

Everything JARVIS does is published as an event: state changes, transcripts,
token deltas, tool calls, task progress, telemetry, errors. The UI is a pure
projection of this stream, which is what keeps the interface honest about what
the assistant is actually doing.

The bus is asyncio-native, non-blocking and never applies back-pressure to a
producer: a slow subscriber drops its oldest events rather than stalling the
orchestrator.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import time
from collections import deque
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any


class EventType:
    """Canonical event names shared with the frontend (see frontend/src/lib/events.ts)."""

    # lifecycle
    HELLO = "hello"
    CONFIG = "config"
    STATE = "state"
    ERROR = "error"
    NOTICE = "notice"

    # conversation
    TRANSCRIPT = "transcript"            # user speech / text, possibly partial
    ASSISTANT_DELTA = "assistant.delta"  # streamed token
    ASSISTANT_MESSAGE = "assistant.message"
    ROUTE = "route"                      # routing decision (developer mode)

    # execution
    ACTIVITY = "activity"                # human-readable "what I'm doing right now"
    INTELLIGENCE_TRACE = "intelligence.trace"  # one stage of the agentic loop
    TOOL_CALL = "tool.call"
    TOOL_RESULT = "tool.result"
    TASK_CREATED = "task.created"
    TASK_UPDATED = "task.updated"
    TASK_FINISHED = "task.finished"

    # voice
    VOICE_STATE = "voice.state"
    SPEECH_START = "speech.start"
    SPEECH_END = "speech.end"
    WAKE = "wake"

    # interaction
    CONFIRM_REQUEST = "confirm.request"
    CONFIRM_RESOLVED = "confirm.resolved"
    SCREEN_IMAGE = "screen.image"
    RESULT_PANEL = "result.panel"        # rich result rendered in the UI
    MEMORY = "memory"
    TELEMETRY = "telemetry"
    #: One request's timeline (core/latency.py): published when the turn
    #: returns, when background work delivers, and when its answer is spoken.
    REQUEST_TIMING = "request.timing"


class AssistantState:
    IDLE = "idle"
    LISTENING = "listening"
    THINKING = "processing"
    SPEAKING = "speaking"
    EXECUTING = "executing"
    RESEARCHING = "researching"
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    ERROR = "error"


@dataclass(slots=True)
class Event:
    type: str
    payload: dict[str, Any] = field(default_factory=dict)
    ts: float = field(default_factory=time.time)
    seq: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {"type": self.type, "ts": self.ts, "seq": self.seq, **self.payload}


class Subscription:
    """A bounded queue fed by the bus. Oldest events are dropped when full."""

    def __init__(self, bus: EventBus, maxsize: int = 512):
        self._bus = bus
        self._queue: deque[Event] = deque(maxlen=maxsize)
        self._wakeup = asyncio.Event()
        self.dropped = 0

    def _offer(self, event: Event) -> None:
        if len(self._queue) == self._queue.maxlen:
            self.dropped += 1
        self._queue.append(event)
        self._wakeup.set()

    async def __aenter__(self) -> Subscription:
        return self

    async def __aexit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        self._bus._unsubscribe(self)
        self._wakeup.set()

    async def __aiter__(self) -> AsyncIterator[Event]:
        while True:
            while self._queue:
                yield self._queue.popleft()
            self._wakeup.clear()
            await self._wakeup.wait()
            if self not in self._bus._subscribers and not self._queue:
                return


class EventBus:
    def __init__(self, history: int = 200):
        self._subscribers: list[Subscription] = []
        self._history: deque[Event] = deque(maxlen=history)
        self._counter = itertools.count(1)
        self._hooks: list[Callable[[Event], None]] = []

    # -- publishing --------------------------------------------------------
    def publish(self, type_: str, **payload: Any) -> Event:
        event = Event(type=type_, payload=payload, seq=next(self._counter))
        self._history.append(event)
        for sub in list(self._subscribers):
            sub._offer(event)
        for hook in list(self._hooks):
            # A hook must never break the bus.
            with contextlib.suppress(Exception):  # pragma: no cover
                hook(event)
        return event

    def emit_state(self, state: str, **payload: Any) -> Event:
        return self.publish(EventType.STATE, state=state, **payload)

    # -- subscribing -------------------------------------------------------
    def subscribe(self, replay: int = 0) -> Subscription:
        sub = Subscription(self)
        self._subscribers.append(sub)
        if replay:
            for event in list(self._history)[-replay:]:
                sub._offer(event)
        return sub

    def _unsubscribe(self, sub: Subscription) -> None:
        if sub in self._subscribers:
            self._subscribers.remove(sub)

    def add_hook(self, hook: Callable[[Event], None]) -> None:
        """Register a synchronous observer (used by logging and telemetry)."""
        self._hooks.append(hook)

    @property
    def history(self) -> list[Event]:
        return list(self._history)

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)
