"""Run the understanding corpus.

    PYTHONPATH=backend python -m evals.run_understanding            # fast path only, no model
    PYTHONPATH=backend python -m evals.run_understanding --model real

The deterministic stage checks every ``quick`` expectation against the fast
path (no model needed — this is what the test suite gates on). With
``--model real`` each utterance the fast path lets through is also sent to
the semantic stage that decides chat vs. action, using your configured
models, and scored against ``mode``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from jarvis.router.quick import QuickCommands
from jarvis.router.router import Router

from .suites import ROOT, Utterance, load_utterances


@dataclass
class UtteranceResult:
    text: str
    tags: list[str]
    mode: str
    quick_expected: str | None
    quick_actual: str | None
    quick_ok: bool | None
    mode_actual: str | None = None
    mode_ok: bool | None = None
    norm_expected: str | None = None
    norm_actual: str | None = None
    norm_ok: bool | None = None
    latency_ms: float = 0.0
    detail: dict = field(default_factory=dict)


def fast_path(text: str) -> str | None:
    """The name the deterministic front door routes *text* to, if any."""
    decision = QuickCommands().match(text) or Router._arithmetic(text)
    return decision.name if decision is not None else None


def check_quick(utterance: Utterance) -> UtteranceResult:
    actual = fast_path(utterance.text)
    expected = utterance.quick
    if expected is None:
        ok = None
    elif expected == "none":
        ok = actual is None
    else:
        ok = actual == expected
    return UtteranceResult(text=utterance.text, tags=utterance.tags, mode=utterance.mode,
                           quick_expected=expected, quick_actual=actual, quick_ok=ok,
                           norm_expected=utterance.norm)


async def semantic_pass(results: list[UtteranceResult], config_path: Path | None,
                        overrides: dict) -> None:
    """Score chat-vs-action on everything the fast path didn't take."""
    from jarvis.core.app import JarvisApp
    from jarvis.core.config import ConfigStore, load_config
    from jarvis.intelligence.state import ConversationState
    from jarvis.intelligence.triage import IntentTriage

    from .harness import apply_overrides

    with tempfile.TemporaryDirectory(prefix="jarvis-understanding-") as tmp:
        config = apply_overrides(load_config(config_path), overrides)
        config.workspace = str(Path(tmp) / "JARVIS")
        config.log_level = "WARNING"
        config.voice.enabled = False
        config.voice.tts_engine = "off"
        config.ensure_workspace()
        app = JarvisApp(ConfigStore(config, Path(tmp) / "config.json"), enable_voice=False)
        triage = IntentTriage(app.models, config.intelligence.reasoning_slot)
        for result in results:
            if result.quick_actual is not None:
                result.mode_actual = "act" if result.quick_actual not in _CHAT_CONTROLS else "chat"
            else:
                started = time.perf_counter()
                decision = await triage.decide(result.text, ConversationState())
                result.latency_ms = round((time.perf_counter() - started) * 1000.0, 1)
                result.mode_actual = "act" if decision.mode == "action" else "chat"
                result.detail = {"reason": decision.reason, "confidence": decision.confidence}
            result.mode_ok = result.mode_actual == result.mode
            mark = "ok " if result.mode_ok else "BAD"
            print(f"{mark} {result.mode:<4}→{result.mode_actual:<4} {result.latency_ms:7.0f}ms  {result.text[:70]}",
                  flush=True)
        await app.shutdown()


#: Fast-path control names that are conversation rather than an action.
_CHAT_CONTROLS = {"greeting", "thanks", "presence", "farewell", "wake"}


def summarise(results: list[UtteranceResult]) -> dict:
    def rate(values):
        values = [v for v in values if v is not None]
        return {"passed": sum(values), "total": len(values),
                "rate": round(sum(values) / len(values), 3) if values else None}

    tags = sorted({tag for r in results for tag in r.tags})
    latencies = sorted(r.latency_ms for r in results if r.latency_ms)
    return {
        "utterances": len(results),
        "quick": rate(r.quick_ok for r in results),
        "mode": rate(r.mode_ok for r in results),
        "by_tag": {tag: {"quick": rate(r.quick_ok for r in results if tag in r.tags),
                         "mode": rate(r.mode_ok for r in results if tag in r.tags)} for tag in tags},
        "semantic_p50_ms": latencies[len(latencies) // 2] if latencies else None,
        "semantic_p95_ms": latencies[int(len(latencies) * 0.95)] if latencies else None,
    }


def print_summary(summary: dict) -> None:
    def fmt(stat: dict) -> str:
        return "—" if not stat["total"] else f"{stat['passed']}/{stat['total']} ({stat['rate']:.0%})"

    print(f"\n{'tag':<14} {'fast path':>16} {'chat/act':>16}")
    for tag, stats in summary["by_tag"].items():
        print(f"{tag:<14} {fmt(stats['quick']):>16} {fmt(stats['mode']):>16}")
    print(f"{'ALL':<14} {fmt(summary['quick']):>16} {fmt(summary['mode']):>16}")
    if summary["semantic_p50_ms"] is not None:
        print(f"semantic latency p50 {summary['semantic_p50_ms']:.0f} ms, "
              f"p95 {summary['semantic_p95_ms']:.0f} ms")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--model", choices=["none", "real"], default="none")
    parser.add_argument("--config", default="")
    parser.add_argument("--set", action="append", default=[], metavar="KEY=VALUE")
    parser.add_argument("--tag", default="", help="only utterances with this tag")
    parser.add_argument("--out", default="")
    parser.add_argument("--quiet", action="store_true", help="don't list quick-path failures")
    parser.add_argument("--label", default="")
    args = parser.parse_args(argv)

    utterances = [u for u in load_utterances() if not args.tag or args.tag in u.tags]
    results = [check_quick(u) for u in utterances]
    if not args.quiet:
        for result in results:
            if result.quick_ok is False:
                print(f"QUICK  expected {result.quick_expected!s:<22} got {result.quick_actual!s:<22} "
                      f"{result.text}")
    if args.model == "real":
        from .run_web import parse_overrides

        asyncio.run(semantic_pass(results, Path(args.config) if args.config else None,
                                  parse_overrides(args.set)))
    summary = summarise(results)
    print_summary(summary)
    out = Path(args.out) if args.out else (
        ROOT / "results" / f"understanding-{args.model}-{time.strftime('%Y%m%d-%H%M%S')}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"suite": "understanding", "model": args.model, "label": args.label,
                               "summary": summary,
                               "results": [asdict(r) for r in results]}, indent=2), encoding="utf-8")
    print(f"results written to {out}")
    quick = summary["quick"]
    return 0 if quick["total"] == quick["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
