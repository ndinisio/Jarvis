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
    #: v2 schema: "clarification" removed (unused by either model in round 1 —
    #: see the benchmark report; an underspecified action is now expressed as
    #: mode="action" with objective_sufficient=False, not a third mode).
    mode: Literal["chat", "action"] = "chat"
    confidence: float = 0.5
    action_evidence: list[str] = Field(default_factory=list)
    requires_tools: bool = False
    objective: Objective | None = None
    reason: str = ""
    #: reply removed: triage routes, it does not draft the conversational
    #: answer — that call belongs to a separate, streamed step either way.

    #: Set after validation if the "positive evidence" rule fired.
    evidence_correction_applied: bool = False

    @model_validator(mode="after")
    def _evidence_gates_action(self) -> TriageResult:
        if self.mode == "action" and not self.action_evidence:
            self.mode = "chat"
            self.objective = None
            self.evidence_correction_applied = True
        return self


_TRIAGE_PROMPT = """You decide what the user wants. Two modes only.

mode="chat": ordinary conversation, greetings, opinions, questions about you,
  discussing a topic, thinking aloud — even if it mentions email, files,
  Safari, calendars or other things JARVIS can act on. Mentioning a domain is
  NOT evidence of wanting an action in that domain. This is the default:
  when in doubt, chat.
mode="action": the user is asking JARVIS to actually do something right now.
  This includes requests where something is missing (who to email, what to
  send) — say mode="action" and list what's missing in objective.missing
  rather than downgrading to chat just because a detail is absent.

Require POSITIVE evidence for mode="action": action_evidence must be short
literal phrases from the user's own words that justify it. If you cannot
point to such words, use mode="chat". Do not guess.

Examples of clear action requests (mode="action"):
  "Open Safari." -> action_evidence: ["open safari"]
  "Check my email." -> action_evidence: ["check my email"]
  "Take a screenshot." -> action_evidence: ["take a screenshot"]
These are genuinely being asked for, not merely mentioned in passing — that
distinction, not the presence of a domain word, is what "action" means.

{context}

User said: "{text}"

Reply with JSON only:
{{"mode": "chat|action",
 "confidence": 0.0-1.0,
 "action_evidence": ["open safari"],
 "requires_tools": true|false,
 "objective": {{"goal": "...", "kind": "...", "targets": [...], "complexity": "trivial|simple|multi_step", "confidence": "confident|probable|ambiguous|impossible", "missing": [...]}} or null,
 "reason": "<one short phrase>"}}"""


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


def _evidence_grounded(evidence: list[str], text: str) -> bool:
    """Is every claimed action_evidence phrase actually present, verbatim
    (case-insensitive), in what the user said?

    Round 1 showed a model can emit plausible-looking evidence — sometimes
    even literal prompt-template placeholder text — that doesn't correspond to
    anything in the input. This turns that manual read into a checkable fact:
    an empty evidence list on mode="chat" counts as vacuously grounded (there
    is nothing ungrounded to claim), but a non-empty list is only grounded if
    every phrase is a real substring of the source text.
    """
    if not evidence:
        return True
    lowered = text.lower()
    return all(phrase.lower() in lowered for phrase in evidence)


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
        for arch, slot in (("A_prime", Slot.FAST), ("B", Slot.REASONING)):
            for rep in range(args.reps):
                prompt = _TRIAGE_PROMPT.format(context=context, text=case.text)
                call = await _call_triage(models, slot, prompt)
                entry = {
                    "text": case.text, "category": case.category,
                    "expected_mode": case.expected_mode, "seed": case.seed,
                    "architecture": arch, "rep": rep, "ok": call.ok,
                    "json_ok": call.json_ok, "latency_ms": round(call.latency_ms, 1),
                    "error": call.error,
                    # Persisted unconditionally — including on failure — so a
                    # validation error can actually be diagnosed afterwards
                    # instead of only being visible as an opaque error count.
                    "raw_text": call.raw_text,
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
                        "evidence_grounded": _evidence_grounded(call.result.action_evidence,
                                                                case.text),
                    })
                results["cases"].append(entry)
                # [RUN]/[FAIL] describes whether the call executed and parsed
                # into a valid TriageResult — nothing about whether the model
                # was *right*. That's what the trailing [match]/[MISS] marker
                # is for for scored cases (see the legend printed with the
                # summary); an unscored case never gets one, since there is no
                # single correct answer to check it against.
                tag = "RUN " if call.ok else "FAIL"
                mode = call.result.mode if call.result else "-"
                if case.expected_mode is None or call.result is None:
                    correctness = ""
                elif call.result.mode == case.expected_mode:
                    correctness = "  [match]"
                else:
                    correctness = "  [MISS]"
                print(f"  [{tag}] {arch:8} {case.category:24} {case.text[:42]!r:44} "
                     f"mode={mode:6} {call.latency_ms:7.0f}ms{correctness}" +
                     (f"  ({call.error})" if not call.ok else ""))

    if args.output != "-":
        Path(args.output).write_text(json.dumps(results, indent=2, default=str))
        print(f"\nRaw results written to {args.output}")

    _print_report(results)
    return 0


