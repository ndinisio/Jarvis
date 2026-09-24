"""The operator loop.

    brief → [model: tool calls] → run them → look again → [model] → … → finish

One loop for everything JARVIS does on the computer, foreground or
background. What it adds over "ask a model what to do next" is the
machinery that makes a small local model finish the job:

* **It sees everything.** Results go back to the model in full (a page
  listing with its element handles, not "60 elements found"), and after a
  web action the page is read again automatically, so no step is spent
  looking (:mod:`.observation`).
* **Done means proven.** A task with a checklist can't be finished by
  saying so: every item needs a quote from something JARVIS actually saw
  (:mod:`.checklist`). The model that claims "added to basket" one step
  before clicking Add to Basket is told the item isn't proven, and goes and
  does it.
* **It notices going round in circles** (:mod:`.stuck`), and **fits the
  model's context window**, keeping the front of every request identical so
  a local model's prompt cache is reused (:mod:`.context`).
* **Every action goes through the registry** — the same validation,
  permission gate, consequence check and confirmation as anywhere else in
  JARVIS. The loop never reaches around them; a declined confirmation ends
  the task as an answer, not a failure to route around.
* **Private things stay local** (:mod:`.privacy`).
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ...core.logging import get_logger
from ...models.base import ToolCall, ToolDef, strip_thinking
from ...models.registry import Slot
from ...security import untrusted
from ..catalog import ToolCatalog
from ..schema import Objective
from ..state import ConversationState
from ..verify import Verifier
from .checklist import Checklist
from .context import Conversation, Turn, budget_for
from .observation import OBSERVE_AFTER, OBSERVERS, ObservationLog, first_line
from .privacy import PRIVATE_CATEGORIES, PrivacyGuard
from .prompts import (
    ASK_USER,
    CONTROL_NAMES,
    FINISH,
    GIVE_UP,
    MARK_DONE,
    brief,
    system_prompt,
)
from .stuck import StuckDetector

log = get_logger("jarvis.intelligence.operator")

#: Calls taken from one reply. More than this and the model is guessing ahead
#: of what it has seen.
MAX_CALLS_PER_REPLY = 5
#: Replies without a tool call tolerated before the run is called stalled.
MAX_NUDGES = 2
#: Model errors in a row tolerated before giving up on the model.
MAX_MODEL_FAILURES = 2
#: Unproven attempts to finish, with nothing done in between, before the run
#: is called stalled rather than spending its whole budget on the claim.
MAX_UNPROVEN_FINISHES = 3
#: How much of one result (or page listing) the model is shown.
RESULT_CHARS = 4000
PAGE_CHARS = 5000
#: Categories whose actions can legitimately repeat on a changed screen —
#: covered by stuck detection rather than refused as duplicates.
_INTERACTIVE = frozenset({"browser", "screen"})


@dataclass
class Budget:
    """How far one run may go before it stops and says how far it got."""

    steps: int = 50
    wall_s: float = 600.0
    model_calls: int = 80


class Status:
    FINISHED = "finished"
    ASKED = "asked"
    GAVE_UP = "gave_up"
    DECLINED = "declined"
    BUDGET = "budget"
    STALLED = "stalled"
    UNAVAILABLE = "unavailable"


@dataclass
class StepReport:
    """One action, for progress displays and narration."""

    tool: str
    message: str
    ok: bool
    expected_ms: int
    elapsed_ms: float
    checklist: list[dict[str, Any]]


@dataclass
class OperatorResult:
    status: str
    #: What to tell the user, when the model gave it. Empty means compose one
    #: from ``findings`` — also the case when the model's summary was written
    #: before it had seen the results it describes.
    answer: str = ""
    question: str = ""
    reason: str = ""
    checklist: list[dict[str, Any]] = field(default_factory=list)
    #: One line per action: what ran and what came of it.
    findings: list[str] = field(default_factory=list)
    #: The latest successful result as the model saw it.
    latest: str = ""
    display: dict[str, Any] | None = None
    steps: int = 0
    tool_calls: int = 0
    model_calls: int = 0
    replans: int = 0
    #: Set when the task ran on local models only, and why.
    local_only: str = ""
    #: Every action as it ran — tool, arguments, whether it worked, and the
    #: listing the model was looking at — for learning a skill from it.
    trail: list[dict[str, Any]] = field(default_factory=list)
    #: Skills the operator ran as tools.
    used_skills: list[str] = field(default_factory=list)

    @property
    def done(self) -> list[str]:
        return [item["text"] for item in self.checklist if item.get("done")]

    @property
    def not_done(self) -> list[str]:
        return [item["text"] for item in self.checklist if not item.get("done")]


class Operator:
    def __init__(self, deps, *, slot: str = Slot.OPERATOR, trace=None):
        self.deps = deps
        self.slot = slot
        self.trace = trace
        self.catalog = ToolCatalog(deps.registry)
        self.verifier = Verifier(deps)

    async def run(self, goal: str, ctx, *, tools: list[str] | tuple[str, ...],
                  objective: Objective | None = None, budget: Budget | None = None,
                  background: bool = False, said: str = "", situation: str = "",
                  context: str = "", state: ConversationState | None = None,
                  before: Callable[[str, dict], None] | None = None,
                  after: Callable[[StepReport], None] | None = None,
                  vet: Callable[[str, dict], str | None] | None = None,
                  skills: list | None = None, observed: list[str] | None = None,
                  tips: str = "") -> OperatorResult:
        """Work towards *goal* with *tools* until it is done, proven, or can't be.

        ``background`` holds the task to a checklist even when the
        interpreter gave no explicit criteria (the goal itself is then the
        one item). ``said`` is the user's own words, ``situation`` what's
        going on right now (open pages, recent results, the conversation) and
        ``context`` what's known about the user. ``before``/``after`` are
        told about each action, for progress displays; ``vet`` may stop an
        action with a question for the user instead. ``skills`` are offered
        as tools of their own (a recipe runs several steps in one call);
        ``observed`` is what was already shown before this run — a skill that
        got partway — and counts as proof; ``tips`` are hints about the site
        or app. A stop reaches the loop through ``ctx``
        (:class:`~jarvis.core.errors.Cancelled`).
        """
        run = _Run(self, goal, ctx, objective or Objective(goal=goal), budget or Budget(),
                   background=background, said=said, situation=situation, context=context,
                   state=state or ConversationState(), tools=tools, before=before,
                   after=after, vet=vet, skills=skills or [], tips=tips)
        for text in observed or []:
            run.seen.add(text)
        return await run.execute()


class _Run:
    """One run's state. Short-lived: built and discarded per task."""

    def __init__(self, owner: Operator, goal: str, ctx, objective: Objective, budget: Budget, *,
                 background: bool, said: str, situation: str, context: str,
                 state: ConversationState, tools, before, after, vet, skills: list, tips: str):
        self.deps = owner.deps
        self.models = owner.deps.models
        self.registry = owner.deps.registry
        self.slot = owner.slot
        self.trace = owner.trace
        self.catalog = owner.catalog
        self.verifier = owner.verifier
        self.goal = goal.strip()
        self.ctx = ctx
        self.objective = objective
        self.budget = budget
        self.state = state
        self.before = before
        self.after = after
        self.vet = vet

        self.checklist = Checklist.for_objective(objective, self.goal, background=background)
        self.seen = ObservationLog()
        self.stuck = StuckDetector()
        self.privacy = PrivacyGuard(list(self.deps.config.models.cloud_exclusions))
        self.privacy.check_text(" ".join([self.goal, said, objective.site or "", objective.app or ""]))
        self.privacy.check_situation(situation, frozenset(
            name for category, names in self.registry.by_category().items()
            if category in PRIVATE_CATEGORIES for name in names))

        self.allowed = [name for name in dict.fromkeys(tools) if self.registry.get(name) is not None]
        #: Which "look again" tools are worth running for this toolset.
        self.observers = [name for name, (_, actors, _) in OBSERVERS.items()
                          if self.registry.get(name) is not None
                          and any(tool in actors for tool in self.allowed)]
        #: Skills offered as tools, by the name the model calls them.
        self.skill_tools = {skill.tool_name: skill for skill in skills}
        self.defs: list[ToolDef] = (self.registry.tool_defs(self.allowed)
                                    + [_skill_def(skill) for skill in skills] + self._control_defs())
        self.overhead = sum(len(d.name) + len(d.description) + len(json.dumps(d.parameters)) + 40
                            for d in self.defs)
        conf = self.models.slot_config(self.slot)
        self.conversation = Conversation(
            system=system_prompt(proven=self.checklist.gated),
            brief=brief(self.goal, self.checklist, objective=objective, said=said,
                        situation=situation, background=context, tips=tips,
                        recipes=bool(skills)),
            budget_chars=budget_for(conf.num_ctx, conf.max_tokens),
        )

        self.started = time.monotonic()
        self.steps = self.tool_calls = self.model_calls = 0
        self.nudges = self.model_failures = self.unproven = 0
        self.findings: list[str] = []
        self.latest = ""
        self.display: dict[str, Any] | None = None
        #: The page as last seen — what stuck detection compares against.
        self.screen = ""
        self.hint = ""
        #: Successful state changes, so an identical one isn't repeated.
        self.done_changes: dict[str, str] = {}
        #: The app the latest native action happened in.
        self.app_in_use = ""
        self.trail: list[dict[str, Any]] = []
        self.used_skills: list[str] = []

    def _control_defs(self) -> list[ToolDef]:
        defs = [FINISH, ASK_USER, GIVE_UP]
        if self.checklist.gated:
            defs.insert(1, MARK_DONE)
        return defs

    # ------------------------------------------------------------------
    async def execute(self) -> OperatorResult:
        if self.trace is not None and self.checklist.items:
            self.trace.checklist(self.checklist.as_dicts())
        while True:
            if await self.ctx.checkpoint():
                self._resumed()
            exhausted = self._exhausted()
            if exhausted:
                return self._result(Status.BUDGET, reason=exhausted)

            think = None
            replan = ""
            if self.stuck.needs_replan:
                replan = self.stuck.replanned()
                think = True
                if self.trace is not None:
                    self.trace.recover("replan", "three attempts in a row failed")
            messages = self.conversation.messages(self._status(replan), fixed_overhead=self.overhead)
            self.hint = ""
            try:
                completion = await self.models.chat(
                    self.slot, messages, tools=self.defs, think=think,
                    allow_cloud=self.privacy.allow_cloud)
            except Exception as exc:
                self.model_failures += 1
                log.debug("operator model call failed: %s", exc)
                if self.model_calls == 0 or self.model_failures >= MAX_MODEL_FAILURES:
                    status = Status.UNAVAILABLE if self.model_calls == 0 else Status.STALLED
                    return self._result(status, reason=f"the model is unavailable ({exc})")
                continue
            self.model_calls += 1
            self.model_failures = 0

            text = strip_thinking(completion.text)
            calls = list(completion.tool_calls)[:MAX_CALLS_PER_REPLY]
            if not calls:
                ended = self._text_reply(text)
                if ended is not None:
                    return ended
                continue
            self.nudges = 0
            ended = await self._run_calls(calls, text)
            if ended is not None:
                return ended

    # -- a reply without tool calls -----------------------------------------
    def _text_reply(self, text: str) -> OperatorResult | None:
        if text and (not self.checklist.gated or self.checklist.all_done()):
            # A plain answer — fine for a question, and for a task whose
            # every item is already proven.
            return self._result(Status.FINISHED, answer=text)
        self.nudges += 1
        if self.nudges > MAX_NUDGES:
            return self._result(Status.STALLED, reason="the model stopped taking actions")
        if text:
            self.conversation.add(Turn(calls=[], full=[], short=[], text=text[:600]))
        unmet = ", ".join(str(n) for n in self.checklist.unmet())
        self.hint = ("Reply with tool calls, not prose. "
                     + (f"Checklist item(s) {unmet} aren't proven yet: carry on with the next action, "
                        "or mark_done with a quote if one already happened."
                        if self.checklist.gated else
                        "If you have the answer, call finish with it."))
        return None

    # -- a reply with tool calls ------------------------------------------------
    async def _run_calls(self, calls: list[ToolCall], text: str) -> OperatorResult | None:
        turn = Turn(calls=[], full=[], short=[], text=text[:600])
        digest: list[str] = []
        acted = False          # an action ran in this reply
        due: list[str] = []    # …and which views to look at again after it
        halted = ""            # why the rest of this reply is skipped

        async def look_again() -> None:
            if turn.full:
                for observer in due:
                    listing = await self._observe(observer)
                    if listing:
                        turn.full[-1] += f"\n\n{OBSERVERS[observer][2]}:\n" + listing
            due.clear()

        for index, call in enumerate(calls):
            call = ToolCall(name=str(call.name or ""), arguments=_arguments(call.arguments),
                            id=f"call_{self.model_calls}_{index}")
            turn.calls.append(call)
            if not halted and call.name not in CONTROL_NAMES and self.steps >= self.budget.steps:
                halted = "the action limit for this task has been reached"
            if not halted and call.name not in CONTROL_NAMES and await self.ctx.checkpoint():
                self._resumed()
                halted = "the user paused the task and may have changed things since — look again"
            if halted:
                self._answer(turn, call, f"Not run: {halted}.")
                continue
            if call.name in CONTROL_NAMES:
                await look_again()
                ended, reply = self._control(call, acted)
                if ended is not None:
                    return ended
                self._answer(turn, call, reply)
                continue

            outcome = await self._act(call)
            self._answer(turn, call, outcome.text, outcome.short)
            digest.append(f"{call.name}({_short(call.arguments)}) → "
                          f"{outcome.short or first_line(outcome.text)}")
            if outcome.ended is not None:
                return outcome.ended
            if outcome.ran:
                acted = True
                self.unproven = 0
                due.extend(o for o in self.observers
                           if call.name in OBSERVERS[o][0] and o not in due)
            if not outcome.ok:
                halted = f"{call.name} didn't work, so the rest of that reply was skipped"
        await look_again()
        turn.digest = "; ".join(digest)[:300]
        self.conversation.add(turn)
        return None

    def _resumed(self) -> None:
        """Back from a pause: whatever was on screen may have changed (the
        user may have taken over), so nothing seen before is trusted as
        current and the next look waits in full."""
        from ...tools.browser.observe import acted

        acted()
        self.screen = ""
        self.hint = ("The user paused this task and has just resumed it; they may have changed "
                     "things. Look at the page or window again before acting.")

    def _answer(self, turn: Turn, call: ToolCall, text: str, short: str = "") -> None:
        turn.full.append(text)
        turn.short.append(short or first_line(text))

    # -- control tools ------------------------------------------------------------
    def _control(self, call: ToolCall, acted: bool) -> tuple[OperatorResult | None, str]:
        args = call.arguments
        if call.name == "mark_done":
            item = _as_int(args.get("item"))
            problem = self.checklist.mark(item, str(args.get("evidence") or ""), self.seen)
            if problem:
                return None, problem
            self._publish_checklist()
            left = self.checklist.unmet()
            return None, (f"Item {item} is done." + (f" Still to do: {', '.join(map(str, left))}."
                                                    if left else " Every item is done: call finish."))
        if call.name == "finish":
            if self.checklist.gated:
                quotes = args.get("evidence")
                if isinstance(quotes, str):
                    quotes = [quotes]
                problems = self.checklist.mark_remaining(
                    [str(q) for q in quotes if str(q).strip()] if isinstance(quotes, list) else [],
                    self.seen)
                self._publish_checklist()
                if not self.checklist.all_done():
                    self.unproven += 1
                    if self.unproven >= MAX_UNPROVEN_FINISHES:
                        return self._result(
                            Status.STALLED,
                            reason="it kept saying it was done without being able to show it"), ""
                    left = self.checklist.unmet()
                    return None, ("Not finished — " + "; ".join(
                        f"item {n} ({self.checklist.items[n - 1].text}) isn't proven" for n in left)
                        + ". " + (" ".join(problems) + " " if problems else "")
                        + "Do what's left, or mark_done each item with a quote that proves it.")
            summary = str(args.get("summary") or "").strip()
            # A summary written in the same breath as the actions it describes
            # was written before their results came back: compose it afresh.
            return self._result(Status.FINISHED, answer="" if acted else summary), ""
        if call.name == "ask_user":
            question = str(args.get("question") or "").strip()
            if not question:
                return None, "ask_user needs the question to ask."
            return self._result(Status.ASKED, question=question), ""
        reason = str(args.get("reason") or "").strip() or "it can't be done"
        return self._result(Status.GAVE_UP, reason=reason), ""

    # -- actions --------------------------------------------------------------------
    async def _act(self, call: ToolCall) -> _Outcome:
        name, arguments = call.name, call.arguments
        if name in self.skill_tools:
            return await self._run_skill(self.skill_tools[name], arguments)
        if name not in self.allowed:
            problem = (f"{name} isn't one of the tools for this task" if self.registry.get(name)
                       else f"there is no tool called {name}")
            return self._refuse(name, arguments, problem)
        ok, problem, cleaned = self.catalog.validate_call(name, arguments)
        if not ok:
            return self._refuse(name, arguments, problem)
        spec = self.registry.get(name).spec
        key = f"{name}:{json.dumps(cleaned, sort_keys=True, default=str)}"
        if (spec.changes_state and not spec.safe_to_retry and spec.category not in _INTERACTIVE
                and key in self.done_changes):
            return self._refuse(name, cleaned, "you already did exactly this and it worked "
                                               f"(“{self.done_changes[key]}”); don't repeat it")

        if self.vet is not None:
            question = self.vet(name, cleaned)
            if question:
                return _Outcome(text="", ran=False, ok=False,
                                ended=self._result(Status.ASKED, question=question))
        self.privacy.check_call(name, spec.category, cleaned)
        if self.trace is not None:
            self.trace.decision("tool_call", tool=name, arguments=cleaned)
        if self.before is not None:
            self.before(name, cleaned)
        screen = self.screen if name in OBSERVE_AFTER or name in OBSERVERS else ""
        started = time.monotonic()
        view_before = self.screen
        result = await self.registry.call(name, cleaned, self.ctx)
        elapsed_ms = (time.monotonic() - started) * 1000.0
        self.steps += 1
        self.tool_calls += 1
        self.trail.append({"tool": name, "arguments": dict(cleaned), "ok": bool(result.ok),
                           "view": view_before})
        self.privacy.check_call(name, spec.category, None, result.data)
        if self.trace is not None:
            self.trace.result(self.steps, name, result)
        if result.display:
            self.display = result.display

        if not result.ok and (result.error or "").startswith("confirmation_declined"):
            # The user said no. That's an answer, not an obstacle to route
            # around: asking the model again would just repeat the question.
            finding = f"{name}({_short(cleaned)}) → declined: {result.summary[:160]}"
            self.findings.append(finding)
            if self.trace is not None:
                self.trace.recover("report", "the user declined the action", tool=name)
            self._report(name, result.summary or "Declined.", False, spec.expected_ms, elapsed_ms)
            return _Outcome(text=result.for_model(), ran=True, ok=False,
                            ended=self._result(Status.DECLINED, reason=result.summary))

        verification = await self.verifier.verify(name, cleaned, result, self.objective, self.state)
        if self.trace is not None:
            self.trace.verify(verification)
        text = result.for_model(RESULT_CHARS)
        if result.ok and (spec.category in untrusted.CONTENT_CATEGORIES or name in untrusted.CONTENT_TOOLS):
            # Someone else's words (a page, an email, a file): information,
            # never instructions — see security/untrusted.py.
            text = untrusted.fence_result(text, _source(spec.category, name))
        ok = result.ok and verification.verified
        if result.ok and not verification.verified:
            text += f"\n(Checked afterwards: this did not work — {verification.problem})"
        if ok:
            self.seen.add(result.observation or result.summary)
            self.latest = text
            if spec.changes_state:
                self.done_changes[key] = first_line(result.summary, 80)
        if name in OBSERVERS and result.ok and result.observation:
            self.screen = result.observation
        if spec.category == "screen" and isinstance(result.data, dict) and result.data.get("application"):
            self.app_in_use = str(result.data["application"])
        state = "ok" if ok else ("not verified" if result.ok else "failed")
        problem = verification.problem if result.ok and not verification.verified else result.summary
        self.findings.append(f"{name}({_short(cleaned)}) → {state}: {(problem or '')[:200]}")

        hint = self.stuck.record(screen, name, cleaned, ok)
        if hint:
            self.hint = hint
            if self.trace is not None:
                self.trace.recover("hint", "the same action on an unchanged page", tool=name)
        self._report(name, (result.summary or ("done" if result.ok else "failed"))[:180], ok,
                     spec.expected_ms, elapsed_ms)
        return _Outcome(text=text, short=first_line(text), ran=True, ok=ok)

    async def _run_skill(self, skill, arguments: dict) -> _Outcome:
        """A recipe as one call: its steps run through the registry, grounded
        in what's on screen; it reports how far it got either way."""
        from ...skills.runner import SkillRunner

        library = getattr(self.deps, "skills", None)
        params = library.parameters(skill, self.objective, given=arguments) if library else None
        if params is None:
            needed = ", ".join(p.name for p in skill.params if p.required and not p.default)
            return self._refuse(skill.tool_name, arguments, f"{skill.title} needs {needed}")
        if self.trace is not None:
            self.trace.decision("tool_call", tool=skill.tool_name, arguments=params)
        if self.before is not None:
            self.before(skill.tool_name, params)
        started = time.monotonic()
        outcome = await SkillRunner(self.deps, self.ctx).run(skill, params, view=self.screen)
        if library is not None:
            library.record(skill, outcome.ok)
        self.used_skills.append(skill.id)
        self.steps += max(1, outcome.actions)
        self.tool_calls += outcome.actions
        for text in outcome.observations:
            self.seen.add(text)
        if outcome.view:
            self.screen = outcome.view
            self.latest = outcome.view
        state = "ok" if outcome.ok else "stopped"
        self.findings.append(f"{skill.tool_name}({_short(params)}) → {state}: {outcome.account()[:200]}")
        self._report(skill.tool_name, skill.title + (" — done" if outcome.ok else " — stopped partway"),
                     outcome.ok, 0, (time.monotonic() - started) * 1000.0)
        if outcome.declined:
            return _Outcome(text=outcome.reason, ran=True, ok=False,
                            ended=self._result(Status.DECLINED, reason=outcome.reason))
        text = ("The recipe finished. " if outcome.ok else "The recipe stopped partway. ") + outcome.account()
        if outcome.evidence:
            text += f" The page showed “{outcome.evidence}”."
        if outcome.view:
            text += "\n\nWhat's on screen now:\n" + outcome.view[:PAGE_CHARS]
        return _Outcome(text=text, short=first_line(text), ran=True, ok=outcome.ok)

    def _refuse(self, name: str, arguments: dict, problem: str) -> _Outcome:
        if self.trace is not None:
            self.trace.step(self.steps, name, arguments, f"rejected: {problem}")
        self.stuck.record("", name, arguments, False)
        return _Outcome(text=f"Not run: {problem}.", ran=False, ok=False)

    async def _observe(self, observer: str) -> str:
        """Look again after an action — the page, or the window of the app
        just acted in (which needn't be the one in front)."""
        arguments = {"app": self.app_in_use} if observer == "read_window" and self.app_in_use else {}
        looked = await self.registry.call(observer, arguments, self.ctx)
        if not (looked.ok and looked.observation):
            return ""
        self.screen = looked.observation
        self.seen.add(looked.observation)
        listing = looked.for_model(PAGE_CHARS)
        self.latest = listing
        category = self.registry.get(observer).spec.category
        self.privacy.check_call(observer, category, None, looked.data)
        return listing

    # -- bookkeeping ----------------------------------------------------------------
    def _exhausted(self) -> str:
        if self.steps >= self.budget.steps:
            return f"reached the limit of {self.budget.steps} actions"
        if self.model_calls >= self.budget.model_calls:
            return f"reached the limit of {self.budget.model_calls} model calls"
        if time.monotonic() - self.started > self.budget.wall_s:
            return f"ran out of time ({self.budget.wall_s:.0f} s)"
        return ""

    def _status(self, replan: str) -> str:
        parts = []
        if self.checklist.gated:
            parts.append("Checklist now:\n" + self.checklist.render())
        if self.steps:
            parts.append(f"Actions used: {self.steps} of {self.budget.steps}.")
        if self.hint:
            parts.append(self.hint)
        if replan:
            parts.append(replan)
        return "\n\n".join(parts)

    def _report(self, tool: str, message: str, ok: bool, expected_ms: int, elapsed_ms: float) -> None:
        if self.after is None:
            return
        try:
            self.after(StepReport(tool=tool, message=message, ok=ok, expected_ms=expected_ms,
                                  elapsed_ms=elapsed_ms, checklist=self.checklist.as_dicts()))
        except Exception:  # pragma: no cover - a progress display must never break the task
            log.exception("operator progress report failed")

    def _publish_checklist(self) -> None:
        if self.trace is not None and self.checklist.items:
            self.trace.checklist(self.checklist.as_dicts())

    def _result(self, status: str, *, answer: str = "", question: str = "",
                reason: str = "") -> OperatorResult:
        return OperatorResult(
            status=status, answer=answer.strip(), question=question, reason=reason,
            checklist=self.checklist.as_dicts() if self.checklist.gated else [],
            findings=list(self.findings), latest=self.latest, display=self.display,
            steps=self.steps, tool_calls=self.tool_calls, model_calls=self.model_calls,
            replans=self.stuck.replans, local_only=self.privacy.reason,
            trail=list(self.trail), used_skills=list(self.used_skills),
        )


