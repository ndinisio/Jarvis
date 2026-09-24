"""Where the time went, request by request.

Spans (``core/telemetry.py``) say how long each *kind* of thing takes. They
can't say why "add AA batteries to my basket" took nine seconds: whether it
was the model, the site, or JARVIS waiting for a page to stop moving. A
**request timeline** can. One is opened when a sentence arrives and follows
that request wherever it goes — into a background task, through every model
call and tool — and closes when the result is delivered:

* **time to first action** — from the sentence to the first tool starting;
* **the split** — model time (with tokens), acting, looking (reading the page
  or window again), waiting (for pages to load and settle), and the rest;
* **answered** and **spoken** — when the result was ready, and when speech
  of it began, so "utterance → spoken result" is one number even for an
  errand that finished a minute after the acknowledgement.

It rides a context variable, like the turn id (``core/tracing.py``): a task
started during a turn copies the context, so its model calls and tools land
on the right request without anything being passed around.
"""

from __future__ import annotations

import contextlib
import contextvars
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

#: Tools that look rather than act — the operator's automatic re-reads and
#: explicit waits. Their time is "looking", not "acting".
LOOK_TOOLS = frozenset({"read_page_manifest", "read_window", "wait_for_page", "wait_for_element"})
#: Spans that are one model call each (``model.ttft`` is part of a stream).
MODEL_SPANS = frozenset({"model.chat", "model.stream"})
#: Steps kept per request, for the diagnostics panel.
MAX_STEPS = 80

_current: contextvars.ContextVar[RequestTimeline | None] = contextvars.ContextVar(
    "jarvis_request", default=None)


@dataclass
class RequestTimeline:
    id: str
    text: str
    source: str = "text"
    started: float = field(default_factory=time.time)
    t0: float = field(default_factory=time.perf_counter)
    route: str = ""
    #: Speech recognition before the sentence arrived (voice only).
    stt_ms: float = 0.0
    first_model_ms: float | None = None
    first_action_ms: float | None = None
    #: When the turn itself returned — the answer, or the acknowledgement of
    #: work that continues in the background.
    replied_ms: float | None = None
    answered_ms: float | None = None
    spoken_ms: float | None = None
    total_ms: float | None = None
    background: bool = False
    task_id: str = ""
    model_ms: float = 0.0
    act_ms: float = 0.0
    look_ms: float = 0.0
    wait_ms: float = 0.0
    model_calls: int = 0
    tool_calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    steps: list[dict[str, Any]] = field(default_factory=list)
    #: Speech queued from now on is the result (not an acknowledgement or a
    #: progress line), so its start is "spoken".
    answering: bool = False
    finished: bool = False
    _waiting: int = 0
    _wait_in_tool: float = 0.0

    # -- recording -----------------------------------------------------------
    def elapsed_ms(self) -> float:
        return (time.perf_counter() - self.t0) * 1000.0

    def _at(self, started: float) -> float:
        """Epoch seconds → ms since this request began."""
        return max(0.0, (started - self.started) * 1000.0)

    def note_span(self, name: str, duration_ms: float, started: float, meta: dict[str, Any]) -> None:
        if self.finished:
            return
        at = self._at(started)
        if name in MODEL_SPANS:
            self.model_calls += 1
            self.model_ms += duration_ms
            prompt = int(meta.get("prompt_tokens") or 0)
            completion = int(meta.get("completion_tokens") or 0)
            self.prompt_tokens += prompt
            self.completion_tokens += completion
            if self.first_model_ms is None:
                self.first_model_ms = at
            self._step("model", str(meta.get("slot") or "model"), at, duration_ms,
                       tokens=prompt + completion)
        elif name.startswith("tool."):
            tool = name[5:]
            self.tool_calls += 1
            if self.first_action_ms is None:
                self.first_action_ms = at
            # Waiting for the page inside the tool is waiting, not acting.
            waited, self._wait_in_tool = self._wait_in_tool, 0.0
            net = max(0.0, duration_ms - waited)
            if tool in LOOK_TOOLS:
                self.look_ms += net
            else:
                self.act_ms += net
            self._step("look" if tool in LOOK_TOOLS else "act", tool, at, duration_ms,
                       waited=round(waited))

    def note_wait(self, duration_ms: float) -> None:
        if self.finished:
            return
        self.wait_ms += duration_ms
        self._wait_in_tool += duration_ms

    def _step(self, kind: str, name: str, at: float, ms: float, **extra: Any) -> None:
        if len(self.steps) < MAX_STEPS:
            self.steps.append({"kind": kind, "name": name, "at_ms": round(at), "ms": round(ms),
                               **{k: v for k, v in extra.items() if v}})

    # -- reporting -------------------------------------------------------------
    def as_dict(self) -> dict[str, Any]:
        total = self.total_ms if self.total_ms is not None else self.elapsed_ms()
        accounted = self.model_ms + self.act_ms + self.look_ms + self.wait_ms

        def r(value: float | None) -> float | None:
            return None if value is None else round(value)

        return {
            "id": self.id, "text": self.text, "source": self.source, "route": self.route,
            "started": self.started, "background": self.background, "task_id": self.task_id,
            "finished": self.finished,
            "stt_ms": r(self.stt_ms) or None,
            "first_model_ms": r(self.first_model_ms), "first_action_ms": r(self.first_action_ms),
            "replied_ms": r(self.replied_ms), "answered_ms": r(self.answered_ms),
            "spoken_ms": r(self.spoken_ms), "total_ms": r(total),
            # What the user waited, from the end of their sentence.
            "heard_to_spoken_ms": (r(self.spoken_ms + self.stt_ms)
                                   if self.spoken_ms is not None else None),
            "model_ms": r(self.model_ms), "act_ms": r(self.act_ms), "look_ms": r(self.look_ms),
            "wait_ms": r(self.wait_ms), "other_ms": r(max(0.0, total - accounted)),
            "model_calls": self.model_calls, "tool_calls": self.tool_calls,
            "prompt_tokens": self.prompt_tokens, "completion_tokens": self.completion_tokens,
            "steps": list(self.steps),
        }


# -- the current request --------------------------------------------------------
def current() -> RequestTimeline | None:
    return _current.get()


def activate(timeline: RequestTimeline) -> contextvars.Token:
    return _current.set(timeline)


def deactivate(token: contextvars.Token) -> None:
    with contextlib.suppress(ValueError):
        _current.reset(token)


@contextlib.contextmanager
def waiting() -> Iterator[None]:
    """Time spent waiting for something outside JARVIS — a page loading, its
    requests finishing, an animation ending. Nested waits count once."""
    timeline = _current.get()
    if timeline is None or timeline.finished:
        yield
        return
    timeline._waiting += 1
    t0 = time.perf_counter()
    try:
        yield
    finally:
        timeline._waiting -= 1
        if timeline._waiting == 0:
            timeline.note_wait((time.perf_counter() - t0) * 1000.0)


def answering() -> None:
    """What's said from here on is the result itself."""
    timeline = _current.get()
    if timeline is not None:
        timeline.answering = True


def speech_marker() -> RequestTimeline | None:
    """Captured when speech is queued: the request whose *result* it is, if
    it is one (not an acknowledgement or a progress line)."""
    timeline = _current.get()
    return timeline if timeline is not None and timeline.answering else None
