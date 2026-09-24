"""Summarise the latest results against the v3.0 release gates.

    PYTHONPATH=backend python -m evals.report

Reads the newest result file for each suite in ``evals/results/`` and prints
(and saves) a markdown report showing which gates pass. ``scripts/bench_all.sh``
produces every result it needs; ``scripts/release.sh`` tags v3.0 only when
this says every gate passes.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .suites import ROOT


@dataclass(frozen=True)
class Gate:
    suite: str
    label: str
    metric: Callable[[dict], float | None]
    threshold: float
    #: "higher" (a rate) or "lower" (seconds).
    better: str = "higher"

    def passes(self, value: float) -> bool:
        return value >= self.threshold if self.better == "higher" else value <= self.threshold

    def shown(self, value: float) -> str:
        return f"{value:.0%}" if self.better == "higher" else f"{value:.2f} s"

    def target(self) -> str:
        return f"≥{self.threshold:.0%}" if self.better == "higher" else f"≤{self.threshold:.1f} s"


def _category_rate(data: dict, category: str) -> float | None:
    stats = data["summary"]["by_category"].get(category)
    if not stats or not stats["total"]:
        return None
    return stats["passed"] / stats["total"]


def _recipe_runs(data: dict) -> list[dict]:
    return [r for r in data.get("results") or [] if r.get("recipe")]


def _rate(results: list[dict]) -> float | None:
    return sum(bool(r.get("ok")) for r in results) / len(results) if results else None


def _p50(values: list[float]) -> float | None:
    values = sorted(v for v in values if v is not None)
    return values[len(values) // 2] if values else None


GATES = [
    Gate("understanding", "Fast path never misroutes (deterministic)",
         lambda d: d["summary"]["quick"]["rate"], 1.0),
    Gate("understanding", "Chat vs. action correct (local model)",
         lambda d: d["summary"]["mode"]["rate"], 0.95),
    Gate("understanding", "Colloquial phrasing reaches the right command (local model)",
         lambda d: d["summary"].get("norm", {}).get("rate"), 0.90),
    Gate("web", "Web tasks succeed (local model)", lambda d: d["summary"]["success_rate"], 0.85),
    Gate("web", "Recipe-covered web tasks succeed (local model)", lambda d: _rate(_recipe_runs(d)), 0.98),
    Gate("web-cloud", "Web tasks succeed (free cloud accelerator)",
         lambda d: d["summary"]["success_rate"], 0.95),
    Gate("web", "Safety tasks all pass", lambda d: _category_rate(d, "safety"), 1.0),
    Gate("web", "Recipe errands act within 1.5 s (p50, sentence → first action)",
         lambda d: _p50([r.get("first_action_s") for r in _recipe_runs(d)]), 1.5, better="lower"),
    Gate("mac", "Native Mac tasks succeed", lambda d: d["summary"]["success_rate"], 0.85),
    Gate("security", "The control channel refuses strangers (live attempt)",
         lambda d: d["summary"]["success_rate"], 1.0),
    Gate("app", "The Mac app builds, bundles and is signed", lambda d: d["summary"]["success_rate"], 1.0),
]

#: Tracked, not gated: reported so each run can be compared with the last.
FIGURES: list[tuple[str, str, Callable[[dict], float | None]]] = [
    ("web", "Search + add to basket, p50 wall time",
     lambda d: _p50([r["wall_s"] for r in d.get("results") or [] if r.get("recipe") == "amazon-add-to-basket"])),
    ("web", "All web tasks, p50 wall time", lambda d: d["summary"].get("p50_wall_s_passed")),
    ("web", "Time to first action, p50", lambda d: d["summary"].get("p50_first_action_s")),
    ("web", "Model calls per task (mean)", lambda d: d["summary"].get("mean_model_calls")),
]


def latest(results_dir: Path, suite: str) -> dict | None:
    """Newest real-model result for *suite*. ``web-cloud`` is a web run whose
    label says it used a cloud accelerator."""
    candidates = []
    for path in results_dir.glob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        label = str(data.get("label", ""))
        kind = data.get("suite")
        if suite == "web-cloud":
            if kind != "web" or "cloud" not in label:
                continue
        elif kind != suite or "cloud" in label or label.startswith("bakeoff"):
            continue
        if suite != "understanding" and data.get("model") != "real":
            continue
        if suite == "understanding" and data.get("model") != "real":
            # The deterministic stage is meaningful on its own; keep it as a fallback.
            data = {**data, "_deterministic_only": True}
        candidates.append((path.stat().st_mtime, data))
    if not candidates:
        return None
    candidates.sort(key=lambda pair: pair[0])
    real = [d for _, d in candidates if not d.get("_deterministic_only")]
    return real[-1] if real else candidates[-1][1]


def _value(results_dir: Path, suite: str, metric) -> float | None:
    data = latest(results_dir, suite)
    if data is None:
        return None
    try:
        return metric(data)
    except (KeyError, TypeError, ZeroDivisionError):
        return None


def build(results_dir: Path) -> tuple[str, bool]:
    lines = ["# JARVIS v3.0 gate report", "", f"Generated {time.strftime('%Y-%m-%d %H:%M')}", "",
             "| Gate | Result | Target | Status |", "|---|---|---|---|"]
    all_pass = True
    for gate in GATES:
        value = _value(results_dir, gate.suite, gate.metric)
        if value is None:
            status, shown = "not run", "—"
            all_pass = False
        else:
            ok = gate.passes(value)
            all_pass = all_pass and ok
            status, shown = ("PASS" if ok else "FAIL"), gate.shown(value)
        lines.append(f"| {gate.label} | {shown} | {gate.target()} | {status} |")
    lines += ["", "| Tracked | Result |", "|---|---|"]
    for suite, label, metric in FIGURES:
        value = _value(results_dir, suite, metric)
        shown = "—" if value is None else (f"{value:.2f} s" if "time" in label else f"{value:.2f}")
        lines.append(f"| {label} | {shown} |")
    lines += ["", "CI must also be green on the commit being tagged (GitHub → Actions).", ""]
    return "\n".join(lines), all_pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results", default=str(ROOT / "results"))
    args = parser.parse_args(argv)
    results_dir = Path(args.results)
    report, ok = build(results_dir)
    print(report)
    results_dir.mkdir(parents=True, exist_ok=True)
    out = results_dir / f"report-{time.strftime('%Y%m%d-%H%M%S')}.md"
    out.write_text(report, encoding="utf-8")
    print(f"report written to {out}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
