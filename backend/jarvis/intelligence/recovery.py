"""Error recovery.

When an action fails or doesn't verify, something has to decide what next:
retry, try a different tool, adjust the arguments, ask the user, or stop and
say what went wrong. That decision is made once, here, with a bounded budget —
blind retry loops are worse than an honest failure.
"""

from __future__ import annotations

from ..core.logging import get_logger
from ..models.base import ChatMessage
from ..tools.base import ToolResult
from .catalog import ToolCard
from .schema import Objective, RecoveryPlan, Verification, load

log = get_logger("jarvis.intelligence.recovery")

RECOVERY_PROMPT = """An action did not achieve the objective. Decide what to do next.

Objective: {goal}
Action taken: {tool}({arguments})
Outcome: {outcome}
Problem: {problem}
Attempts so far on this objective: {attempts} of {budget}

Tools available:
{tools}

Reply with JSON only, choosing one strategy:
{{"strategy": "retry|alternative_tool|modify_arguments|ask_user|report",
  "tool": "<tool name, for alternative_tool>",
  "arguments": {{}},
  "question": "<for ask_user>",
  "reason": "<one short sentence>"}}

Guidance:
- retry only if the failure looks transient and the tool is safe to repeat.
- modify_arguments if the arguments were wrong or incomplete.
- alternative_tool if a different tool would get there.
- ask_user if only the user can resolve it.
- report if nothing sensible remains."""


class RecoveryManager:
    """Chooses the next move after a failure, within a fixed attempt budget."""

    def __init__(self, models, catalog, slot: str = "reasoning", budget: int = 2):
        self._models = models
        self._catalog = catalog
        self._slot = slot
        self.budget = budget

    async def decide(self, objective: Objective, tool: str, arguments: dict,
                     result: ToolResult, verification: Verification,
                     cards: list[ToolCard], attempts: int) -> RecoveryPlan:
        if attempts >= self.budget:
            return RecoveryPlan(strategy="report",
                                reason=f"tried {attempts} time(s) without success")

        deterministic = self._deterministic(tool, arguments, result, verification, attempts)
        if deterministic is not None:
            return deterministic

        prompt = RECOVERY_PROMPT.format(
            goal=objective.goal,
            tool=tool,
            arguments=_short(arguments),
            outcome=(result.summary or "no summary")[:200],
            problem=(verification.problem or result.error or "did not verify")[:200],
            attempts=attempts,
            budget=self.budget,
            tools="\n".join(card.render() for card in cards[:10]),
        )
        try:
            data = await self._models.complete_json(
                self._slot,
                [ChatMessage("system", "You choose recovery strategies. JSON only."),
                 ChatMessage("user", prompt)],
                max_tokens=250,
                timeout_s=25.0,
            )
        except Exception as exc:
            log.debug("recovery model unavailable: %s", exc)
            data = None

        plan = load(RecoveryPlan, data)
        if plan is None:
            return RecoveryPlan(strategy="report", reason="could not determine a recovery")
        if plan.strategy == "alternative_tool" and not self._catalog.card(plan.tool or ""):
            return RecoveryPlan(strategy="report",
                                reason=f"suggested tool {plan.tool} does not exist")
        if plan.strategy == "retry" and not self._safe_to_retry(tool):
            return RecoveryPlan(strategy="report",
                                reason=f"{tool} changes state and shouldn't be repeated blindly")
        return plan

    # -- cheap decisions that need no model --------------------------------
    def _deterministic(self, tool: str, arguments: dict, result: ToolResult,
                       verification: Verification, attempts: int) -> RecoveryPlan | None:
        detail = f"{result.summary} {result.error or ''}".lower()

        # The user said no. That is an answer, not a failure to work around.
        if "confirm" in detail and ("declin" in detail or "didn't receive" in detail):
            return RecoveryPlan(strategy="report", reason="the user declined the action")

        # Bad or missing arguments are the model's to fix, not worth a round trip.
        if "missing required argument" in detail or "must be one of" in detail:
            return RecoveryPlan(strategy="modify_arguments",
                                reason="the arguments were incomplete or invalid")

        # A tool that doesn't exist on this host will never start existing.
        if "only works on macos" in detail or "isn't available on this host" in detail:
            return RecoveryPlan(strategy="report",
                                reason="that capability isn't available on this machine")

        if verification.skipped and result.ok:
            return RecoveryPlan(strategy="report", reason="nothing to recover from")
        return None

    def _safe_to_retry(self, tool: str) -> bool:
        card = self._catalog.card(tool)
        return bool(card and card.retryable)


def _short(value: dict) -> str:
    text = ", ".join(f"{k}={str(v)[:50]}" for k, v in (value or {}).items())
    return text[:200] or "no arguments"
