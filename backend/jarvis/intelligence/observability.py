"""Intelligence tracing.

Developer mode should make the *process* legible — what was understood, what
was planned, which tool ran, what came back, whether it verified — without
exposing the model's private reasoning. Every entry here is structured state:
decisions, arguments, outcomes and statuses. The ``reason`` fields are one-line
justifications the model attaches to a decision, not its deliberation.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from ..core.events import EventBus, EventType
from ..core.logging import get_logger

log = get_logger("jarvis.intelligence")

#: Event name shared with the frontend (frontend/src/lib/events.ts).
TRACE_EVENT = EventType.INTELLIGENCE_TRACE


class Trace:
    """Collects one turn's decisions and publishes them as they happen."""

    def __init__(self, bus: EventBus | None = None, telemetry=None, verbose: bool = False,
                 task_id: str | None = None):
        self._bus = bus
        self._telemetry = telemetry
        #: Set for a background task's trace, so a display of the current
        #: turn's reasoning can tell the two apart.
        self._task_id = task_id
        #: Developer mode logs each stage at INFO, in the aligned form the V1.2
        #: brief asks for, so a terminal shows the process without a log-level
        #: change. Otherwise it stays at DEBUG.
        self._level = logging.INFO if verbose else logging.DEBUG
        self.entries: list[dict[str, Any]] = []
        self._started = time.perf_counter()

    # -- stages ------------------------------------------------------------
    def triage(self, triage) -> None:
        self._add("triage", {"mode": triage.mode, "confidence": round(triage.confidence, 2),
                             "action_evidence": triage.action_evidence[:5],
                             "reason": triage.reason[:160]})

    def intent(self, objective, state) -> None:
        self._add("intent", {
            "goal": objective.goal,
            "kind": objective.kind,
            "targets": objective.targets[:5],
            "constraints": objective.constraints[:5],
            "complexity": objective.complexity,
            "confidence": objective.confidence,
            "refines_previous": objective.refines_previous,
            "is_correction": objective.is_correction,
            "context": _context_line(state),
        })

    def checklist(self, items: list[dict[str, Any]]) -> None:
        """What "done" means for this task, and how much of it is proven."""
        self._add("checklist", {"items": [{"text": item.get("text", "")[:160],
                                           "done": bool(item.get("done"))} for item in items]})

    def decision(self, action: str, *, tool: str | None = None,
                 arguments: dict[str, Any] | None = None, reason: str = "") -> None:
        self._add("decision", {"action": action, "tool": tool,
                               "arguments": _redact(arguments), "reason": reason[:160]})

    def step(self, index: int, tool: str, arguments: dict, note: str = "") -> None:
        self._add("step", {"index": index, "tool": tool,
                           "arguments": _redact(arguments), "note": note})

    def result(self, index: int, tool: str, result) -> None:
        self._add("result", {"index": index, "tool": tool, "ok": result.ok,
                             "summary": (result.summary or "")[:200],
                             "duration_ms": round(result.duration_ms, 1)})

    def verify(self, verification) -> None:
        self._add("verify", {"verified": verification.verified,
                             "confidence": round(verification.confidence, 2),
                             "skipped": verification.skipped,
                             "problem": verification.problem[:160],
                             "evidence": verification.evidence[:160]})

    def recover(self, strategy: str, reason: str, *, tool: str | None = None) -> None:
        """A change of course: a re-plan, a hint about going in circles, or
        stopping to report (a declined confirmation)."""
        self._add("recover", {"strategy": strategy, "tool": tool, "reason": reason[:160]})

    def clarify(self, question: str) -> None:
        self._add("clarify", {"question": question})

    def complete(self, outcome) -> None:
        self._add("complete", {
            "steps": outcome.steps,
            "tool_calls": outcome.tool_calls,
            "model_calls": outcome.model_calls,
            "replans": getattr(outcome, "replans", 0),
            "elapsed_ms": round((time.perf_counter() - self._started) * 1000, 1),
        })
        if self._telemetry is not None:
            self._telemetry.record("intelligence.turn",
                                   (time.perf_counter() - self._started) * 1000,
                                   steps=outcome.steps, tool_calls=outcome.tool_calls,
                                   model_calls=outcome.model_calls)

    # -- plumbing ----------------------------------------------------------
    def _add(self, stage: str, payload: dict[str, Any]) -> None:
        entry = {"stage": stage, "ts": time.time(), **payload}
        if self._task_id:
            entry["task_id"] = self._task_id
        self.entries.append(entry)
        log.log(self._level, "%-9s %s", stage.upper(), _log_line(stage, payload))
        if self._bus is not None:
            self._bus.publish(TRACE_EVENT, **entry)


def _context_line(state) -> str:
    parts = [state.browser.describe(), state.email.describe(),
             state.research.describe(), state.screen.describe()]
    return " | ".join(part.split("\n")[0] for part in parts if part)[:200]


_SENSITIVE = {"password", "token", "api_key", "secret", "body"}


def _redact(arguments: dict[str, Any] | None) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in (arguments or {}).items():
        if key.lower() in _SENSITIVE:
            out[key] = f"<{len(str(value))} chars>"
        else:
            text = str(value)
            out[key] = text[:120] + ("…" if len(text) > 120 else "")
    return out


def _log_line(stage: str, payload: dict[str, Any]) -> str:
    if stage == "triage":
        return f"{payload['mode']} (confidence {payload['confidence']}) — {payload['reason']}"
    if stage == "intent":
        return f"{payload['kind']}: {payload['goal']} [{payload['confidence']}]"
    if stage == "checklist":
        items = payload.get("items") or []
        return f"{sum(1 for i in items if i.get('done'))}/{len(items)} proven"
    if stage == "decision":
        return f"{payload['action']} {payload.get('tool') or ''}".strip()
    if stage == "result":
        return f"{payload['tool']} {'ok' if payload['ok'] else 'failed'}: {payload['summary'][:80]}"
    if stage == "verify":
        return ("verified" if payload["verified"] else f"NOT verified — {payload['problem']}")
    if stage == "recover":
        return f"{payload['strategy']}: {payload['reason']}"
    if stage == "complete":
        return (f"{payload['steps']} step(s), {payload['tool_calls']} tool call(s), "
                f"{payload['model_calls']} model call(s), {payload['elapsed_ms']} ms")
    return str(payload)[:120]
