"""The dedicated, repeated Amazon add-to-basket benchmark.

    PYTHONPATH=backend python -m evals.run_amazon_basket --model oracle
    PYTHONPATH=backend python -m evals.run_amazon_basket --model real \
        --set models.operator.provider=groq --set models.operator.model=qwen/qwen3-32b \
        --label groq-qwen3-32b

Unlike the aggregate `shop` category inside `run_web.py` (which exercises
Amazon among six other sites to check routing/coverage broadly), this script
exists to answer one focused question with enough repetitions to mean
something: *given a real model, how reliably and how fast does JARVIS find
the right product on Amazon, pick the right variant/quantity, and get it
into the basket — verified against the mock site's own ground truth, not
just "the click succeeded"?*

It adds no Amazon-specific logic anywhere in `jarvis/` — it just runs the
existing `shop`/`shop-compare` add-to-basket tasks (the same recipe,
`amazon-add-to-basket`, and the same mock site everything else uses) enough
times, over enough different products/variants/quantities/phrasings, for
the aggregate numbers to be a real reliability measurement rather than one
demo run. See plan section 10 for the full metrics rationale.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import resource
import sys
import time
from pathlib import Path
from typing import Any

from .harness import Harness, TaskResult
from .run_web import parse_overrides
from .suites import ROOT, WebTask, load_web_tasks


def basket_family(tasks: list[WebTask]) -> list[WebTask]:
    """The add-to-basket task family: a fresh basket, checked against the
    mock site's real cart state (`basket_has`/`basket_has_any` read
    `/__state` directly — see checks.py). Excludes `shop-remove-kettle`
    (a pre-seeded basket, a removal task, not an addition) and anything
    outside the `shop`/`shop-compare` categories (e.g. `shop-deals`, which
    is a nav task that happens to live in the same file)."""
    chosen = []
    for task in tasks:
        if not task.category.startswith("shop") or task.setup:
            continue
        if any("basket_has" in check or "basket_has_any" in check for check in task.checks):
            chosen.append(task)
    return chosen


def _p(values: list[float], pct: float) -> float:
    values = sorted(values)
    if not values:
        return 0.0
    index = min(len(values) - 1, int(round(pct * (len(values) - 1))))
    return values[index]


def _mean(values: list[float]) -> float:
    return round(sum(values) / len(values), 3) if values else 0.0


async def run(args) -> list[tuple[TaskResult, bool]]:
    """Runs the family and returns each result paired with whether *some*
    item ended up in the basket even though the task failed its checks — a
    wrong product/variant/quantity, not merely nothing happening at all."""
    tasks = basket_family(load_web_tasks(Path(args.suite) if args.suite else None))
    if not tasks:
        raise RuntimeError("no add-to-basket tasks found — has web_tasks.yaml changed shape?")
    runs: list[tuple[WebTask, str]] = []
    for _ in range(args.repeat):
        for task in tasks:
            for phrasing in (task.phrasings if args.phrasings == "all" else task.phrasings[:1]):
                runs.append((task, phrasing))

    results: list[tuple[TaskResult, bool]] = []
    async with Harness(model=args.model, config_path=Path(args.config) if args.config else None,
                       overrides=parse_overrides(args.set), headless=not args.headed,
                       channel=args.channel, task_timeout_s=args.timeout) as harness:
        for index, (task, phrasing) in enumerate(runs):
            if index and args.task_gap > 0:
                # Outside the task's own wall time: lets a rate-limited tier
                # (Groq free: 8k tokens/min) refill instead of the run
                # measuring the quota rather than the model.
                await asyncio.sleep(args.task_gap)
            result = await harness.run_task(task, phrasing)
            state = harness.server.state()
            wrong_action = bool(state["amazon"]["cart"]) and not result.ok
            results.append((result, wrong_action))
            mark = "PASS" if result.ok else ("WRONG" if wrong_action else "FAIL")
            print(f"{mark:<5} {task.id:<28} {result.wall_s:6.1f}s  tools={result.tool_calls:<3} "
                  f"models={result.model_calls:<3} replans={result.replans} "
                  f"dup={result.duplicate_actions} {phrasing[:44]}", flush=True)
            if not result.ok and args.verbose:
                for failure in result.failures:
                    print(f"      - {failure}")
    return results


def summarise(results: list[tuple[TaskResult, bool]]) -> dict[str, Any]:
    records = [r for r, _ in results]
    total = len(records)
    passed = sum(r.ok for r in records)
    walls = [r.wall_s for r in records]
    false_completions = sum(1 for r in records if r.task_claimed_success and not r.ok)
    never_attempted = sum(1 for r in records if r.task_claimed_success is None and not r.ok)
    wrong_actions = sum(1 for _, wrong in results if wrong)
    usage = resource.getrusage(resource.RUSAGE_SELF)
    return {
        "total": total,
        "passed": passed,
        "success_rate": round(passed / total, 3) if total else 0.0,
        "p50_wall_s": _p(walls, 0.5),
        "p95_wall_s": _p(walls, 0.95),
        "mean_model_calls": _mean([r.model_calls for r in records]),
        "mean_tool_calls": _mean([r.tool_calls for r in records]),
        "mean_replans": _mean([r.replans for r in records]),
        "mean_wrong_actions": round(wrong_actions / total, 3) if total else 0.0,
        "wrong_actions_count": wrong_actions,
        "mean_duplicate_actions": _mean([r.duplicate_actions for r in records]),
        "false_completions": false_completions,
        "never_attempted": never_attempted,
        "mean_human_interventions": _mean([len(r.confirmations) for r in records]),
        # Peak resident set size for this harness process across the whole
        # run. Meaningful as "local resource consumption" only when
        # --model real is also running the model locally (e.g. Ollama) —
        # for a cloud-provider run this mostly reflects the browser +
        # harness overhead, since inference happens on the provider's own
        # hardware; read it as the latency/network figure instead there.
        # ru_maxrss is kilobytes on Linux but bytes on macOS (BSD-derived).
        "peak_rss_mb": round(usage.ru_maxrss / (1024.0 if sys.platform == "linux" else 1024.0 * 1024.0), 1),
        "cpu_user_s": round(usage.ru_utime, 1),
        "cpu_sys_s": round(usage.ru_stime, 1),
    }


def render(summary: dict[str, Any], label: str) -> str:
    rows = [
        ("Success rate (verified basket state)", f"{summary['success_rate']:.0%} ({summary['passed']}/{summary['total']})"),
        ("Median latency (p50 wall time)", f"{summary['p50_wall_s']:.1f} s"),
        ("Tail latency (p95 wall time)", f"{summary['p95_wall_s']:.1f} s"),
        ("Model calls (mean/task)", summary["mean_model_calls"]),
        ("Tool calls (mean/task)", summary["mean_tool_calls"]),
        ("Retries/replans (mean/task)", summary["mean_replans"]),
        ("Wrong actions (mean/task)", f"{summary['mean_wrong_actions']} ({summary['wrong_actions_count']} runs)"),
        ("Duplicate actions (mean/task)", summary["mean_duplicate_actions"]),
        ("False completions (claimed done, basket wrong)", summary["false_completions"]),
        ("Never attempted (misrouted, no task ran)", summary["never_attempted"]),
        ("Human interventions (mean confirmations/task)", summary["mean_human_interventions"]),
        ("Peak RSS (this process)", f"{summary['peak_rss_mb']:.1f} MB"),
        ("CPU time, user+sys (this process)", f"{summary['cpu_user_s'] + summary['cpu_sys_s']:.1f} s"),
    ]
    width = max(len(label) for label, _ in rows)
    lines = [f"Amazon add-to-basket benchmark — {label}", "=" * (len(label) + 32)]
    lines += [f"{name:<{width}}  {value}" for name, value in rows]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--model", choices=["oracle", "real"], default="real")
    parser.add_argument("--suite", default="")
    parser.add_argument("--repeat", type=int, default=1,
                        help="how many times to cycle the whole task family (default 1; the family "
                             "itself has 16 tasks x 2-3 phrasings, so --phrasings all --repeat 1 "
                             "already clears the planned 20-30 repeated runs)")
    parser.add_argument("--phrasings", choices=["first", "all"], default="all")
    parser.add_argument("--config", default="")
    parser.add_argument("--set", action="append", default=[], metavar="KEY=VALUE")
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--channel", default=None)
    parser.add_argument("--timeout", type=float, default=240.0)
    parser.add_argument("--task-gap", type=float, default=0.0, metavar="SECONDS",
                        help="sleep between tasks (not counted in latency) to stay under a "
                             "provider's tokens-per-minute limit")
    parser.add_argument("--out", default="")
    parser.add_argument("--label", default="", help="tag this configuration, e.g. 'groq-qwen3-32b'")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)

    started = time.time()
    results = asyncio.run(run(args))
    summary = summarise(results)
    label = args.label or args.model
    print()
    print(render(summary, label))

    out = Path(args.out) if args.out else (
        ROOT / "results" / f"amazon-basket-{args.model}-{time.strftime('%Y%m%d-%H%M%S')}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({
        "suite": "amazon-basket", "model": args.model, "label": label,
        "overrides": parse_overrides(args.set),
        "started": started, "duration_s": round(time.time() - started, 1),
        "summary": summary,
        "results": [{**r.as_dict(), "wrong_action": wrong} for r, wrong in results],
    }, indent=2), encoding="utf-8")
    print(f"\nresults written to {out}")
    return 0 if summary["passed"] == summary["total"] else 1


if __name__ == "__main__":
    sys.exit(main())
