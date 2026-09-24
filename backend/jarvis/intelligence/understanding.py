"""Intent and entity understanding.

Produces an :class:`Objective` — what the user is trying to achieve — rather
than a capability label. The objective carries the references the user made,
already resolved against conversation state where possible, plus an honest
confidence so the agent knows when to ask instead of guess.

The reasoning model does the understanding. A deterministic fallback keeps the
system usable when no model is reachable; it is deliberately modest, because
pretending to understand offline would be worse than admitting the limit.
"""

from __future__ import annotations

import re

from ..core.logging import get_logger
from ..models.base import ChatMessage
from .entities import ReferenceResolver
from .schema import Complexity, Confidence, EntityRef, Objective, load
from .state import ConversationState, PendingClarification

log = get_logger("jarvis.intelligence.understanding")

UNDERSTANDING_PROMPT = """You work out what the user wants. Reply with JSON only.

{context}

User just said: "{text}"

Return:
{{"goal": "<what they want, one short phrase>",
 "kind": "<verb phrase: read email|navigate|research|inspect screen|type text|schedule|diagnose|automation|chat|...>",
 "targets": ["<things acted on>"],
 "constraints": ["<filters or limits they gave>"],
 "references": [{{"text": "<phrase like 'it' or 'my brother'>", "kind": "person|app|url|email|file|result|screen_element"}}],
 "needs_tools": true|false,
 "complexity": "trivial|simple|multi_step",
 "confidence": "confident|probable|ambiguous|impossible",
 "refines_previous": true|false,
 "is_correction": true|false,
 "missing": ["<information you would need but were not given>"]}}

Rules:
- refines_previous is true when this narrows or changes the previous request rather than starting fresh.
- is_correction is true when the user is saying the last action was wrong.
- needs_tools is false only for chat, greetings and questions you can answer from what is already known.
- complexity is multi_step only when several different actions are genuinely required.
- kind="automation" (always with complexity="multi_step") is specifically for operating an app or
  website through several real steps to reach an end result — searching, comparing, filling in
  forms, clicking through pages, downloading a file. "Click the search bar" is kind="click",
  complexity="simple"; "find the best value two-pack of ESP-32 boards and add them to my basket"
  is kind="automation", complexity="multi_step".
- confidence is ambiguous when a reference could mean several different things."""


#: Phrases that mark a correction when no model is available. The model is the
#: primary mechanism; this only keeps the offline path from being useless.
_CORRECTION_HINTS = re.compile(
    r"\b(that'?s not (right|it)|wrong|not what i (meant|asked)|no,? i meant|try again|"
    r"that isn'?t (right|it)|incorrect|not that)\b", re.I)
_REFINEMENT_HINTS = re.compile(
    r"^(only|just|but|and|what about|how about|focus on|narrow|filter|instead)\b", re.I)


class Understanding:
    def __init__(self, models, resolver: ReferenceResolver | None = None,
                 slot: str = "reasoning"):
        self._models = models
        self._resolver = resolver or ReferenceResolver()
        self._slot = slot

    async def understand(self, text: str, state: ConversationState,
                         pending: PendingClarification | None = None) -> Objective:
        """Work out what ``text`` means in context.

        ``pending`` is the question JARVIS asked last turn, if any. The caller
        passes it in because it takes the clarification off the state before
        calling — this turn is the answer to it, and leaving it in place would
        make the agent ask again.
        """
        pending = pending if pending is not None else state.pending_clarification
        objective = await self._from_model(text, state)
        if objective is None:
            objective = self._fallback(text, state)
        return self.finalize(objective, text, state, pending)

    def finalize(self, objective: Objective, text: str, state: ConversationState,
                pending: PendingClarification | None = None) -> Objective:
        """Run the deterministic passes on an objective that already exists.

        Shared with :class:`~.triage.IntentTriage`: when triage's own
        objective is already confident and complete, the agent uses it
        directly rather than paying for a second model call — but it still
        needs the same clarification-résumé, reference-resolution and
        inheritance handling any other objective gets.
        """
        pending = pending if pending is not None else state.pending_clarification
        self._apply_clarification(objective, text, pending)
        self._resolve_references(objective, state)
        self._inherit(objective, state)
        return objective

    # -- model path --------------------------------------------------------
    async def _from_model(self, text: str, state: ConversationState) -> Objective | None:
        context = state.describe_for_model(include_turns=3) or "(no prior context)"
        prompt = UNDERSTANDING_PROMPT.format(context=context, text=text.replace('"', "'"))
        try:
            data = await self._models.complete_json(
                self._slot,
                [ChatMessage("system", "You extract structured intent. JSON only."),
                 ChatMessage("user", prompt)],
                max_tokens=400,
                timeout_s=25.0,
            )
        except Exception as exc:
            log.debug("understanding model unavailable: %s", exc)
            return None
        objective = load(Objective, data)
        if objective is None or not objective.goal.strip():
            return None
        return objective

    # -- deterministic fallback -------------------------------------------
    @staticmethod
    def _fallback(text: str, state: ConversationState) -> Objective:
        stripped = text.strip()
        is_correction = bool(_CORRECTION_HINTS.search(stripped))
        refines = bool(_REFINEMENT_HINTS.match(stripped)) or is_correction
        return Objective(
            goal=state.objective if (refines and state.objective) else stripped,
            kind="general",
            targets=[],
            constraints=[stripped] if refines and state.objective else [],
            needs_tools=True,
            complexity=Complexity.SIMPLE,
            confidence=Confidence.PROBABLE,
            refines_previous=refines,
            is_correction=is_correction,
        )

    # -- post-processing ---------------------------------------------------
    @staticmethod
    def _apply_clarification(objective: Objective, text: str,
                             pending: PendingClarification | None) -> None:
        """A turn that answers a pending question resumes the waiting objective."""
        if pending is None:
            return
        objective.goal = pending.objective_goal or objective.goal
        objective.refines_previous = True
        answer = text.strip()
        if answer and pending.slot:
            objective.targets = [answer] + [t for t in objective.targets if t != answer]
        elif answer:
            # A free-form question ("which size?") has no argument to fill:
            # the answer travels with the objective as a constraint.
            note = f"{pending.question.strip().rstrip('?')}? {answer}"
            objective.constraints = [note] + [c for c in objective.constraints if c != note]
        objective.confidence = Confidence.CONFIDENT
        objective.missing = []

    def _resolve_references(self, objective: Objective, state: ConversationState) -> None:
        """Bind each reference to something concrete, or mark it ambiguous."""
        ambiguous = False
        for reference in objective.references:
            resolution = self._resolver.resolve(reference.text, state, reference.kind)
            if resolution.resolved:
                reference.resolved = resolution.value
                reference.kind = resolution.kind
                reference.confidence = resolution.confidence
            elif resolution.ambiguous:
                ambiguous = True
                reference.confidence = 0.0
        if ambiguous and objective.confidence == Confidence.CONFIDENT:
            objective.confidence = Confidence.AMBIGUOUS

    @staticmethod
    def _inherit(objective: Objective, state: ConversationState) -> None:
        """A refinement keeps the previous goal and adds to it."""
        if objective.refines_previous and state.objective:
            if objective.goal and objective.goal != state.objective:
                objective.constraints = [objective.goal, *objective.constraints]
            objective.goal = state.objective

    def unresolved(self, objective: Objective) -> list[EntityRef]:
        return [r for r in objective.references if not r.resolved]
