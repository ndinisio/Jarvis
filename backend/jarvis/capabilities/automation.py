"""Multi-step app/web automation.

    confirm the plan → for each milestone: decide → act → verify → repeat or
    move on → summarise

Built for the same shape of problem :mod:`research` solves — a real,
multi-step job that must run in the background with genuine progress and
genuine cancellation — but for *operating* an app or a website rather than
reading one. The generic per-turn agent loop (``intelligence/agent.py``) is
deliberately not stretched to cover this: its ``max_steps`` default (six) is
sized for a short info-gathering turn, its plan is a flat one-shot checklist
with no repeat/until construct, and — the concrete reason this needs its own
``Task`` — a turn routed through it never receives a ``cancel_event`` at all
(see ``core/orchestrator.py: _handle_intelligently``'s ``task=None``). This
capability is reached instead through the same backgrounding machinery a
quick-matched capability gets (``core/orchestrator.py: _handoff_to_automation``
→ ``_handle_capability`` → ``_run_in_background``), which is what gives it a
real ``Task``: a ``cancel_event`` "stop" actually reaches, live progress, and
a step trail the user can review.

Like :class:`~.research.ResearchCapability`, the model is never used as the
hands: milestones and individual steps are decided by narrow, structured
calls, but every tool call itself runs through the ordinary registry —
``self.call_tool()`` — so permission gating, verification and state-recording
are exactly what they are anywhere else in JARVIS. What *is* new here is a
task-scoped permission grant (``security/permissions.py: grant_task``): once
the user approves starting the task, routine steps proceed without a fresh
prompt each time, but nothing the consequence classifier marks consequential
— spending money, submitting a payment, deleting, sending, running an
installer — is ever covered by that grant. See ``security/consequence.py``.
"""

from __future__ import annotations

import time
from collections import deque
from typing import Any

from ..core.logging import get_logger
from ..core.narration import ActionNarrator
from ..intelligence.schema import Objective
from ..intelligence.state import ConversationState
from ..intelligence.verify import Verifier
from ..models.base import ChatMessage
from ..models.registry import Slot
from ..security.permissions import RiskLevel
from .base import Capability, Request, Response

log = get_logger("jarvis.capabilities.automation")

DECOMPOSE_PROMPT = """Break this into a short sequence of concrete milestones \
— real, checkable stages towards the end result (e.g. "search for the \
item", "compare the listings", "add the best one to the basket"). Two to \
five milestones; each should be reachable with a handful of tool calls. A \
simple request that's really only one stage still gets one milestone.

Request: {goal}

Reply with JSON only: {{"milestones": ["...", "..."]}}"""

STEP_PROMPT = """Decide the single next action for one part of a larger task.

Overall goal: {goal}
Current milestone: {milestone}

What has happened so far:
{observations}

Tools you may use:
{tools}

Reply with JSON only, one of:
{{"action": "tool_call", "tool": "<name>", "arguments": {{...}}, "reason": "<short>"}}
{{"action": "complete", "reason": "<why this milestone is done>"}}
{{"action": "give_up", "reason": "<why nothing more can be done here>"}}

Rules:
- Use a tool only if it moves this milestone forward; the results above may already cover it.
- "complete" once the milestone's own goal is actually satisfied, not merely attempted.
- "give_up" rather than repeating a call that already failed the same way."""

SUMMARISE_PROMPT = """Tell the user what happened, in one or two sentences unless they asked \
for detail. Be specific about what was actually done, and say plainly anything you couldn't \
complete or stopped short of.

They asked: {goal}

What happened:
{observations}

Answer directly. Do not describe your process or mention tool names."""

#: Curated per-capability tool set — deliberately not ToolCatalog.shortlist():
#: that scoring runs once per turn and never re-scores as task state
#: evolves, which is a real problem for the generic agent loop but doesn't
#: apply here, since this list is fixed and re-offered at every single step.
WEB_TOOLS: tuple[str, ...] = (
    "browse_to", "get_current_page", "list_browser_tabs", "read_page_manifest",
    "click_page_element", "fill_page_field", "submit_page_form",
)
NATIVE_TOOLS: tuple[str, ...] = (
    "open_application", "activate_application", "get_frontmost_app", "list_windows",
    "click_element", "type_text", "press_key", "wait_for_element", "scroll",
)
READ_TOOLS: tuple[str, ...] = (
    "search_web", "fetch_page", "fetch_pages", "capture_screen", "analyse_screen",
)
SHELL_TOOLS: tuple[str, ...] = ("run_shell_command",)
DOWNLOAD_TOOLS: tuple[str, ...] = ("download_file",)
INSTALL_TOOLS: tuple[str, ...] = ("run_installer",)

