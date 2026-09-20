"""Intent triage: chat or action, before any tool-shaped machinery runs.

This is the one semantic authority for the chat/action split (V1.3). Nothing
downstream — :class:`~.catalog.ToolCatalog`, the planner, the execution loop —
ever sees a turn until triage has said "action"; a capability keyword scorer
is not consulted for this decision (see ``router/router.py``, which no longer
calls its own heuristic/classify stages for this reason).

Always the reasoning slot (defers to ``general`` — llama3.1:8b — unless a
stronger model is configured), never ``fast``. The V1.3 benchmark
(``scripts/bench_triage.py``) is why: the 1B model classified every explicit
action request as chat, with schema-validation failures on top. Speed is
intentionally traded for reliability here; the fast gateway
(``router/quick.py``) is what keeps genuinely deterministic requests fast.
"""

from __future__ import annotations

from ..core.logging import get_logger
from ..models.base import ChatMessage
from .schema import Objective, Triage, load
from .state import ConversationState

log = get_logger("jarvis.intelligence.triage")

TRIAGE_PROMPT = """You decide what the user wants. Two modes only.

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
literal phrases from what the user just said in THIS message — not from
earlier context, and not inferred from a task or topic that was active
before. A prior task being unfinished is never itself evidence that a new,
unrelated message continues it. If you cannot point to such words in the
current message, use mode="chat". Do not guess.

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


class IntentTriage:
    """Decides chat or action. Never executes, never replies."""

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
                max_tokens=350,
                timeout_s=30.0,
            )
        except Exception as exc:
            log.debug("triage model unavailable: %s", exc)
            return self._fallback()
        if isinstance(data, dict) and isinstance(data.get("objective"), dict):
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
