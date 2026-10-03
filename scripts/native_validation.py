"""Recording, timing and reporting for scripts/check_native.py.

A real-Mac validation pass has to leave behind more than a screenful of ticks:
what ran, how long each part of it took, whether it passed every time it was
run, and which of the things the native control is supposed to be able to do
have actually been shown. This is that layer, kept apart from the checks so it
can be tested like any other code: it knows nothing about macOS.

* ``Recorder`` collects every result, and the time each check spent in each
  *phase* — observation (reading a window), locator (finding the element behind
  a handle), action (pressing, typing, choosing, dragging), refresh (looking
  again after acting), verification (confirming it worked by something other
  than the tree) and settle (waits the check inserts for an app to catch up,
  kept apart so they don't pass for JARVIS being slow).
* ``TimedSurface`` wraps a ``NativeSurface`` so those phases are timed without
  every call site doing it by hand.
* ``evaluate`` turns the results into the exit criteria for real-Mac
  validation: which are shown, which failed, which weren't reached, which could
  not be told.
"""

from __future__ import annotations

import contextlib
import platform
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

PHASES = ("observation", "locator", "action", "refresh", "verification", "settle")

#: Which phase a surface call belongs to. A read or mark after the check has
#: acted is the "refresh" of what it did; the time a call spends finding the
#: element behind a handle is taken out of it and counted as "locator".
PHASE_OF = {
    "read": "observation", "mark": "observation",
    "find": "locator",
    "describe": "verification",
    "press": "action", "type_into": "action", "type_text": "action", "press_key": "action",
    "choose_option": "action", "choose_menu": "action", "drag": "action", "click_mark": "action",
    "scroll_to": "action",
}


class Timings:
    """The seconds one run of one check spent in each phase."""

    def __init__(self, name: str):
        self.name = name
        self.seconds = dict.fromkeys(PHASES, 0.0)
        self.calls = dict.fromkeys(PHASES, 0)
        self.started = time.monotonic()
        self.total: float | None = None
        self.acted = False

    def add(self, phase: str, seconds: float) -> None:
        self.seconds[phase] += max(0.0, seconds)
        self.calls[phase] += 1

    def finish(self) -> None:
        self.total = time.monotonic() - self.started

    def snapshot(self) -> dict[str, Any]:
        return {"seconds": dict(self.seconds), "calls": dict(self.calls), "total": self.total or 0.0}


class TimedSurface:
    """A ``NativeSurface`` whose observing, locating, acting and verifying calls
    are timed into *timings*. Everything else passes straight through."""

    def __init__(self, surface: Any, timings: Timings):
        self._surface = surface
        self._timings = timings

    def __getattr__(self, name: str) -> Any:
        attribute = getattr(self._surface, name)
        phase = PHASE_OF.get(name)
        if phase is None or not callable(attribute):
            return attribute
        surface, timings = self._surface, self._timings

        async def timed_call(*args: Any, **kwargs: Any) -> Any:
            resolving = getattr(surface, "resolve_seconds", 0.0)
            started = time.monotonic()
            try:
                return await attribute(*args, **kwargs)
            finally:
                elapsed = time.monotonic() - started
                located = min(elapsed, max(0.0, getattr(surface, "resolve_seconds", 0.0) - resolving))
                kind = "refresh" if phase == "observation" and timings.acted else phase
                if phase == "action":
                    timings.acted = True
                if located and phase in {"action", "verification"}:
                    timings.add("locator", located)
                    elapsed -= located
                timings.add(kind, elapsed)

        return timed_call


@dataclass
class Recorder:
    """Everything a validation pass saw: each result (``steps``), and for each
    check each run's timings (``runs``)."""

    steps: list[dict[str, Any]] = field(default_factory=list)
    runs: list[dict[str, Any]] = field(default_factory=list)
    iteration: int = 0
    current: Timings | None = None
    check: str = ""
    #: Findings that aren't a pass or a fail (what the observer is worth, say).
    extra: dict[str, Any] = field(default_factory=dict)

    def record(self, label: str, status: str, detail: str = "") -> None:
        self.steps.append({"iteration": self.iteration, "check": self.check, "label": label,
                           "status": status, "detail": detail})

    def add(self, phase: str, seconds: float) -> None:
        if self.current is not None:
            self.current.add(phase, seconds)

    @contextlib.contextmanager
    def verifying(self) -> Iterator[None]:
        started = time.monotonic()
        try:
            yield
        finally:
            self.add("verification", time.monotonic() - started)

    @contextlib.contextmanager
    def measure(self, name: str, surface: Any) -> Iterator[TimedSurface]:
        """Time one run of a check named *name*. Its result is whether every
        step it recorded passed (False if it raised, undecided if it recorded
        none) — or whatever the caller sets afterwards with ``mark``."""
        timings = Timings(name)
        previous, self.current, self.check = self.current, timings, name
        first = len(self.steps)
        crashed = False
        try:
            yield TimedSurface(surface, timings)
        except BaseException:
            crashed = True
            raise
        finally:
            timings.finish()
            mine = self.steps[first:]
            passed: bool | None = False if crashed else (all(s["status"] == "pass" for s in mine) if mine else None)
            self.runs.append({"check": name, "iteration": self.iteration, "timings": timings.snapshot(),
                              "passed": passed})
            self.current, self.check = previous, previous.name if previous else ""

    def mark(self, name: str, passed: bool) -> None:
        """Set the result of the latest run of *name*, for a check whose verdict
        is decided after it returns."""
        for run in reversed(self.runs):
            if run["check"] == name:
                run["passed"] = passed
                return


