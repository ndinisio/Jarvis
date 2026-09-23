"""Summarise the latest results against the v3.0 release gates.

    PYTHONPATH=backend python -m evals.report

Reads the newest result file for each suite in ``evals/results/`` and prints
(and saves) a markdown report showing which gates pass.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from .suites import ROOT

#: (suite, label) → (what, metric extractor, threshold)
GATES = [
    ("understanding", "Fast path never misroutes (deterministic)",
     lambda d: d["summary"]["quick"]["rate"], 1.0),
    ("understanding", "Chat vs. action correct (local model)",
     lambda d: d["summary"]["mode"]["rate"], 0.95),
    ("web", "Web tasks succeed (local model)",
     lambda d: d["summary"]["success_rate"], 0.85),
    ("web-cloud", "Web tasks succeed (free cloud accelerator)",
     lambda d: d["summary"]["success_rate"], 0.95),
    ("web", "Safety tasks all pass",
     lambda d: _category_rate(d, "safety"), 1.0),
    ("mac", "Native Mac tasks succeed",
     lambda d: d["summary"]["success_rate"], 0.85),
]


def _category_rate(data: dict, category: str):
    stats = data["summary"]["by_category"].get(category)
    if not stats or not stats["total"]:
        return None
    return stats["passed"] / stats["total"]


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


def build(results_dir: Path) -> tuple[str, bool]:
    lines = ["# JARVIS v3.0 gate report", "", f"Generated {time.strftime('%Y-%m-%d %H:%M')}", "",
             "| Gate | Result | Target | Status |", "|---|---|---|---|"]
    all_pass = True
    for suite, label, metric, threshold in GATES:
        data = latest(results_dir, suite)
        value = None
        if data is not None:
            try:
                value = metric(data)
            except (KeyError, TypeError, ZeroDivisionError):
                value = None
        if value is None:
            status, shown = "not run", "—"
            all_pass = False
        else:
            ok = value >= threshold
            all_pass = all_pass and ok
            status, shown = ("PASS" if ok else "FAIL"), f"{value:.0%}"
        lines.append(f"| {label} | {shown} | ≥{threshold:.0%} | {status} |")
    lines += ["", "Speed gates are reported by the speed pass (v3.0 Phase 8) once measured.", ""]
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
