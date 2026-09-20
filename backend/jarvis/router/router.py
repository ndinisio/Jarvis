"""The router.

Two authoritative stages, cheapest first:

1. **quick** — deterministic patterns (~0.05 ms). Greetings, app launches,
   system facts, clipboard, volume, time, explicit browser commands.
2. **arithmetic** — sums, computed rather than reasoned about.

Everything neither stage catches falls through to a single ``conversation``
capability decision when ``capability_routing`` is left at its default
(``False``): the orchestrator hands that turn to the agent loop, and
specifically to :class:`~jarvis.intelligence.triage.IntentTriage` (V1.3), the
one semantic authority for chat vs. action. ``_heuristic`` (weighted keyword
scoring across capabilities) and ``_classify`` (a 1B-class model guessing a
capability) are no longer part of that decision: a benchmark showed the 1B
model misclassifying real actions as chat and the keyword scorer scoring
partial credit for a domain word mentioned in passing ("I hate dealing with
email"). They remain load-bearing for exactly one case — ``capability_routing
=True``, which the orchestrator passes when ``intelligence.enabled`` is
``False``: with no agent and no triage downstream, V1.1 mode has no other way
to reach a specific capability, so the old three-stage behaviour is preserved
there unchanged.

The rule the whole design serves: *never spend a second of model time on a
question the computer can already answer*.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass

from ..core.logging import get_logger
from ..core.telemetry import Telemetry
from ..models.base import ChatMessage
from ..models.registry import ModelRouter, Slot
from .quick import QuickCommands
from .schema import RouteDecision, RouteKind, RoutePath

log = get_logger("jarvis.router")


@dataclass(slots=True)
class CapabilityHint:
    name: str
    description: str
    keywords: tuple[str, ...]
    strong: tuple[str, ...] = ()
    long_running: bool = False


#: Capability descriptions do double duty: keyword scoring *and* the prompt the
#: fast model sees. Keeping them in one place stops the two drifting apart.
CAPABILITIES: tuple[CapabilityHint, ...] = (
    CapabilityHint(
        "system",
        "facts about this Mac: storage, memory, battery, CPU, network, version, uptime, volume",
        ("storage", "disk", "space", "battery", "memory", "ram", "cpu", "processor", "chip",
         "macos", "version", "uptime", "network", "wifi", "volume", "mute", "specs", "hardware"),
        strong=("how much storage", "battery percentage", "how much ram"),
    ),
    CapabilityHint(
        "apps",
        "opening, closing, focusing and listing macOS applications",
        ("open", "launch", "quit", "close", "app", "application", "switch to", "running"),
        strong=("open ", "launch ", "quit "),
    ),
    CapabilityHint(
        "clipboard",
        "reading from and writing to the clipboard",
        ("clipboard", "copied", "copy", "paste"),
        strong=("clipboard",),
    ),
    CapabilityHint(
        "screen",
        "looking at the user's screen: describing it, reading errors, explaining what is visible",
        ("screen", "screenshot", "see", "looking at", "visible", "this error", "display"),
        strong=("on my screen", "what do you see", "this error"),
        long_running=True,
    ),
    CapabilityHint(
        "email",
        "reading, searching, summarising and drafting email in Apple Mail",
        ("email", "mail", "inbox", "message from", "reply", "unread", "draft", "compose"),
        strong=("check my email", "my inbox", "new mail", "draft a reply", "reply to",
                "write to", "email to"),
        long_running=True,
    ),
    CapabilityHint(
        "calendar",
        "calendar events: today, upcoming, searching, creating",
        ("calendar", "schedule", "meeting", "appointment", "agenda", "event", "diary"),
        strong=("on my calendar", "my schedule", "next meeting"),
        long_running=True,
    ),
    CapabilityHint(
        "research",
        "investigating something on the web: searching, reading pages, comparing, summarising",
        ("research", "look up", "find out", "compare", "news", "best", "cheapest", "reviews",
         "price", "deals", "latest", "who is", "what is the current"),
        strong=("research ", "compare ", "find the best", "look into"),
        long_running=True,
    ),
    CapabilityHint(
        "browser",
        "controlling the browser: opening URLs, reading the current page",
        ("website", "url", "browser", "safari", "chrome", "page", "tab", "web address"),
        strong=("go to ", "open the website", "this page"),
    ),
    CapabilityHint(
        "files",
        "files and notes in the JARVIS workspace: listing, reading, writing, searching",
        ("file", "folder", "note", "workspace", "document", "save", "write down", "directory"),
        strong=("in my workspace", "make a note"),
    ),
    CapabilityHint(
        "diagnostics",
        "diagnosing problems with the Mac: slowness, crashes, disk pressure, permissions",
        ("slow", "wrong", "broken", "crash", "freeze", "hang", "problem", "diagnose", "fix",
         "not working", "laggy", "overheating"),
        strong=("why is my mac", "what's wrong"),
        long_running=True,
    ),
    CapabilityHint(
        "memory",
        "what JARVIS remembers about the user: recalling, remembering, forgetting",
        ("remember", "forget", "memory", "preference", "call me"),
        strong=("do you remember", "forget that"),
    ),
    CapabilityHint(
        "conversation",
        "anything else: chat, general knowledge, explanations, jokes, definitions, maths",
        (),
    ),
)

CAPABILITY_NAMES = tuple(c.name for c in CAPABILITIES)

_HEURISTIC_ACCEPT = 2.2
_HEURISTIC_MARGIN = 1.0

_ARITHMETIC = re.compile(
    r"^\s*(?:what(?:'s| is)\s+)?(?P<expr>[\d\s()+\-*/.^%]+)\s*\??\s*$"
)


class Router:
    def __init__(self, models: ModelRouter, telemetry: Telemetry | None = None,
                 quick: QuickCommands | None = None):
        self._models = models
        self._telemetry = telemetry or Telemetry()
        self._quick = quick or QuickCommands()

    async def route(self, text: str, *, context: str = "", allow_model: bool = True,
                    capability_routing: bool = False) -> RouteDecision:
        """Route one turn.

        ``capability_routing`` opts back into the old heuristic/fast-model
        capability guess for V1.1 mode (``intelligence.enabled=False``),
        which has no agent and no triage downstream to do this more
        reliably. Leave it ``False`` (the default) whenever intelligence is
        enabled — see the module docstring.
        """
        t0 = time.perf_counter()

        decision = self._quick.match(text)
        if decision is not None:
            self._telemetry.record("router.quick", (time.perf_counter() - t0) * 1000.0,
                                   route=f"{decision.kind}:{decision.name}")
            return decision

        maths = self._arithmetic(text)
        if maths is not None:
            maths.latency_ms = (time.perf_counter() - t0) * 1000.0
            self._telemetry.record("router.quick", maths.latency_ms, route="control:arithmetic")
            return maths

        if capability_routing:
            decision = self._heuristic(text)
            if decision is not None:
                decision.latency_ms = (time.perf_counter() - t0) * 1000.0
                self._telemetry.record("router.heuristic", decision.latency_ms,
                                       route=f"{decision.kind}:{decision.name}")
                return decision

            if allow_model:
                decision = await self._classify(text, context)
                if decision is not None:
                    decision.latency_ms = (time.perf_counter() - t0) * 1000.0
                    self._telemetry.record("router.model", decision.latency_ms,
                                           route=f"{decision.kind}:{decision.name}")
                    return decision

        fallback = RouteDecision(
            kind=RouteKind.CAPABILITY, name="conversation", confidence=0.4,
            path=RoutePath.FALLBACK, reason="no confident classification",
            speak_result_directly=False,
        )
        fallback.latency_ms = (time.perf_counter() - t0) * 1000.0
        return fallback

    # -- stage 2: heuristics ----------------------------------------------
    def _heuristic(self, text: str) -> RouteDecision | None:
        lowered = (text or "").lower()
        if not lowered.strip():
            return None
        scores: dict[str, float] = {}
        for capability in CAPABILITIES:
            if capability.name == "conversation":
                continue
            score = 0.0
            for phrase in capability.strong:
                if phrase in lowered:
                    score += 2.0
            for keyword in capability.keywords:
                if re.search(rf"\b{re.escape(keyword)}\b", lowered):
                    score += 1.0
            if score:
                scores[capability.name] = score
        if not scores:
            return None
        ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
        best, best_score = ranked[0]
        runner_up = ranked[1][1] if len(ranked) > 1 else 0.0
        if best_score < _HEURISTIC_ACCEPT or (best_score - runner_up) < _HEURISTIC_MARGIN:
            return None
        hint = next(c for c in CAPABILITIES if c.name == best)
        return RouteDecision(
            kind=RouteKind.CAPABILITY,
            name=best,
            args={"query": text},
            confidence=min(0.9, 0.5 + best_score / 10.0),
            path=RoutePath.HEURISTIC,
            reason=f"keyword score {best_score:.1f} vs {runner_up:.1f}",
            long_running=hint.long_running,
            speak_result_directly=False,
        )

    # -- stage 3: fast-model classification --------------------------------
    async def _classify(self, text: str, context: str = "") -> RouteDecision | None:
        listing = "\n".join(f"- {c.name}: {c.description}" for c in CAPABILITIES)
        prompt = (
            "Classify the user's request into exactly one capability.\n\n"
            f"Capabilities:\n{listing}\n\n"
            + (f"Recent context:\n{context}\n\n" if context else "")
            + f'Request: "{text}"\n\n'
            'Answer with JSON only: {"capability": "<name>", "confidence": <0-1>}'
        )
        try:
            data = await self._models.complete_json(
                Slot.FAST,
                [
                    ChatMessage("system", "You are a fast, silent request classifier. "
                                          "You reply with JSON and nothing else."),
                    ChatMessage("user", prompt),
                ],
                max_tokens=60,
                timeout_s=8.0,
            )
        except Exception as exc:
            log.debug("classification unavailable (%s); falling back to conversation", exc)
            return None
        if not data:
            return None
        name = str(data.get("capability", "")).strip().lower()
        if name not in CAPABILITY_NAMES:
            name = _closest(name)
        if not name:
            return None
        hint = next(c for c in CAPABILITIES if c.name == name)
        try:
            confidence = float(data.get("confidence", 0.7))
        except (TypeError, ValueError):
            confidence = 0.7
        return RouteDecision(
            kind=RouteKind.CAPABILITY,
            name=name,
            args={"query": text},
            confidence=max(0.3, min(1.0, confidence)),
            path=RoutePath.MODEL,
            reason="fast-model classification",
            long_running=hint.long_running,
            speak_result_directly=False,
        )

    # -- arithmetic --------------------------------------------------------
    @staticmethod
    def _arithmetic(text: str) -> RouteDecision | None:
        """Simple sums are computed, not reasoned about."""
        cleaned = (text or "").strip().rstrip("?")
        cleaned = re.sub(r"^(?:what(?:'s| is)|calculate|compute)\s+", "", cleaned, flags=re.I)
        cleaned = (cleaned.replace("plus", "+").replace("minus", "-")
                   .replace("times", "*").replace("divided by", "/").replace("x", "*"))
        if not re.fullmatch(r"[\d\s()+\-*/.%]+", cleaned or "x"):
            return None
        if not re.search(r"[+\-*/%]", cleaned):
            return None
        try:
            value = eval(compile(
                cleaned, "<arithmetic>", "eval"), {"__builtins__": {}}, {})
        except Exception:
            return None
        if isinstance(value, float) and value.is_integer():
            value = int(value)
        return RouteDecision(
            kind=RouteKind.CONTROL, name="arithmetic",
            args={"expression": cleaned, "value": value},
            confidence=1.0, path=RoutePath.QUICK, reason="arithmetic expression",
        )


def _closest(name: str) -> str:
    import difflib

    if not name:
        return ""
    matches = difflib.get_close_matches(name, CAPABILITY_NAMES, n=1, cutoff=0.6)
    return matches[0] if matches else ""


def describe_capabilities() -> str:
    return json.dumps({c.name: c.description for c in CAPABILITIES}, indent=2)
