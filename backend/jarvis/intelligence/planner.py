"""Planning.

Only genuinely multi-step work gets a plan. Everything else goes straight to
the execution loop, because planning a single tool call costs a model round
trip and buys nothing.

A plan is advisory, not a script: the loop consults it for direction and marks
steps done, but each actual decision is made against what the last tool
returned, which is what lets JARVIS change course mid-task.
"""

from __future__ import annotations

from ..core.logging import get_logger
from ..models.base import ChatMessage
from .catalog import ToolCard
from .schema import Objective, Plan, PlanStep, load
from .state import ConversationState

log = get_logger("jarvis.intelligence.planner")

PLAN_PROMPT = """Break the objective into the fewest steps that achieve it.

Objective: {goal}
Details: kind={kind}; targets={targets}; constraints={constraints}

{context}

Tools available:
{tools}

Reply with JSON only:
{{"steps": [{{"intent": "<what this step achieves>", "tool_hint": "<tool name or null>"}}],
  "rationale": "<one sentence>"}}

Use at most {max_steps} steps. Do not include steps for things already known."""


class Planner:
    def __init__(self, models, slot: str = "reasoning", max_steps: int = 6):
        self._models = models
        self._slot = slot
        self._max_steps = max_steps

    async def plan(self, objective: Objective, cards: list[ToolCard],
                   state: ConversationState) -> Plan | None:
        if not objective.needs_planning:
            return None
        prompt = PLAN_PROMPT.format(
            goal=objective.goal,
            kind=objective.kind,
            targets=", ".join(objective.targets) or "none",
            constraints=", ".join(objective.constraints) or "none",
            context=state.describe_for_model(include_turns=2) or "(no prior context)",
            tools="\n".join(card.render() for card in cards),
            max_steps=self._max_steps,
        )
        try:
            data = await self._models.complete_json(
                self._slot,
                [ChatMessage("system", "You plan tool use. JSON only."),
                 ChatMessage("user", prompt)],
                max_tokens=400,
                timeout_s=30.0,
            )
        except Exception as exc:
            log.debug("planner unavailable: %s", exc)
            return None

        plan = load(Plan, data)
        if plan is None or not plan.steps:
            return None
        plan.steps = plan.steps[: self._max_steps]
        return plan

    @staticmethod
    def advance(plan: Plan | None, tool: str) -> None:
        """Mark the first pending step that this tool plausibly satisfies."""
        if plan is None:
            return
        for step in plan.steps:
            if step.done:
                continue
            if not step.tool_hint or step.tool_hint == tool:
                step.done = True
                return
        for step in plan.steps:          # nothing matched by hint; retire the first
            if not step.done:
                step.done = True
                return


def fallback_plan(objective: Objective) -> Plan:
    """A generic shape for multi-step work when the model can't plan.

    Deliberately abstract — gather, then interpret, then report — so it applies
    to research, diagnosis or anything else rather than encoding one scenario.
    """
    return Plan(
        steps=[
            PlanStep(intent=f"gather information about {objective.goal}"),
            PlanStep(intent="interpret what was found"),
            PlanStep(intent="report the result"),
        ],
        rationale="default gather-interpret-report plan",
    )