@dataclass
class _Outcome:
    text: str
    short: str = ""
    ran: bool = False
    ok: bool = True
    ended: OperatorResult | None = None


def _skill_def(skill) -> ToolDef:
    return ToolDef(name=skill.tool_name,
                   description=f"Recipe: {skill.description.rstrip('.')}. Several steps in one call.",
                   parameters=skill.parameter_schema())


_SOURCES = {"research": "the web", "email": "the email", "messages": "the messages",
            "files": "the file", "clipboard": "the clipboard", "calendar": "the calendar",
            "contacts": "the contacts", "reminders": "the reminders"}


def _source(category: str, tool: str) -> str:
    if tool == "get_current_page":
        return "the page"
    if category == "screen":
        return "the screen"
    return _SOURCES.get(category, "the content")


def _arguments(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except ValueError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _as_int(value: Any) -> int:
    try:
        return int(str(value).strip().lstrip("#"))
    except (TypeError, ValueError):
        return 0


def _short(arguments: dict) -> str:
    """The arguments that say something — defaults left empty are noise."""
    given = [(k, v) for k, v in (arguments or {}).items() if v not in ("", None, [], {})]
    return ", ".join(f"{k}={str(v)[:40]}" for k, v in given[:3])


__all__ = ["Budget", "Operator", "OperatorResult", "Status", "StepReport"]
