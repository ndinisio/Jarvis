"""Run the end-to-end web suite.

    PYTHONPATH=backend python -m evals.run_web --model oracle
    PYTHONPATH=backend python -m evals.run_web --model real --set models.general.model=qwen3:8b

``--model real`` uses your own JARVIS configuration (and JARVIS_* environment
variables), so run it on the Mac with Ollama running. Results are written to
``evals/results/web-<model>-<timestamp>.json``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

from .harness import Harness, TaskResult
from .suites import ROOT, load_web_tasks


def parse_overrides(pairs: list[str]) -> dict:
    overrides = {}
    for pair in pairs:
        key, _, raw = pair.partition("=")
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            value = raw
        overrides[key.strip()] = value
    return overrides


def select(tasks, *, ids: str = "", category: str = "", model: str = "oracle",
           max_phase: int | None = None):
    wanted = {i.strip() for i in ids.split(",") if i.strip()}
    chosen = []
    for task in tasks:
        if wanted and task.id not in wanted:
            continue
        if category and task.category != category:
            continue
        if model == "oracle" and "real-only" in task.tags:
            continue
        if model == "real" and "oracle-only" in task.tags:
            continue
        if max_phase is not None and task.target_phase > max_phase:
            continue
        chosen.append(task)
    return chosen


async def run(args) -> list[TaskResult]:
    tasks = select(load_web_tasks(Path(args.suite) if args.suite else None), ids=args.tasks,
                   category=args.category, model=args.model, max_phase=args.max_phase)
    results: list[TaskResult] = []
    async with Harness(model=args.model, config_path=Path(args.config) if args.config else None,
                       overrides=parse_overrides(args.set), headless=not args.headed,
                       channel=args.channel, task_timeout_s=args.timeout) as harness:
        for task in tasks:
            phrasings = task.phrasings if args.phrasings == "all" else task.phrasings[:1]
            for phrasing in phrasings:
                result = await harness.run_task(task, phrasing)
                results.append(result)
                mark = "PASS" if result.ok else "FAIL"
                print(f"{mark}  {task.id:<28} {result.wall_s:6.1f}s  tools={result.tool_calls:<3} "
                      f"models={result.model_calls:<3} {phrasing[:60]}", flush=True)
                if not result.ok and args.verbose:
                    for failure in result.failures:
                        print(f"      - {failure}")
                    print(f"      route: {result.route}; tools: {', '.join(result.tools[-8:])}")
    return results


def summarise(results: list[TaskResult]) -> dict:
    total = len(results)
    passed = sum(r.ok for r in results)
    by_category: dict[str, list[int]] = {}
    for r in results:
        stats = by_category.setdefault(r.category, [0, 0])
        stats[0] += r.ok
        stats[1] += 1
    walls = sorted(r.wall_s for r in results if r.ok) or [0.0]
    return {
        "total": total, "passed": passed,
        "success_rate": round(passed / total, 3) if total else 0.0,
        "by_category": {k: {"passed": v[0], "total": v[1]} for k, v in sorted(by_category.items())},
        "p50_wall_s_passed": walls[len(walls) // 2],
        "mean_model_calls": round(sum(r.model_calls for r in results) / total, 2) if total else 0.0,
        "mean_tool_calls": round(sum(r.tool_calls for r in results) / total, 2) if total else 0.0,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--model", choices=["oracle", "real"], default="oracle")
    parser.add_argument("--suite", default="")
    parser.add_argument("--tasks", default="", help="comma-separated task ids")
    parser.add_argument("--category", default="")
    parser.add_argument("--max-phase", type=int, default=None,
                        help="only tasks whose target_phase is at most this")
    parser.add_argument("--phrasings", choices=["first", "all"], default="first")
    parser.add_argument("--config", default="", help="JARVIS config.json (real model only)")
    parser.add_argument("--set", action="append", default=[], metavar="KEY=VALUE",
                        help="config override, e.g. models.general.model=qwen3:8b")
    parser.add_argument("--headed", action="store_true", help="show the browser window")
    parser.add_argument("--channel", default=None, help="e.g. chrome, to use installed Chrome")
    parser.add_argument("--timeout", type=float, default=240.0, help="per-task timeout (s)")
    parser.add_argument("--out", default="", help="results file (default evals/results/…)")
    parser.add_argument("--label", default="",
                        help="tag this run, e.g. 'cloud' when using a free cloud accelerator")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)

    started = time.time()
    results = asyncio.run(run(args))
    summary = summarise(results)
    print(json.dumps(summary, indent=2))

    out = Path(args.out) if args.out else (
        ROOT / "results" / f"web-{args.model}-{time.strftime('%Y%m%d-%H%M%S')}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({
        "suite": "web", "model": args.model, "label": args.label,
        "overrides": parse_overrides(args.set),
        "started": started, "duration_s": round(time.time() - started, 1),
        "summary": summary, "results": [r.as_dict() for r in results],
    }, indent=2), encoding="utf-8")
    print(f"results written to {out}")
    return 0 if summary["passed"] == summary["total"] else 1


if __name__ == "__main__":
    sys.exit(main())
