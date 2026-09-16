"""Performance telemetry.

The goal is not "make everything fast" — it is to make the *cost of every
execution path visible*, so a request that could have been answered
deterministically in 20 ms is never quietly sent to a 2-second model.

Spans are cheap (a perf_counter pair), retained in a ring buffer, and surfaced
in the developer panel.
"""

from __future__ import annotations

import time
from collections import defaultdict, deque
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class Span:
    name: str
    duration_ms: float
    started: float
    meta: dict[str, Any] = field(default_factory=dict)
    ok: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "duration_ms": round(self.duration_ms, 2),
            "started": self.started,
            "ok": self.ok,
            **self.meta,
        }


class Telemetry:
    """Ring buffer of spans plus aggregate statistics per span name."""

    def __init__(self, bus=None, capacity: int = 400):
        self._bus = bus
        self._spans: deque[Span] = deque(maxlen=capacity)
        self._totals: dict[str, list[float]] = defaultdict(list)

    @contextmanager
    def span(self, name: str, **meta: Any) -> Iterator[dict[str, Any]]:
        """Time a block. Mutate the yielded dict to attach metadata."""
        started = time.time()
        t0 = time.perf_counter()
        extra: dict[str, Any] = dict(meta)
        ok = True
        try:
            yield extra
        except BaseException:
            ok = False
            raise
        finally:
            duration = (time.perf_counter() - t0) * 1000.0
            self.record(name, duration, started=started, ok=ok, **extra)

    def record(self, name: str, duration_ms: float, *, started: float | None = None,
               ok: bool = True, **meta: Any) -> Span:
        span = Span(name=name, duration_ms=duration_ms, started=started or time.time(),
                    meta=meta, ok=ok)
        self._spans.append(span)
        series = self._totals[name]
        series.append(duration_ms)
        if len(series) > 200:
            del series[:-200]
        if self._bus is not None:
            from .events import EventType

            self._bus.publish(EventType.TELEMETRY, **span.as_dict())
        return span

    def mark(self, name: str, **meta: Any) -> _Stopwatch:
        return _Stopwatch(self, name, meta)

    # -- reporting ---------------------------------------------------------
    def recent(self, limit: int = 60) -> list[dict[str, Any]]:
        return [s.as_dict() for s in list(self._spans)[-limit:]]

    def summary(self) -> dict[str, dict[str, float]]:
        out: dict[str, dict[str, float]] = {}
        for name, values in self._totals.items():
            if not values:
                continue
            ordered = sorted(values)
            out[name] = {
                "count": len(values),
                "avg_ms": round(sum(values) / len(values), 2),
                "p50_ms": round(ordered[len(ordered) // 2], 2),
                "p95_ms": round(ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))], 2),
                "max_ms": round(ordered[-1], 2),
            }
        return out

    def clear(self) -> None:
        self._spans.clear()
        self._totals.clear()


class _Stopwatch:
    """Manual span: ``sw = telemetry.mark("stt"); ...; sw.stop(chars=42)``."""

    def __init__(self, telemetry: Telemetry, name: str, meta: dict[str, Any]):
        self._telemetry = telemetry
        self._name = name
        self._meta = meta
        self._started = time.time()
        self._t0 = time.perf_counter()
        self._stopped = False

    @property
    def elapsed_ms(self) -> float:
        return (time.perf_counter() - self._t0) * 1000.0

    def stop(self, ok: bool = True, **meta: Any) -> Span | None:
        if self._stopped:
            return None
        self._stopped = True
        return self._telemetry.record(
            self._name, self.elapsed_ms, started=self._started, ok=ok, **{**self._meta, **meta}
        )