def percentile(values: list[float], fraction: float) -> float:
    """Nearest-rank percentile; 0.0 of nothing."""
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, max(0, round(fraction * len(ordered) + 0.5) - 1))]


def summarise(runs: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Per check: how many runs, how many passed, and each phase's median, 95th
    percentile and worst, in milliseconds."""
    summary: dict[str, dict[str, Any]] = {}
    for name in dict.fromkeys(run["check"] for run in runs):
        mine = [run for run in runs if run["check"] == name]
        phases = {}
        for phase in (*PHASES, "total"):
            values = [(run["timings"]["total"] if phase == "total" else run["timings"]["seconds"][phase]) * 1000
                      for run in mine]
            phases[phase] = {"p50": percentile(values, 0.5), "p95": percentile(values, 0.95),
                             "max": max(values)}
        summary[name] = {"runs": len(mine), "passed": sum(1 for run in mine if run["passed"]),
                         "phases": phases}
    return summary


def format_timings(summary: dict[str, dict[str, Any]], repeat: int) -> list[str]:
    if not summary:
        return []
    columns = [("obs", "observation"), ("locate", "locator"), ("action", "action"), ("refresh", "refresh"),
               ("verify", "verification"), ("settle", "settle"), ("total", "total")]
    width = max(len(name) for name in summary) + 2
    lines = [f"Timings in milliseconds — median of {repeat} run(s)"
             + (", worst in brackets" if repeat > 1 else "") + "; settle is the check's own waiting",
             "".join([f"{'check':<{width}}", *(f"{title:>14}" for title, _ in columns), f"{'passed':>9}"])]
    for name, entry in summary.items():
        cells = []
        for _, phase in columns:
            stats = entry["phases"][phase]
            cells.append(f"{stats['p50']:.0f}" + (f" [{stats['max']:.0f}]" if repeat > 1 else ""))
        lines.append("".join([f"{name:<{width}}", *(f"{cell:>14}" for cell in cells),
                              f"{entry['passed']:>5}/{entry['runs']:<3}"]))
    return lines


# ---------------------------------------------------------------------------------------------
# the exit criteria
# ---------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Criterion:
    area: str
    requirement: str
    labels: tuple[str, ...] = ()
    #: "manual" for what no script can do; "repeat" and "timings" are computed.
    kind: str = "steps"


CRITERIA = (
    Criterion("AX observation", "real applications successfully inspected",
              ("read the window", "menus", "text on a screenshot",
               "screenshot text matches the window's own text")),
    Criterion("AX actions", "press and set-value actually work",
              ("pressing the buttons worked the sum", "typed Unicode text")),
    Criterion("Keyboard", "real input to the target process verified",
              ("typed Unicode text", "pressing the buttons worked the sum")),
    Criterion("Windows", "background targeting verified", ("a press works with another app in front",)),
    Criterion("Menus", "real menu traversal verified",
              ("chose Format › Font › Bold", "switched the Finder window to a list")),
    Criterion("Stale handles", "stale → re-found → verified; ambiguity and look-alikes refused",
              ("a rebuilt button is re-found and pressed",
               "two buttons told apart by identifier: the right one is pressed",
               "two identical buttons: none is guessed",
               "a look-alike of another kind is not pressed",
               "a button renamed in place is not pressed",
               "the same name in another window is not pressed")),
    Criterion("Controls", "Calculator, TextEdit and Finder checks pass",
              ("pressing the buttons worked the sum", "clicking the mark pressed the button",
               "the file is inside the folder")),
    Criterion("Save sheet", "the real TextEdit save flow works",
              ("the Save sheet lists a pop-up button", "the pop-up now says Desktop",
               "the save sheet appeared, listed first", "discarding closed the document without saving")),
    Criterion("Observer", "real notifications received, none missed, none raising",
              ("AXApplicationActivated arrives", "AXMenuOpened arrives", "no notification was missed",
               "no callback raised", "the observer costs next to no CPU", "no thread is left behind")),
    Criterion("Observer fallback", "polling still works without it",
              ("an unsupported notification is handled", "waits still work with the observer stopped")),
    Criterion("Permissions", "the right failure when Accessibility or Screen Recording is off",
              kind="manual"),
    Criterion("Evals", "the three new Mac tasks pass (evals.run_mac)", kind="manual"),
    Criterion("Repeatability", "every critical check passes every time it is run", kind="repeat"),
    Criterion("Performance", "baseline timings captured", kind="timings"),
)

#: What "critical" means for the Repeatability row: everything the rows above it rest on.
CRITICAL = tuple(dict.fromkeys(label for criterion in CRITERIA if criterion.kind == "steps"
                               for label in criterion.labels))
MIN_REPEATS = 3


def label_status(steps: list[dict[str, Any]], label: str) -> str:
    """"fail" if it ever failed, "not run" if never reached, "inconclusive" if
    it ever couldn't tell, else "pass"."""
    records = [step["status"] for step in steps if step["label"] == label]
    if "fail" in records:
        return "fail"
    if not records:
        return "not run"
    return "inconclusive" if "inconclusive" in records else "pass"


def evaluate(steps: list[dict[str, Any]], runs: list[dict[str, Any]], repeat: int
             ) -> list[tuple[str, str, str, str]]:
    """``(area, requirement, status, detail)`` for every criterion. Status is one
    of pass, fail, inconclusive, not run, manual."""
    rows = []
    for criterion in CRITERIA:
        if criterion.kind == "manual":
            rows.append((criterion.area, criterion.requirement, "manual", "see evals/MAC_VALIDATION.md"))
        elif criterion.kind == "timings":
            captured = [run for run in runs if run["timings"]["total"]]
            rows.append((criterion.area, criterion.requirement, "pass" if captured else "not run",
                         f"{len({run['check'] for run in captured})} check(s) timed" if captured else ""))
        elif criterion.kind == "repeat":
            rows.append((criterion.area, criterion.requirement, *_repeatability(steps, repeat)))
        else:
            statuses = {label: label_status(steps, label) for label in criterion.labels}
            worst = next((status for status in ("fail", "not run", "inconclusive")
                          if status in statuses.values()), "pass")
            rows.append((criterion.area, criterion.requirement, worst,
                         "; ".join(f"{label} ({status})" for label, status in statuses.items()
                                   if status != "pass")))
    return rows


def _repeatability(steps: list[dict[str, Any]], repeat: int) -> tuple[str, str]:
    if repeat < MIN_REPEATS:
        return "not run", f"needs --repeat {MIN_REPEATS} or more (this was {repeat})"
    flaky = []
    for label in CRITICAL:
        records = [step["status"] for step in steps if step["label"] == label]
        passes = sum(1 for status in records if status == "pass")
        if records and passes != repeat:
            flaky.append(f"{label} ({passes}/{repeat})")
    return ("fail", "; ".join(flaky)) if flaky else ("pass", f"{repeat} runs")


MARKS = {"pass": "✓", "fail": "✗", "inconclusive": "⚠", "not run": "·", "manual": "☐"}


def format_criteria(rows: list[tuple[str, str, str, str]]) -> list[str]:
    width = max(len(area) for area, *_ in rows) + 2
    lines = ["Exit criteria for real-Mac validation"]
    for area, requirement, status, detail in rows:
        lines.append(f"  {MARKS[status]} {area:<{width}}{requirement} — {status}" + (f": {detail}" if detail else ""))
    return lines


# ---------------------------------------------------------------------------------------------
# the report
# ---------------------------------------------------------------------------------------------
def environment(run: Callable[..., Any] = subprocess.run) -> dict[str, Any]:
    """Where this pass ran: enough to tell one machine's numbers from another's."""
    chip = ""
    with contextlib.suppress(Exception):
        chip = run(["sysctl", "-n", "machdep.cpu.brand_string"], capture_output=True, text=True,
                   timeout=5).stdout.strip()
    return {"macos": platform.mac_ver()[0], "machine": platform.machine(), "chip": chip,
            "python": sys.version.split()[0], "platform": platform.platform()}


def build_report(recorder: Recorder, *, argv: list[str], repeat: int, ok: bool) -> dict[str, Any]:
    rows = evaluate(recorder.steps, recorder.runs, repeat)
    return {
        "schema": 1,
        "when": datetime.now().astimezone().isoformat(timespec="seconds"),
        "argv": argv,
        "repeat": repeat,
        "ok": ok,
        "environment": environment(),
        "criteria": [{"area": area, "requirement": requirement, "status": status, "detail": detail}
                     for area, requirement, status, detail in rows],
        "timings": summarise(recorder.runs),
        "findings": recorder.extra,
        "steps": recorder.steps,
        "runs": recorder.runs,
    }
