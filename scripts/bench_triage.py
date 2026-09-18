#!/usr/bin/env python3
"""V1.3 architecture benchmark: A' (1B triage) vs B (unified 8B) vs the current
V1.2 path — against real Ollama models, not FakeProvider.

This is a standalone measurement tool. It does not import or exercise the
orchestrator, the tool registry, the UI, voice, or memory, and it never writes
to the real ~/JARVIS workspace. It imports three existing, UNMODIFIED classes
read-only — Router, Understanding, Personality, ConversationState — and calls
their real public methods directly, so the "V1.2 baseline" numbers reflect the
actual current code path, not a re-implementation of it. Architectures A' and
B are both defined entirely inside this file, per the V1.3 audit's instruction
not to integrate either into the application yet.

Usage (on a Mac with `ollama serve` running and the models pulled):

    PYTHONPATH=backend ./.venv/bin/python scripts/bench_triage.py
    PYTHONPATH=backend ./.venv/bin/python scripts/bench_triage.py --reps 5
    PYTHONPATH=backend ./.venv/bin/python scripts/bench_triage.py --skip-baseline

Writes a console report and, by default, a JSON artifact next to this script
(--output to change or --output - to disable) so results can be reviewed or
handed back for analysis without re-running.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from jarvis.core.config import Config
from jarvis.core.personality import Personality
from jarvis.core.telemetry import Telemetry
from jarvis.intelligence.entities import ReferenceResolver
from jarvis.intelligence.schema import Objective
from jarvis.intelligence.schema import load as load_objective
from jarvis.intelligence.state import ConversationState
from jarvis.intelligence.understanding import Understanding
from jarvis.models.base import ChatMessage, extract_json
from jarvis.models.registry import ModelRouter, Slot
from jarvis.router.router import Router
from pydantic import BaseModel, Field, ValidationError, model_validator

CALL_TIMEOUT_S = 30.0


# ===========================================================================
# corpus — exactly the phrases given in the authorization, organised by
# category, with the two explicit context-sensitivity pairs
# ===========================================================================
@dataclass
class Case:
    text: str
    category: str
    expected_mode: str | None  # None = scored qualitatively, not for accuracy
    seed: str = ""  # which _seed_* function builds this case's ConversationState


CHAT = [
    "Hey Jarvis, how are you today?",
    "Hello",
    "Who are you?",
    "Yo, what's going on?",
    "I hate dealing with email.",
    "Safari is really slow today.",
    "I was reading about calendars.",
    "Files are getting messy.",
    "I've been thinking about buying a Mac.",
    "Tell me about specialised cells.",
]

ACTION = [
    "Check my new emails.",
    "Open Safari.",
    "Search Safari for dog pictures.",
    "Open Safari and search for dog pictures.",
    "Draft an email to my brother.",
    "Find the current price of the MacBook Pro.",
    "Take a screenshot.",
    "What is currently on my screen?",
    "Put specialised cells into the search bar.",
    "Open BBC.",
]

CASES: list[Case] = [Case(t, "chat", "chat") for t in CHAT]
CASES += [Case(t, "action", "action") for t in ACTION]
CASES += [
    Case("Email him about that.", "ambiguous_reference", "action"),
    Case("Open the second one.", "ambiguous_reference", "action", seed="search"),
    Case("Search it.", "ambiguous_reference", "action", seed="search_antecedent"),
    Case("Search it.", "ambiguous_reference", None, seed="none"),  # no-antecedent companion
    Case("Do that again.", "ambiguous_reference", "action", seed="prior_action"),
]
CASES += [
    Case(t, "clarification_underspecified", None)  # no single right answer in a vacuum
    for t in ("Do that.", "Do it.", "Go ahead.", "Send it.")
]


def _seed_state(seed: str) -> ConversationState:
    """Build context using the REAL ConversationState, exactly as V1.2's
    registry observer would today — including today's real limitation (a
    single current page, not itemised results; see V1.3 audit §4)."""
    state = ConversationState()
    state.begin_turn("(seed)")
    if seed == "search":
        state.note_observation(
            "browse_to", {"query": "dog pictures"}, True,
            "Opening a search for dog pictures.",
            {"url": "https://www.google.com/search?q=dog+pictures",
             "title": "dog pictures - Google Search"}, "browser")
    elif seed == "search_antecedent":
        state.note_observation(
            "search_web", {"query": "dog pictures"}, True, "6 results for dog pictures.",
            {"query": "dog pictures", "results": [
                {"title": "50 Best Dog Photos", "url": "https://a.example"},
                {"title": "Dog Pictures - Getty", "url": "https://b.example"}]}, "research")
    elif seed == "prior_action":
        state.note_observation(
            "open_application", {"name": "Safari"}, True, "Opening Safari.",
            {"application": "Safari"}, "macos")
    return state


# ===========================================================================
# the structured decision both A' and B produce — defined only in this file,
# per the authorization ("implement B only inside the benchmark")
# ===========================================================================
class TriageResult(BaseModel):
    mode: Literal["chat", "action", "clarification"] = "chat"
    confidence: float = 0.5
    action_evidence: list[str] = Field(default_factory=list)
    requires_tools: bool = False
    objective: Objective | None = None
    reply: str = ""
    reason: str = ""

    #: Set after validation if the "positive evidence" rule fired.
    evidence_correction_applied: bool = False

    @model_validator(mode="after")
    def _evidence_gates_action(self) -> TriageResult:
        if self.mode == "action" and not self.action_evidence:
            self.mode = "chat"
            self.objective = None
            self.evidence_correction_applied = True
        return self


_TRIAGE_PROMPT = """You decide what the user wants, in three modes only.