ALL_AUTOMATION_TOOLS: tuple[str, ...] = (
    WEB_TOOLS + NATIVE_TOOLS + READ_TOOLS + SHELL_TOOLS + DOWNLOAD_TOOLS + INSTALL_TOOLS
)


class AutomationCapability(Capability):
    name = "automation"
    description = "Operate an app or website through several real steps to reach an end result."
    long_running = True

    def __init__(self, deps):
        super().__init__(deps)
        self._narrator = ActionNarrator(deps)
        self._verifier = Verifier(deps)

    async def handle(self, request: Request) -> Response:
        conf = self.deps.config.automation
        task = request.task
        objective = request.args.get("objective")
        goal = ((objective.goal if isinstance(objective, Objective) else "") or request.text).strip()

        request.ctx.raise_if_cancelled()
        milestones = await self._decompose(goal)
        await self._confirm_start(goal, milestones)

        task_id = request.ctx.task_id
        if task_id:
            self.deps.permissions.grant_task(task_id)

        findings: deque[str] = deque(maxlen=conf.findings_window)
        total_steps = 0
        try:
            for milestone in milestones:
                request.ctx.raise_if_cancelled()
                self._announce_milestone(request, task, milestone)
                for _ in range(conf.max_steps_per_milestone):
                    total_steps += 1
                    if total_steps > conf.max_total_steps:
                        findings.append(
                            f"stopped partway through — reached the overall step "
                            f"limit ({conf.max_total_steps} steps)"
                        )
                        break
                    request.ctx.raise_if_cancelled()
                    finding, keep_going = await self._take_step(goal, milestone, findings,
                                                                 request, task)
                    if finding:
                        findings.append(finding)
                    if not keep_going:
                        break
                if total_steps > conf.max_total_steps:
                    break
        finally:
            if task_id:
                self.deps.permissions.revoke_task(task_id)

        report = await self._summarise(goal, list(findings))
        return Response(
            text=report,
            display={"kind": "automation", "title": goal[:90], "findings": list(findings)},
            data={"findings": list(findings)},
        )

    # -- one step of one milestone -------------------------------------------
    async def _take_step(self, goal: str, milestone: str, findings: deque, request: Request,
                         task) -> tuple[str | None, bool]:
        """Returns ``(finding_to_record, keep_going)`` — ``keep_going`` is
        true only after an ordinary tool call that didn't get declined,
        which is what makes this a genuine repeat-until-done loop rather
        than one decision per milestone."""
        decision = await self._decide_step(goal, milestone, findings)
        if decision is None:
            return f"couldn't decide how to continue: {milestone}", False

        action = decision.get("action")
        if action == "complete":
            return f"done: {milestone}", False
        if action == "give_up":
            reason = str(decision.get("reason") or "").strip()
            return (f"couldn't complete: {milestone}" + (f" — {reason}" if reason else ""),
                    False)
        if action != "tool_call" or not decision.get("tool"):
            return f"the decision for “{milestone}” didn't make sense — stopping there", False

        tool = str(decision["tool"])
        if tool not in ALL_AUTOMATION_TOOLS:
            return f"suggested a tool that isn't available here ({tool}) — stopping there", False
        arguments = decision.get("arguments")
        if not isinstance(arguments, dict):
            arguments = {}

        registered = self.registry.get(tool)
        expected_ms = registered.spec.expected_ms if registered else 0
        started = time.monotonic()
        result = await self.call_tool(tool, arguments, request.ctx)
        elapsed_ms = (time.monotonic() - started) * 1000.0

        message = (result.summary or ("done" if result.ok else "failed"))[:180]
        self._announce_step(request, task, milestone, message, expected_ms, elapsed_ms)

        if not result.ok and (result.error or "").startswith("confirmation_declined"):
            # The same deterministic short-circuit intelligence/recovery.py
            # uses: a decline is an answer, not a failure to route around —
            # asking the model to try again would just repeat the prompt
            # the user already said no to.
            return f"you declined: {result.summary}", False

        verification = await self._verifier.verify(
            tool, arguments, result, Objective(goal=milestone, kind="automation"),
            ConversationState(),
        )
        finding = (f"{tool}({_short(arguments)}) → "
                  f"{'ok' if result.ok else 'failed'}: {message}")
        if not verification.verified:
            finding += f" (not verified: {verification.problem[:120]})"
        return finding, True

    # -- model calls ----------------------------------------------------------
    async def _decompose(self, goal: str) -> list[str]:
        try:
            data = await self.models.complete_json(
                Slot.REASONING,
                [ChatMessage("system", "You break a task into milestones. JSON only."),
                 ChatMessage("user", DECOMPOSE_PROMPT.format(goal=goal))],
                max_tokens=200, timeout_s=25.0,
            )
        except Exception as exc:
            log.debug("automation decomposition unavailable: %s", exc)
            return [goal]
        milestones = data.get("milestones") if isinstance(data, dict) else None
        if isinstance(milestones, list):
            cleaned = [str(m).strip() for m in milestones if str(m).strip()][:5]
            if cleaned:
                return cleaned
        return [goal]

    async def _decide_step(self, goal: str, milestone: str, findings: deque) -> dict[str, Any] | None:
        listing = self.registry.describe_for_model(ALL_AUTOMATION_TOOLS)
        prompt = STEP_PROMPT.format(
            goal=goal, milestone=milestone,
            observations="\n".join(f"- {f}" for f in findings) or "- nothing yet",
            tools=listing,
        )
        try:
            data = await self.models.complete_json(
                Slot.REASONING,
                [ChatMessage("system", "You choose the next action for one part of a larger "
                                      "task. JSON only."),
                 ChatMessage("user", prompt)],
                max_tokens=300, timeout_s=30.0,
            )
        except Exception as exc:
            log.debug("automation step decision unavailable: %s", exc)
            return None
        return data if isinstance(data, dict) else None

    async def _summarise(self, goal: str, findings: list[str]) -> str:
        if not findings:
            return "I wasn't able to make progress on that, sir."
        prompt = SUMMARISE_PROMPT.format(goal=goal, observations="\n".join(f"- {f}" for f in findings))
        try:
            completion = await self.models.complete(
                Slot.GENERAL,
                [ChatMessage("system", "You report back plainly on completed work."),
                 ChatMessage("user", prompt)],
                max_tokens=300, temperature=0.3,
            )
            if completion.text.strip():
                return completion.text.strip()
        except Exception as exc:
            log.debug("automation summary unavailable: %s", exc)
        return "; ".join(findings[-4:])

    # -- confirmation & narration ---------------------------------------------
    async def _confirm_start(self, goal: str, milestones: list[str]) -> None:
        plan = "; ".join(milestones)
        summary = (
            f"I'll {goal.rstrip('.')}. "
            + (f"That means: {plan}. " if plan and plan != goal else "")
            + "I'll ask again before anything that spends money, deletes something, sends "
              "something, or runs an installer."
        )
        await self.deps.permissions.require(
            action="automation:start", risk=RiskLevel.MEDIUM, summary=summary,
            details={"goal": goal, "milestones": milestones},
        )

    def _announce_milestone(self, request: Request, task, milestone: str) -> None:
        message = f"{milestone}…"
        if task is not None:
            self.deps.tasks.step(task, message, phase="milestone")
        else:
            request.ctx.report(message, phase="milestone")
        self._narrator.phase(message)

    def _announce_step(self, request: Request, task, milestone: str, message: str,
                       expected_ms: int, elapsed_ms: float) -> None:
        if task is not None:
            self.deps.tasks.step(task, message, phase=milestone)
        else:
            request.ctx.report(message, phase=milestone)
        self._narrator.maybe_narrate(message, expected_ms=expected_ms, elapsed_ms=elapsed_ms)


def _short(arguments: dict) -> str:
    return ", ".join(f"{k}={str(v)[:40]}" for k, v in list((arguments or {}).items())[:3])
