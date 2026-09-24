"""The interpreter: what does the person actually want?

The one semantic authority for chat vs. action (V1.3), rewritten for v3.0
around how people really talk to an assistant. One schema-constrained call
decides, from a speech transcript:

* **mode** — act on the computer, or talk. Asking, telling, hinting and
  wishing all count as asking ("I need some AA batteries", "pop YouTube
  on"); mentioning something JARVIS could act on does not ("I hate dealing
  with email").
* **normalized_command** — the request restated as one plain instruction.
  The orchestrator tries it against the deterministic fast path, so "could
  you pop a new tab open" gets the same millisecond answer as "open a new
  tab" without the fast path ever having to understand slang itself.
* **objective** — goal, kind, targets, constraints, complexity, what's
  missing, and, new in v3.0, **success_criteria**: what must be true when
  the work is done, which the operator checks its own work against before
  it claims to have finished. Plus where the work happens (surface, site,
  app).

Always the reasoning slot, never ``fast`` — the V1.3 benchmark
(``scripts/bench_triage.py``) showed a 1B model calling every action "chat".
With a schema, providers that can constrain output (Ollama's grammar, an
OpenAI-compatible ``json_schema``) cannot return a malformed reply at all.
"""

from __future__ import annotations

from ..core.logging import get_logger
from ..models.base import ChatMessage
from .schema import Objective, Triage, load
from .state import ConversationState

log = get_logger("jarvis.intelligence.triage")

TRIAGE_PROMPT = """You decide what the user wants. Two modes only.

The words come from speech recognition, so expect casual phrasing, filler words
and the odd misheard word ("bass kit" is "basket", "spot if I" is "Spotify") —
read what the person meant, not the literal transcript.

mode="action": the user wants something done on this computer, now — however
they put it. Asking, telling, hinting and wishing all count:
  "open a new tab", "could you pop YouTube on", "I need some AA batteries",
  "stick some music on", "let's get the white kettle ordered", "crank it up a bit",
  "what's on my calendar today", "anything new from Sarah?", "remind me to call mum at 6".
  Questions about the user's own things — their mail, calendar, files, screen,
  basket, this Mac — are actions.
mode="chat": conversation — greetings, opinions, feelings, jokes, advice, general
knowledge, talking about a topic:
  "I hate dealing with email", "what's the capital of France", "why do batteries die
  in the cold", "do you think Safari is better than Chrome", "how much storage does
  the iPhone 16 have". Mentioning something JARVIS can act on is NOT asking for it.

action_evidence: the words in THIS message that ask for something — not from earlier
context. A task that was active before is never itself evidence that a new message
continues it. No such words means mode="chat".

For mode="action" also give:
  normalized_command: the request as ONE plain instruction a literal-minded assistant
    would understand, e.g. "open a new tab in Safari", "play music in Spotify",
    "set the volume to 30 percent", "search Amazon for AA batteries and add a pack to
    the basket", "read today's calendar".
  objective: goal, kind, targets, constraints, complexity, confidence, missing, and
    targets: the specific things it is about, in the user's words (["AA batteries"],
      ["Tom"], ["the airport"]);
    success_criteria: what must be TRUE when it's done, each one checkable
      ("a pack of AA batteries is in the Amazon basket", "Safari shows a new empty tab");
    surface: "web" | "native" | "either"; site: the website meant, if any (e.g.
      "amazon.co.uk"); app: the app meant, if any.

Something needing several real steps in an app or website — searching, comparing,
filling in forms, clicking through pages, adding to a basket, downloading — is
kind="automation", complexity="multi_step". A single step ("click the search bar",
"what's on my screen") is complexity="simple".
If something needed is missing (who to email, what to send), stay mode="action" and
list it in objective.missing rather than guessing.

{context}

User said: "{text}"

Reply with JSON only."""

_STRINGS = {"type": "array", "items": {"type": "string"}}

#: The reply shape, in the order the model should think in: decide first,
#: evidence next, then the details. Constrained decoding follows it exactly.
TRIAGE_SCHEMA = {
    "type": "object",
    "properties": {
        "mode": {"type": "string", "enum": ["chat", "action"]},
        "action_evidence": _STRINGS,
        "confidence": {"type": "number"},
        "normalized_command": {"type": "string"},
        "requires_tools": {"type": "boolean"},
        "objective": {
            "type": "object",
            "properties": {
                "goal": {"type": "string"},
                "kind": {"type": "string"},
                "targets": _STRINGS,
                "constraints": _STRINGS,
                "complexity": {"type": "string", "enum": ["trivial", "simple", "multi_step"]},
                "confidence": {"type": "string",
                               "enum": ["confident", "probable", "ambiguous", "impossible"]},
                "missing": _STRINGS,
                "success_criteria": _STRINGS,
                "surface": {"type": "string", "enum": ["web", "native", "either", "none"]},
                "site": {"type": "string"},
                "app": {"type": "string"},
            },
        },
        "reason": {"type": "string"},
    },
    "required": ["mode", "action_evidence", "reason"],
}


class IntentTriage:
    """Decides chat or action and says what the action is. Never executes."""

    def __init__(self, models, slot: str = "reasoning"):
        self._models = models
        self._slot = slot

    async def decide(self, text: str, state: ConversationState) -> Triage:
        # The conversational view only — deliberately not describe_for_model,
        # which carries the active objective, browser/email/research state and
        # recent actions. That task/execution context is exactly what must
        # not silently become "evidence" that a fresh, unrelated utterance is
        # an action (a stale "current objective: research X" line sitting in
        # front of a plain "how are you" is precisely the failure mode this
        # guards against). Reference resolution, which legitimately needs
        # that fuller state, happens downstream in Understanding, once triage
        # has already said "action".
        context = state.describe_recent_conversation(include_turns=2) or "(no prior context)"
        prompt = TRIAGE_PROMPT.format(context=context, text=text.replace('"', "'"))
        try:
            data = await self._models.complete_json(
                self._slot,
                [ChatMessage("system", "You decide chat or action. JSON only."),
                 ChatMessage("user", prompt)],
                schema=TRIAGE_SCHEMA,
                max_tokens=500,
                timeout_s=30.0,
            )
        except Exception as exc:
            log.debug("triage model unavailable: %s", exc)
            return self._fallback()
        if isinstance(data, dict) and isinstance(data.get("objective"), dict) and data["objective"]:
            # The same repair-pass loader as everywhere else, applied to the
            # nested objective before the outer model validates — otherwise a
            # near-miss objective fails the whole triage call instead of just
            # itself.
            data["objective"] = load(Objective, data["objective"])
        triage = load(Triage, data)
        if triage is None:
            return self._fallback()
        return triage

    @staticmethod
    def _fallback() -> Triage:
        # Pretending to understand offline would be worse than admitting the
        # limit — the same rule Understanding's own fallback follows. Chat is
        # the safe default: a missed action costs a repeated request; a
        # fabricated one costs an unwanted tool call.
        return Triage(mode="chat", confidence=0.2, reason="triage model unavailable")


def objective_sufficient(objective: Objective | None) -> bool:
    """Would this objective be usable directly, skipping a separate
    Understanding call?

    "Action" means the user appears to be asking for something; this is the
    separate, stricter question of whether there is enough here to actually
    act on (V1.3 §6) — confident or probable, nothing missing.
    """
    if objective is None:
        return False
    if not objective.goal.strip():
        return False
    if objective.confidence not in ("confident", "probable"):
        return False
    return not objective.missing
