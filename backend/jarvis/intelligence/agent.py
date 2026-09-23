"""The execution loop.

    triage → understand → decide → act → observe → verify → repair or continue → respond

V1.3 adds one step in front: :class:`~.triage.IntentTriage` is the single
semantic authority for chat vs. action, so a domain word mentioned in passing
("I hate dealing with email") never reaches the tool machinery below it. Once
triage says "action", each decision is made against what the last tool
actually returned, which is what separates V1.2 from V1.1's "one sentence, one
route, one action". The loop is bounded by a configurable step budget so it
cannot run away, and it never reaches around the permission broker: every tool
call goes through the same registry, with the same confirmations, as V1.1.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..core.errors import Cancelled
from ..core.logging import get_logger
from ..models.base import ChatMessage
from ..tools.base import ToolResult
from .catalog import ToolCard, ToolCatalog
from .entities import ReferenceResolver
from .observability import Trace
from .planner import Planner, fallback_plan
from .recovery import RecoveryManager
from .schema import (
    AgentDecision,
    Complexity,
    Confidence,
    Objective,
    Plan,
    load,
)
from .state import ConversationState, PendingClarification, Turn
from .triage import IntentTriage, objective_sufficient
from .understanding import Understanding
from .verify import Verifier

log = get_logger("jarvis.intelligence.agent")

DECISION_PROMPT = """Decide the single next action towards the objective.

Objective: {goal}
Details: kind={kind}; targets={targets}; constraints={constraints}

{context}
{plan}
What has happened so far this turn:
{observations}
{view}
Tools you may use:
{tools}

Reply with JSON only, one of:
{{"action": "tool_call", "tool": "<name>", "arguments": {{...}}, "reason": "<short>"}}
{{"action": "clarify", "question": "<one short question>", "reason": "<short>"}}
{{"action": "respond", "content": "<the answer for the user>", "reason": "<short>"}}
{{"action": "complete", "reason": "<why nothing more is needed>"}}

Rules:
- Use a tool only if it moves the objective forward; the results above may already answer it.
- Use information already gathered rather than fetching it again.
- clarify only when you genuinely cannot proceed without the user.
- respond when you can answer now. Answer from the results above, not from guesses.
- To act on a web page, use the [handle] shown next to an element in the last result. Never make up a handle."""

FINAL_PROMPT = """Give the user the answer, in one or two sentences unless detail was asked for.

What they wanted: {goal}

What you found:
{observations}