mode="chat": ordinary conversation, greetings, opinions, questions about you,
  discussing a topic, thinking aloud — even if it mentions email, files,
  Safari, calendars or other things JARVIS can act on. Mentioning a domain is
  NOT evidence of wanting an action in that domain. This is the default.
mode="action": the user is asking JARVIS to actually do something right now.
mode="clarification": action intent is clear but there is nothing concrete
  enough to act on, even with the context given.

Require POSITIVE evidence for mode="action": action_evidence must be short
literal phrases from the user's own words that justify it. If you cannot
point to such words, use mode="chat". Do not guess.

{context}

User said: "{text}"

Reply with JSON only:
{{"mode": "chat|action|clarification",
 "confidence": 0.0-1.0,
 "action_evidence": ["<literal phrase>", ...],
 "requires_tools": true|false,
 "objective": {{"goal": "...", "kind": "...", "targets": [...], "complexity": "trivial|simple|multi_step", "confidence": "confident|probable|ambiguous|impossible", "missing": [...]}} or null,
 "reply": "<only if mode=chat and you are asked to draft the reply here, else empty>",
 "reason": "<one short phrase>"}}"""

_UNIFIED_PROMPT = _TRIAGE_PROMPT + (
    "\n\nYou are the only model consulted this turn. When mode=\"chat\", also "
    "write the actual reply to the user in the \"reply\" field, in JARVIS's "
    "voice: precise, warm, a little formal, addressing the user as sir."
)


def _build_context(state: ConversationState | None) -> str:
    if state is None:
        return "(no prior context)"
    described = state.describe_for_model(include_turns=2)
    return described or "(no prior context)"


def _objective_sufficient(obj: Objective | None) -> bool:
    """Would this objective be usable directly, skipping a separate
    Understanding call? Mirrors the rule in the V1.3 audit §2/§4."""
    if obj is None:
        return False
    if not obj.goal.strip():
        return False
    if obj.confidence not in ("confident", "probable"):
        return False
    return not obj.missing


# ===========================================================================
# one measured call, either architecture
# ===========================================================================
@dataclass
class CallResult:
    ok: bool
    latency_ms: float
    raw_text: str = ""
    json_ok: bool = False
    result: TriageResult | None = None
    error: str = ""


async def _call_triage(models: ModelRouter, slot: str, prompt: str) -> CallResult:
    t0 = time.perf_counter()
    try:
        completion = await asyncio.wait_for(
            models.complete(
                slot,
                [ChatMessage("system", "You reply with JSON only, nothing else."),
                 ChatMessage("user", prompt)],
                json_mode=True, temperature=0.0, max_tokens=350,
            ),
            timeout=CALL_TIMEOUT_S,
        )
    except Exception as exc:
        return CallResult(ok=False, latency_ms=(time.perf_counter() - t0) * 1000.0,
                          error=f"{type(exc).__name__}: {exc}")

    latency_ms = (time.perf_counter() - t0) * 1000.0
    raw = completion.text
    data = extract_json(raw)
    if data is None:
        return CallResult(ok=False, latency_ms=latency_ms, raw_text=raw, json_ok=False,
                          error="no JSON object found in the response")
    # objective, if present, goes through the same repair-pass loader schema.py
    # already uses elsewhere, so a near-miss doesn't count as a hard failure.
    if isinstance(data.get("objective"), dict):
        data["objective"] = load_objective(Objective, data["objective"])
    try:
        result = TriageResult.model_validate(data)
    except ValidationError as exc:
        return CallResult(ok=False, latency_ms=latency_ms, raw_text=raw, json_ok=True,
                          error=f"schema validation failed: {exc.error_count()} error(s)")
    return CallResult(ok=True, latency_ms=latency_ms, raw_text=raw, json_ok=True, result=result)


# ===========================================================================
# V1.2 baseline — the REAL, unmodified router + understanding (+ a replicated
# _converse()-shaped call for the chat case) called directly
# ===========================================================================
@dataclass
class BaselineResult:
    label: str
    text: str
    route_path: str
    route_kind: str
    route_name: str
    route_latency_ms: float
    understanding_ran: bool
    understanding_latency_ms: float
    conversation_latency_ms: float | None
    total_latency_ms: float
    total_model_calls: int


async def _run_baseline_case(models: ModelRouter, config: Config, telemetry: Telemetry,
                             label: str, text: str, *, is_chat: bool) -> BaselineResult:
    router = Router(models, telemetry)
    t_total0 = time.perf_counter()

    decision = await router.route(text, context="", allow_model=True)
    calls = 1 if decision.path == "model" else 0

    understanding_ran = False
    understanding_ms = 0.0
    if decision.path != "quick":
        # This is what the agent loop does unconditionally today, regardless
        # of whether the request turns out to need a tool at all.
        state = ConversationState()
        t0 = time.perf_counter()
        await Understanding(models, ReferenceResolver(), slot=Slot.REASONING).understand(
            text, state)
        understanding_ms = (time.perf_counter() - t0) * 1000.0
        understanding_ran = True
        calls += 1

    conversation_ms: float | None = None
    if is_chat and decision.path != "quick":
        # Replicates _converse()'s exact call shape (agent.py) without
        # constructing a full IntelligenceAgent/Deps.
        persona = Personality(config).system_prompt("")
        t0 = time.perf_counter()
        parts = []
        async for delta in models.stream(Slot.REASONING,
                                         [ChatMessage("system", persona),
                                          ChatMessage("user", text)],
                                         max_tokens=200):
            parts.append(delta)
        conversation_ms = (time.perf_counter() - t0) * 1000.0
        calls += 1

    total_ms = (time.perf_counter() - t_total0) * 1000.0
    return BaselineResult(
        label=label, text=text, route_path=decision.path, route_kind=decision.kind,
        route_name=decision.name, route_latency_ms=round(decision.latency_ms, 1),
        understanding_ran=understanding_ran, understanding_latency_ms=round(understanding_ms, 1),
        conversation_latency_ms=round(conversation_ms, 1) if conversation_ms else None,
        total_latency_ms=round(total_ms, 1), total_model_calls=calls,
    )


# ===========================================================================
# runner
# ===========================================================================
async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:11434")
    parser.add_argument("--fast-model", default="llama3.2:1b")
    parser.add_argument("--general-model", default="llama3.1:8b")
    parser.add_argument("--reps", type=int, default=3)
    parser.add_argument("--skip-baseline", action="store_true")
    parser.add_argument("--output", default=str(Path(__file__).with_name("bench_triage_results.json")),
                        help="write raw results as JSON; pass '-' to disable")
    args = parser.parse_args()

    config = Config()
    config.models.providers["ollama"].base_url = args.base_url
    config.models.fast.model = args.fast_model
    config.models.general.model = args.general_model
    # reasoning stays "" (defers to general) — the real production default.

    telemetry = Telemetry()
    models = ModelRouter(config, telemetry)

    print(f"Checking Ollama at {args.base_url} ...")
    ollama = models.providers.get("ollama")
    reachable = await ollama.available() if ollama else False
    if not reachable:
        print(
            "\nOllama is not reachable at this address.\n"
            "This benchmark requires a running local Ollama server with the "
            "configured models pulled — it cannot run without one, and this "
            "script will not substitute simulated numbers.\n\n"
            "If you are seeing this from a Claude Code session that is not "
            "actually running on your Mac (e.g. a cloud/remote session), that "
            "is the reason: this container has no network path to your Mac's "
            "Ollama instance. Run this script in a terminal on your Mac "
            "instead, with `ollama serve` running:\n\n"
            f"    PYTHONPATH=backend ./.venv/bin/python {Path(__file__).relative_to(Path.cwd()) if Path(__file__).is_relative_to(Path.cwd()) else __file__}\n"
        )
        return 1

    installed = await models.installed_models("ollama")
    print(f"Ollama reachable. Installed models: {installed or '(none reported)'}")
    for wanted in (args.fast_model, args.general_model):
        if installed and wanted not in installed:
            print(f"  WARNING: {wanted!r} is not in the installed list — "
                 "the call will fail or Ollama will substitute something.")

    results: dict[str, Any] = {"config": vars(args), "cases": []}

    # -- V1.2 baseline -------------------------------------------------
    baseline_summaries: list[BaselineResult] = []
    if not args.skip_baseline:
        print("\n--- V1.2 baseline (real Router + Understanding, unmodified) ---")
        baseline_cases = [
            ("ordinary chat", "Hey Jarvis, how are you today?", True),
            ("simple action", "Search Safari for dog pictures.", False),
            ("complex action", "Find the current price of the MacBook Pro.", False),
        ]
        for label, text, is_chat in baseline_cases:
            for _ in range(max(1, args.reps // 2 or 1)):
                r = await _run_baseline_case(models, config, telemetry, label, text,
                                             is_chat=is_chat)
                baseline_summaries.append(r)
                print(f"  [{label}] {text!r} -> route={r.route_path}:{r.route_kind}:{r.route_name} "
                     f"({r.route_latency_ms}ms) understanding={r.understanding_ran} "
                     f"({r.understanding_latency_ms}ms) conversation={r.conversation_latency_ms} "
                     f"total={r.total_latency_ms}ms calls={r.total_model_calls}")
        results["baseline"] = [vars(b) for b in baseline_summaries]

    # -- A' and B --------------------------------------------------------
    print(f"\n--- A' (fast/{args.fast_model}) and B (reasoning/{args.general_model}) "
         f"— {len(CASES)} cases x {args.reps} reps each ---")
    for case in CASES:
        state = _seed_state(case.seed) if case.seed else None
        context = _build_context(state)
        for arch, slot, prompt_template in (("A_prime", Slot.FAST, _TRIAGE_PROMPT),
                                            ("B", Slot.REASONING, _UNIFIED_PROMPT)):
            for rep in range(args.reps):
                prompt = prompt_template.format(context=context, text=case.text)
                call = await _call_triage(models, slot, prompt)
                entry = {
                    "text": case.text, "category": case.category,
                    "expected_mode": case.expected_mode, "seed": case.seed,
                    "architecture": arch, "rep": rep, "ok": call.ok,
                    "json_ok": call.json_ok, "latency_ms": round(call.latency_ms, 1),
                    "error": call.error,
                }
                if call.result is not None:
                    entry.update({
                        "mode": call.result.mode,
                        "confidence": call.result.confidence,
                        "action_evidence": call.result.action_evidence,
                        "requires_tools": call.result.requires_tools,
                        "objective_sufficient": _objective_sufficient(call.result.objective),
                        "objective_goal": call.result.objective.goal if call.result.objective else "",
                        "evidence_correction_applied": call.result.evidence_correction_applied,
                        "reply_len": len(call.result.reply),
                    })
                results["cases"].append(entry)
                tag = "OK " if call.ok else "ERR"
                mode = call.result.mode if call.result else "-"
                print(f"  [{tag}] {arch:8} {case.category:24} {case.text[:42]!r:44} "
                     f"mode={mode:6} {call.latency_ms:7.0f}ms" +
                     (f"  ({call.error})" if not call.ok else ""))

    if args.output != "-":
        Path(args.output).write_text(json.dumps(results, indent=2, default=str))
        print(f"\nRaw results written to {args.output}")

    _print_report(results)
    return 0


def _print_report(results: dict[str, Any]) -> None:
    cases = results["cases"]
    print("\n" + "=" * 72)
    print("SUMMARY")
    print("=" * 72)
    for arch in ("A_prime", "B"):
        rows = [c for c in cases if c["architecture"] == arch]
        scored = [c for c in rows if c["expected_mode"] is not None]
        correct = [c for c in scored if c.get("mode") == c["expected_mode"]]
        latencies = sorted(c["latency_ms"] for c in rows if c["ok"])
        json_fail = sum(1 for c in rows if not c["json_ok"])
        action_rows = [c for c in rows if c.get("mode") == "action"]
        sufficient = [c for c in action_rows if c.get("objective_sufficient")]
        corrections = sum(1 for c in rows if c.get("evidence_correction_applied"))

        def pct(n: int, d: int) -> str:
            return f"{100 * n / d:.0f}%" if d else "n/a"

        def p(vals: list[float], q: float) -> str:
            if not vals:
                return "n/a"
            idx = min(len(vals) - 1, int(len(vals) * q))
            return f"{vals[idx]:.0f}ms"

        print(f"\n{arch}")
        print(f"  accuracy (scored cases): {pct(len(correct), len(scored))} "
             f"({len(correct)}/{len(scored)})")
        print(f"  JSON failure rate: {pct(json_fail, len(rows))} ({json_fail}/{len(rows)})")
        print(f"  evidence-validator corrections: {corrections}")
        print(f"  p50 latency: {p(latencies, 0.50)}   p95: {p(latencies, 0.95)}")
        print(f"  action cases with a skip-Understanding-worthy objective: "
             f"{pct(len(sufficient), len(action_rows))} ({len(sufficient)}/{len(action_rows)})")

    print("\nClarification/underspecified cases (no single right answer — raw outputs):")
    for c in cases:
        if c["category"] != "clarification_underspecified" or c["rep"] != 0:
            continue
        print(f"  {c['architecture']:8} {c['text']!r:16} -> mode={c.get('mode','-'):6} "
             f"evidence={c.get('action_evidence')}")

    print("\nPer-case mode stability (did repeated runs agree?):")
    seen: set[tuple[str, str, str]] = set()
    for c in cases:
        key = (c["text"], c["seed"], c["architecture"])
        if key in seen:
            continue
        seen.add(key)
        modes = {x.get("mode") for x in cases
                 if x["text"] == c["text"] and x["seed"] == c["seed"]
                 and x["architecture"] == c["architecture"]}
        if len(modes) > 1:
            print(f"  UNSTABLE  {c['architecture']:8} {c['text']!r} -> {modes}")


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