def pct(n: int, d: int) -> str:
    return f"{100 * n / d:.0f}%" if d else "n/a"


def _p(vals: list[float], q: float) -> str:
    if not vals:
        return "n/a"
    idx = min(len(vals) - 1, int(len(vals) * q))
    return f"{vals[idx]:.0f}ms"


def _print_report(results: dict[str, Any]) -> None:
    """Every number below is counted directly from ``results["cases"]`` —
    each metric is its own independent pass over the raw records, never
    derived by arithmetic on another already-computed metric. That's
    deliberate: the bug this function replaces (a "JSON failure rate" that
    read 0% while three schema-validation failures sat in the same rows)
    came from counting only one failure *kind* under a label that implied
    it covered both. Direct, separate counts for each named thing, plus the
    consistency checks at the end of each block, are what stop that class of
    bug from being silent again.
    """
    cases = results["cases"]
    print("\n" + "=" * 72)
    print("SUMMARY")
    print("=" * 72)
    print("([RUN]/[FAIL] above describes whether a call executed and parsed —")
    print(" never whether the model's answer was correct. [match]/[MISS] is correctness.)")

    for arch in ("A_prime", "B"):
        rows = [c for c in cases if c["architecture"] == arch]
        scored = [c for c in rows if c["expected_mode"] is not None]

        # -- execution status: every row is either ok or not-ok, and every
        # not-ok row failed at exactly one of two points — parsing the JSON,
        # or validating it against the schema. Each is counted on its own.
        ok_count = sum(1 for c in rows if c["ok"])
        not_ok_count = sum(1 for c in rows if not c["ok"])
        parse_fail_count = sum(1 for c in rows if not c["json_ok"])
        validation_fail_count = sum(1 for c in rows if c["json_ok"] and not c["ok"])

        # -- correctness, scored cases only (expected_mode in {chat, action}) --
        # A call that failed to execute has no `mode` at all — it is neither a
        # correct "chat" nor a wrong "action", it's a third thing the four
        # confusion buckets don't have room for. Forcing it into one (e.g.
        # letting a chat-expected failure hide inside "not predicted action")
        # is exactly the kind of miscount this rewrite exists to stop. So the
        # confusion matrix covers only scored calls that actually executed;
        # a failed scored call is still counted as wrong in `scored accuracy`
        # (below) and in the failure-rate metrics, just not force-fit here.
        correct_count = sum(1 for c in scored if c.get("mode") == c["expected_mode"])
        scored_ok = [c for c in scored if c["ok"]]
        scored_failed_count = len(scored) - len(scored_ok)
        tp = sum(1 for c in scored_ok if c["expected_mode"] == "action" and c["mode"] == "action")
        fn = sum(1 for c in scored_ok if c["expected_mode"] == "action" and c["mode"] != "action")
        tn = sum(1 for c in scored_ok if c["expected_mode"] == "chat" and c["mode"] == "chat")
        fp = sum(1 for c in scored_ok if c["expected_mode"] == "chat" and c["mode"] == "action")

        # -- evidence grounding: only meaningful where evidence was claimed --
        with_evidence_count = sum(1 for c in rows if c.get("action_evidence"))
        grounded_count = sum(1 for c in rows if c.get("action_evidence") and c.get("evidence_grounded"))

        # -- objective sufficiency, split: genuine (scored) actions vs. the
        # deliberately underspecified pool, which should legitimately score
        # low here — that's a correct "I can't act on this yet", not a miss.
        genuine_action_count = sum(1 for c in rows
                                   if c.get("mode") == "action" and c["expected_mode"] == "action")
        genuine_sufficient_count = sum(1 for c in rows if c.get("mode") == "action"
                                       and c["expected_mode"] == "action"
                                       and c.get("objective_sufficient"))
        underspec_action_count = sum(1 for c in rows if c.get("mode") == "action"
                                     and c["category"] == "clarification_underspecified")
        underspec_sufficient_count = sum(1 for c in rows if c.get("mode") == "action"
                                         and c["category"] == "clarification_underspecified"
                                         and c.get("objective_sufficient"))

        corrections = sum(1 for c in rows if c.get("evidence_correction_applied"))
        latencies = sorted(c["latency_ms"] for c in rows if c["ok"])

        # -- self-checks: every count above was taken independently straight
        # from `rows`/`scored`, so these should hold by construction. If they
        # don't, the report is wrong and says so loudly rather than quietly
        # printing numbers that don't add up.
        checks = [
            ("ok + not-ok == total rows", ok_count + not_ok_count == len(rows)),
            ("parse-fail + validation-fail == not-ok",
             parse_fail_count + validation_fail_count == not_ok_count),
            ("TP+FN+TN+FP == scored calls that executed", tp + fn + tn + fp == len(scored_ok)),
            ("grounded <= claimed evidence", grounded_count <= with_evidence_count),
        ]
        for description, holds in checks:
            if not holds:
                print(f"  ⚠️  INTERNAL CHECK FAILED for {arch}: {description}")

        print(f"\n{arch}  ({len(rows)} calls, {len(scored)} scored)")
        print("  -- correctness --")
        print(f"    scored accuracy: {pct(correct_count, len(scored))} "
             f"({correct_count}/{len(scored)})")
        print(f"    action recall  (of {tp + fn} genuine actions, caught): "
             f"{pct(tp, tp + fn)} ({tp}/{tp + fn})")
        print(f"    chat precision (of {tn + fn} calls predicted chat, truly chat): "
             f"{pct(tn, tn + fn)} ({tn}/{tn + fn})")
        excl = (f" — excludes {scored_failed_count} failed scored call(s), already counted "
               "as wrong above and in the failure rates below") if scored_failed_count else ""
        print(f"    confusion matrix (scored cases that executed successfully{excl}):")
        print(f"        TP={tp}   FN={fn}")
        print(f"        FP={fp}   TN={tn}")
        print("  -- execution --")
        print(f"    JSON/schema failure rate (either kind): "
             f"{pct(not_ok_count, len(rows))} ({not_ok_count}/{len(rows)})")
        print(f"    parse failure rate       (not valid JSON at all): "
             f"{pct(parse_fail_count, len(rows))} ({parse_fail_count}/{len(rows)})")
        print(f"    validation failure rate  (valid JSON, failed the schema): "
             f"{pct(validation_fail_count, len(rows))} ({validation_fail_count}/{len(rows)})")
        print(f"    evidence-validator corrections (empty evidence + action): {corrections}")
        print("  -- evidence & objectives --")
        print(f"    evidence-grounding rate (claimed evidence actually in the input): "
             f"{pct(grounded_count, with_evidence_count)} ({grounded_count}/{with_evidence_count})")
        print(f"    objective sufficiency on genuine actions: "
             f"{pct(genuine_sufficient_count, genuine_action_count)} "
             f"({genuine_sufficient_count}/{genuine_action_count})")
        if underspec_action_count:
            print(f"    objective sufficiency on underspecified requests "
                 f"(low is correct — nothing to act on yet): "
                 f"{pct(underspec_sufficient_count, underspec_action_count)} "
                 f"({underspec_sufficient_count}/{underspec_action_count})")
        else:
            print("    objective sufficiency on underspecified requests: "
                 "n/a (none classified action)")
        print("  -- latency --")
        print(f"    p50: {_p(latencies, 0.50)}    p95: {_p(latencies, 0.95)}")

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