Answer directly. Do not describe your process, mention tools, or use headings."""


@dataclass
class AgentOutcome:
    text: str = ""
    spoken: str | None = None
    display: dict[str, Any] | None = None
    clarification: str | None = None
    objective: Objective | None = None
    plan: Plan | None = None
    steps: int = 0
    tool_calls: int = 0
    model_calls: int = 0
    recovered: int = 0
    error: str | None = None
    #: True when ``text`` was already delivered token by token.
    streamed: bool = False
    trace: list[dict[str, Any]] = field(default_factory=list)
    #: Set when this turn belongs to a capability better suited to run it
    #: than this loop — currently only "automation" (see the handoff in
    #: run(), just before the shortlist is built). When set, every other
    #: field except ``objective`` is meaningless; the orchestrator re-routes
    #: instead of composing an answer from this outcome.
    handoff: str | None = None


class IntelligenceAgent:
    """Runs one user turn through the full loop."""

    def __init__(self, deps, models, state: ConversationState, *, max_steps: int = 6,
                 recovery_budget: int = 2, reasoning_slot: str = "reasoning",
                 publish_trace: bool = True):
        self.deps = deps
        self.models = models
        self.state = state
        self.max_steps = max_steps
        self.publish_trace = publish_trace
        self.catalog = ToolCatalog(deps.registry)
        self.resolver = ReferenceResolver()
        self.triage = IntentTriage(models, reasoning_slot)
        self.understanding = Understanding(models, self.resolver, reasoning_slot)
        self.planner = Planner(models, reasoning_slot, max_steps=max_steps)
        self.verifier = Verifier(deps)
        self.recovery = RecoveryManager(models, self.catalog, reasoning_slot,
                                        budget=recovery_budget)
        self._slot = reasoning_slot
        self._context = ""

    # ------------------------------------------------------------------
    async def run(self, text: str, ctx, *, task=None, emit=None, stream=None,
                  context: str = "") -> AgentOutcome:
        """One turn.

        ``emit`` narrates progress ("Searching the web…"); ``stream`` receives
        the answer token by token, which is what stops a thoughtful reply from
        feeling like a hang. ``context`` is the layered long-term context the
        orchestrator assembles — identity, preferences, relevant memories.
        """
        trace = Trace(self.deps.bus if self.publish_trace else None, self.deps.telemetry,
                      verbose=self.deps.config.ui.developer_mode)
        outcome = AgentOutcome()
        state = self.state
        pending = state.take_clarification()
        self._context = context

        # A clarification JARVIS asked last turn makes this turn structurally
        # an answer, not a fresh utterance — resume the waiting objective
        # rather than asking triage to guess at a bare "Ada." or "the second
        # one". See Understanding._apply_clarification.
        triage_objective = None
        if pending is None:
            with self.deps.telemetry.span("intelligence.triage"):
                triage = await self.triage.decide(text, state)
            outcome.model_calls += 1
            trace.triage(triage)

            if triage.mode == "chat":
                # The one semantic authority said chat: straight to
                # conversation, never through ToolCatalog or the planner, and
                # no separate Understanding call.
                objective = Objective(goal=text, kind="chat", needs_tools=False,
                                      complexity=Complexity.TRIVIAL,
                                      confidence=Confidence.CONFIDENT)
                outcome.objective = objective
                trace.intent(objective, state)
                return await self._converse(text, objective, outcome, trace, stream)
            triage_objective = triage.objective

        # "Action" means the user appears to be asking for something, not
        # that there is enough to act on (V1.3 §6). Use triage's own
        # objective directly only when it is already confident and complete;
        # otherwise escalate to the full Understanding pass.
        if triage_objective is not None and objective_sufficient(triage_objective):
            objective = self.understanding.finalize(triage_objective, text, state, pending)
        else:
            with self.deps.telemetry.span("intelligence.understand"):
                objective = await self.understanding.understand(text, state, pending)
            outcome.model_calls += 1
        outcome.objective = objective
        state.set_objective(objective.goal)
        trace.intent(objective, state)

        # Impossible or purely conversational requests never touch a tool.
        if objective.confidence == Confidence.IMPOSSIBLE:
            return await self._finish(outcome, trace,
                                      "That isn't something I can do, sir.")
        # A request that needs nothing from the machine is conversation, however
        # much thinking it takes.
        if not objective.needs_tools:
            return await self._converse(text, objective, outcome, trace, stream)

        # A genuinely multi-step app/web operation — search, compare, click
        # through, fill in, download — needs a real Task (cancel_event,
        # progress, a step budget this loop's max_steps was never sized
        # for), not the short decide/verify loop below. Handing off here,
        # before shortlist() runs, means no model spend is wasted on a turn
        # that's about to be re-routed. See orchestrator.py's handling of
        # AgentOutcome.handoff for what happens next.
        if (self.deps.config.capabilities.automation
                and _normalise_kind(objective.kind) == "automation"
                and objective.complexity == Complexity.MULTI_STEP):
            outcome.handoff = "automation"
            outcome.trace = trace.entries
            return outcome

        cards = self.catalog.shortlist(objective, state)

        # An ambiguous reference is a question rather than a guess — when it
        # matters. See _ambiguity_question.
        question = self._ambiguity_question(objective, pending,
                                            consequential=_changes_state(cards))
        if question is not None:
            return self._ask(question, objective, outcome, trace)

        plan = await self.planner.plan(objective, cards, state)
        if plan is None and objective.needs_planning:
            plan = fallback_plan(objective)
        if plan is not None:
            outcome.model_calls += 1
            outcome.plan = plan
            trace.plan(plan)

        attempts: dict[str, int] = {}
        # ``findings`` are things that actually happened and are worth telling
        # the user about. ``notes`` are the loop talking to itself — a rejected
        # call, a failure to parse — which belong in the next decision's prompt
        # and nowhere near the answer.
        findings: list[str] = []
        notes: list[str] = []
        stalled = False
        #: The full model-facing view of the latest result (a page's elements,
        #: a file's contents…) — findings are one line each; this is what the
        #: next decision actually acts on.
        view = ""

        for step in range(1, self.max_steps + 1):
            if ctx is not None and ctx.cancelled():
                raise Cancelled()
            outcome.steps = step

            decision = await self._decide(objective, plan, cards, findings + notes, state, view)
            outcome.model_calls += 1
            if decision is None:
                notes.append("no decision could be read from the model")
                stalled = True
                break

            if decision.action == "clarify":
                return self._ask(decision.question or "Could you be more specific, sir?",
                                 objective, outcome, trace)
            if decision.action == "respond" and decision.content:
                trace.decision(decision)
                return await self._finish(outcome, trace, decision.content)
            if decision.action == "complete":
                trace.decision(decision)
                break

            tool = decision.tool or ""
            ok, problem, arguments = self.catalog.validate_call(tool, decision.arguments)
            if not ok:
                trace.step(step, tool, decision.arguments, f"rejected: {problem}")
                notes.append(f"{tool} could not be called: {problem}")
                attempts[tool] = attempts.get(tool, 0) + 1
                if attempts[tool] > self.recovery.budget:
                    break
                continue

            # Last chance to ask, at the point where the consequence is. An
            # objective that looked like a read can still end up proposing a
            # send; the reference is no more resolved than it was.
            card = self.catalog.card(tool)
            if card is not None and (card.mutates or card.confirms):
                question = self._ambiguity_question(objective, pending, consequential=True)
                if question is not None:
                    return self._ask(question, objective, outcome, trace)

            trace.decision(decision)
            # The result lands in ``state`` through the registry observer
            # (intelligence.state.attach), so it is recorded exactly once no
            # matter which path made the call.
            result = await self._execute(tool, arguments, ctx, task, emit)
            outcome.tool_calls += 1
            finding_index = len(findings)
            findings.append(f"{tool}({_short(arguments)}) → "
                            f"{'ok' if result.ok else 'failed'}: {result.summary[:200]}")
            if result.observation or result.ok:
                view = result.for_model(4000)
            trace.result(step, tool, result)
            if result.display:
                outcome.display = result.display

            verification = await self.verifier.verify(tool, arguments, result, objective, state)
            trace.verify(verification)
            if verification.verified:
                Planner.advance(plan, tool)
                if plan is not None and not plan.pending:
                    break
                continue

            # Something went wrong — decide once, within budget. The tool's
            # own "ok" was optimistic; correct the finding in place so
            # whatever composes the final answer never sees a success claim
            # that verification has already disproved (a real gap this
            # closes: "opened" is not "verified open", and JARVIS must not
            # say the first when only the second was checked).
            problem = verification.problem or result.summary
            findings[finding_index] = (f"{tool}({_short(arguments)}) → "
                                       f"not verified: {problem[:200]}")
            attempts[tool] = attempts.get(tool, 0) + 1
            recovery = await self.recovery.decide(objective, tool, arguments, result,
                                                  verification, cards, attempts[tool])
            outcome.model_calls += 1
            outcome.recovered += 1
            trace.recover(recovery)
            notes.append(f"that didn't work: {problem}")

            if recovery.strategy == "ask_user":
                return self._ask(recovery.question or "How would you like me to proceed, sir?",
                                 objective, outcome, trace)
            if recovery.strategy == "report":
                break
            if recovery.strategy == "alternative_tool" and recovery.tool:
                extra = self.catalog.card(recovery.tool)
                if extra and extra not in cards:
                    cards.insert(0, extra)
            # retry / modify_arguments simply continue the loop, which will
            # decide again with the failure now visible in the observations.

        return await self._compose(text, objective, findings, outcome, trace, stream,
                                   stalled=stalled)

    # ------------------------------------------------------------------
    async def _decide(self, objective: Objective, plan: Plan | None,
                      cards: list[ToolCard], observations: list[str],
                      state: ConversationState, view: str = "") -> AgentDecision | None:
        prompt = DECISION_PROMPT.format(
            goal=objective.goal,
            kind=objective.kind,
            targets=", ".join(objective.targets) or "none",
            constraints=", ".join(objective.constraints) or "none",
            context=state.describe_for_model(include_turns=2) or "(no prior context)",
            plan=(f"\nPlan: {plan.summary()}\n" if plan else ""),
            observations="\n".join(f"- {o}" for o in observations) or "- nothing yet",
            view=f"\nWhat the last result showed:\n{view}\n" if view else "",
            tools=self.catalog.render(cards),
        )
        try:
            data = await self.models.complete_json(
                self._slot,
                [ChatMessage("system", "You choose the next action. JSON only."),
                 ChatMessage("user", prompt)],
                max_tokens=400,
                timeout_s=40.0,
            )
        except Exception as exc:
            log.debug("decision model unavailable: %s", exc)
            return None
        decision = load(AgentDecision, data)
        if decision is None:
            return None
        problem = decision.validate_shape()
        if problem:
            log.debug("malformed decision: %s", problem)
            return None
        return decision

    async def _execute(self, tool: str, arguments: dict, ctx, task, emit) -> ToolResult:
        """Run a tool through the normal registry — permissions included."""
        if emit:
            emit(f"{_humanise(tool)}…")
        context = ctx if ctx is not None else self.deps.tool_context(task=task)
        with self.deps.telemetry.span("intelligence.tool", tool=tool):
            return await self.deps.registry.call(tool, arguments, context)

    # -- endings -----------------------------------------------------------
    async def _compose(self, text: str, objective: Objective, findings: list[str],
                       outcome: AgentOutcome, trace: Trace, stream=None, *,
                       stalled: bool = False) -> AgentOutcome:
        """Say what happened — or fall back to simply answering.

        The user never sees the loop's own bookkeeping. And a request the agent
        couldn't turn into a decision is not a failure to report: it is usually
        a question, so it goes to the conversation path rather than producing an
        apology. Genuine unavailability is reported there, where it is true.
        """
        if not findings and stalled:
            return await self._converse(text, objective, outcome, trace, stream)
        if not findings:
            return await self._finish(outcome, trace,
                                      "I wasn't able to make progress on that, sir.")
        prompt = FINAL_PROMPT.format(goal=objective.goal,
                                     observations="\n".join(f"- {o}" for o in findings))
        answer = await self._generate(
            [ChatMessage("system", self._persona()), ChatMessage("user", prompt)],
            outcome, stream, max_tokens=400, temperature=0.3)
        if not answer:
            # No model to phrase it with. The tools already wrote summaries fit
            # to be spoken, so use the last one that worked.
            answer = next((f.split(": ", 1)[-1].strip()
                           for f in reversed(findings) if "\u2192 ok:" in f), "")
        return await self._finish(outcome, trace,
                                  answer or "I wasn't able to make progress on that, sir.")

    async def _converse(self, text: str, objective: Objective, outcome: AgentOutcome,
                        trace: Trace, stream=None) -> AgentOutcome:
        """No tools needed — answer from the conversation itself.

        Genuine continuity comes from the real message history built below,
        not from a description of ongoing task state — see ``_persona``.
        """
        messages = [ChatMessage("system", self._persona())]
        history = [t for t in list(self.state.turns)[-7:]]
        if not history or history[-1].role != "user":
            history.append(Turn("user", text, self.state.turn))
        for turn in history:
            messages.append(ChatMessage(turn.role, turn.text[:800]))
        answer = await self._generate(messages, outcome, stream, max_tokens=400)
        if not answer and outcome.error:
            answer = ("The local AI service isn't available, sir. Start Ollama and I'll "
                      "pick this up — direct commands still work in the meantime.")
        return await self._finish(outcome, trace, answer or "I'm not sure, sir.")

    async def _generate(self, messages: list[ChatMessage], outcome: AgentOutcome, stream,
                        *, max_tokens: int = 400, temperature: float | None = None) -> str:
        """Produce prose, streamed when someone is listening for it.

        Streaming is not decoration: the answer starts arriving in a few hundred
        milliseconds instead of after the whole completion, which is most of the
        difference between "thinking" and "hung".
        """
        try:
            if stream is None:
                completion = await self.models.complete(
                    self._slot, messages, max_tokens=max_tokens, temperature=temperature)
                outcome.model_calls += 1
                return completion.text.strip()
            parts: list[str] = []
            async for delta in self.models.stream(self._slot, messages, max_tokens=max_tokens,
                                                  temperature=temperature):
                parts.append(delta)
                stream(delta)
            outcome.model_calls += 1
            text = "".join(parts).strip()
            outcome.streamed = bool(text)
            return text
        except Exception as exc:
            log.debug("generation unavailable: %s", exc)
            outcome.error = str(exc)
            outcome.streamed = False
            return ""

    def _ask(self, question: str, objective: Objective, outcome: AgentOutcome,
             trace: Trace) -> AgentOutcome:
        self.state.ask(PendingClarification(
            question=question, objective_goal=objective.goal,
            purpose=objective.goal, slot=(objective.missing[0] if objective.missing else ""),
        ))
        trace.clarify(question)
        outcome.text = question
        outcome.clarification = question
        outcome.trace = trace.entries
        self.state.note_assistant(question)
        return outcome

    async def _finish(self, outcome: AgentOutcome, trace: Trace, text: str) -> AgentOutcome:
        outcome.text = text
        outcome.trace = trace.entries
        trace.complete(outcome)
        self.state.note_assistant(text)
        return outcome

    # -- helpers -----------------------------------------------------------
    def _ambiguity_question(self, objective: Objective,
                            pending: PendingClarification | None, *,
                            consequential: bool) -> str | None:
        """Ask when a reference could mean several things *and it matters*.

        Misreading "anything from my brother?" costs a wrong answer, and the
        user corrects it in four words. Misreading "email him" sends a message
        to the wrong person, and nobody can take that back. So low-risk
        ambiguity is inferred from context and consequential ambiguity is a
        question — the rule the V1.2 brief states, decided here by the same
        shortlist that picks the tool rather than by a list of dangerous verbs.
        """
        if pending is not None:
            return None            # this turn is the answer; don't ask again
        if objective.confidence != Confidence.AMBIGUOUS:
            return None
        if not consequential:
            return None
        for reference in objective.references:
            if reference.resolved:
                continue
            resolution = self.resolver.resolve(reference.text, self.state, reference.kind)
            options = resolution.candidate_labels()
            if len(options) > 1:
                return (f"Which {_kind_noun(reference.kind)} do you mean — "
                        f"{_join(options)}?")
            if not options:
                return f"Who or what do you mean by “{reference.text}”, sir?"
        if objective.missing:
            return f"What {objective.missing[0]} should I use, sir?"
        return None

    def _persona(self) -> str:
        """The identity layer plus long-term context (preferences, relevant
        memory — never task/execution state).

        Deliberately never includes :meth:`ConversationState.describe_for_model`
        here: that block carries the active objective, browser/email/research
        state and recent actions, which is exactly the stale-task material
        that must not leak into a chat reply or a composed answer. Chat gets
        genuine conversational continuity separately, from the real message
        history built in :meth:`_converse`; the machinery that legitimately
        needs the fuller state (Understanding, the planner, the decision
        loop) reads it directly from ``state``, not through the persona.
        """
        from ..core.personality import Personality

        return Personality(self.deps.config).system_prompt(self._context)


def _normalise_kind(kind: str) -> str:
    """``Objective.kind`` is free-form (no validator, unlike ``complexity``/
    ``confidence`` — see its docstring), so the model returning
    "Automation", trailing whitespace, or similar despite the prompt's
    exact-string instruction must not silently defeat the handoff check."""
    return (kind or "").strip().lower()


def _changes_state(cards: list[ToolCard]) -> bool:
    """Would the best-matching tool for this objective change something?"""
    return bool(cards) and (cards[0].mutates or cards[0].confirms)


def _short(arguments: dict) -> str:
    return ", ".join(f"{k}={str(v)[:40]}" for k, v in list((arguments or {}).items())[:3])


def _humanise(tool: str) -> str:
    return tool.replace("_", " ").capitalize()


def _kind_noun(kind: str) -> str:
    return {"person": "person", "email": "message", "url": "page", "app": "application",
            "file": "file", "result": "result"}.get(kind, "one")


def _join(options: list[str]) -> str:
    if len(options) == 2:
        return f"{options[0]} or {options[1]}"
    return ", ".join(options[:-1]) + f", or {options[-1]}"
