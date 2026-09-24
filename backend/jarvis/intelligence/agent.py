"""One user turn, understood and carried out.

    understand → converse, or act → respond

The interpreter (:class:`~.triage.IntentTriage`) is the single semantic
authority for chat vs. action, so a domain word mentioned in passing ("I hate
dealing with email") never reaches the tool machinery below it. An action
runs on the operator (:mod:`.operator`) — the same loop a background errand
uses — with the tools shortlisted for this objective and a short budget; a
genuine multi-step errand is handed to a background ``Task`` instead (see the
handoff in :meth:`IntelligenceAgent.run`). Either way every tool call goes
through the same registry, with the same confirmations, as everything else.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..core.logging import get_logger
from ..models.base import ChatMessage
from ..models.registry import Slot
from .catalog import ToolCard, ToolCatalog
from .entities import ReferenceResolver
from .observability import Trace
from .operator import Budget, Operator, OperatorResult, Status
from .schema import Complexity, Confidence, Objective
from .state import ConversationState, PendingClarification, Turn
from .triage import IntentTriage, objective_sufficient
from .understanding import Understanding

log = get_logger("jarvis.intelligence.agent")

FINAL_PROMPT = """Give the user the answer, in one or two sentences unless detail was asked for.

What they wanted: {goal}

What you found:
{observations}
{latest}{ending}
Answer directly, from what was found — never from guesses. Say plainly if something wasn't done.
Do not describe your process, mention tools, or use headings."""

#: How much of the last result an answer is composed from.
_LATEST_CHARS = 1500


@dataclass
class AgentOutcome:
    text: str = ""
    spoken: str | None = None
    display: dict[str, Any] | None = None
    clarification: str | None = None
    objective: Objective | None = None
    #: What "done" meant, and which of it was proven (operator runs only).
    checklist: list[dict[str, Any]] = field(default_factory=list)
    steps: int = 0
    tool_calls: int = 0
    model_calls: int = 0
    replans: int = 0
    error: str | None = None
    #: True when ``text`` was already delivered token by token.
    streamed: bool = False
    trace: list[dict[str, Any]] = field(default_factory=list)
    #: Set when this turn belongs to something better suited to run it than
    #: this loop — "automation" (a multi-step errand; see the handoff in
    #: run(), just before the shortlist is built) or "quick" (the request,
    #: restated plainly by the interpreter, is a deterministic command; see
    #: ``quick_decision``). When set, every other field except ``objective``
    #: is meaningless; the orchestrator re-routes instead of composing an
    #: answer from this outcome.
    handoff: str | None = None
    #: The fast-path route for a "quick" handoff.
    quick_decision: Any = None


class IntelligenceAgent:
    """Runs one user turn: understand it, then answer or act."""

    def __init__(self, deps, models, state: ConversationState, *, max_steps: int = 6,
                 reasoning_slot: str = "reasoning", publish_trace: bool = True):
        self.deps = deps
        self.models = models
        self.state = state
        self.max_steps = max_steps
        self.publish_trace = publish_trace
        self.catalog = ToolCatalog(deps.registry)
        self.resolver = ReferenceResolver()
        self.triage = IntentTriage(models, reasoning_slot)
        self.understanding = Understanding(models, self.resolver, reasoning_slot)
        self._slot = reasoning_slot
        self._context = ""

    # ------------------------------------------------------------------
    async def run(self, text: str, ctx, *, task=None, emit=None, stream=None,
                  context: str = "", allow_quick: bool = True) -> AgentOutcome:
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
                # conversation, never through the tool machinery, and no
                # separate Understanding call.
                objective = Objective(goal=text, kind="chat", needs_tools=False,
                                      complexity=Complexity.TRIVIAL,
                                      confidence=Confidence.CONFIDENT)
                outcome.objective = objective
                trace.intent(objective, state)
                return await self._converse(text, objective, outcome, trace, stream)
            triage_objective = triage.objective
            # "Could you pop a new tab open" restated as "open a new tab" is
            # a deterministic command: hand it to the fast path rather than
            # spend further model calls on it. Never for a multi-step errand,
            # and never twice (a rescued quick route comes back with
            # allow_quick=False).
            quick = _quick_route(triage, text) if allow_quick else None
            if quick is not None:
                outcome.handoff = "quick"
                outcome.quick_decision = quick
                outcome.objective = triage_objective
                outcome.trace = trace.entries
                return outcome

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

        # A multi-step errand — search, compare, click through, fill in,
        # download, then report — runs as a real background Task: the user
        # can keep talking, "stop" reaches it, and it gets an errand-sized
        # budget. Handing off here, before anything else is spent on the
        # turn, is what the orchestrator's AgentOutcome.handoff handling
        # picks up (and capabilities/automation.py runs).
        if (self.deps.config.capabilities.automation
                and objective.complexity == Complexity.MULTI_STEP):
            outcome.handoff = "automation"
            # This turn's part is done; the errand reports as its own task.
            trace.complete(outcome)
            outcome.trace = trace.entries
            return outcome

        cards = self.catalog.shortlist(objective, state)

        # An ambiguous reference is a question rather than a guess — when it
        # matters. See _ambiguity_question.
        question = self._ambiguity_question(objective, pending,
                                            consequential=_changes_state(cards))
        if question is not None:
            return self._ask(question, objective, outcome, trace)

        result = await self._operate(text, objective, cards, ctx, pending, emit, trace)
        outcome.steps = result.steps
        outcome.tool_calls = result.tool_calls
        outcome.model_calls += result.model_calls
        outcome.replans = result.replans
        outcome.checklist = result.checklist
        if result.display:
            outcome.display = result.display

        if result.status == Status.ASKED:
            return self._ask(result.question, objective, outcome, trace)
        if result.status == Status.DECLINED:
            return await self._finish(outcome, trace,
                                      result.reason or "Understood — I've left it alone.")
        if result.status == Status.FINISHED and result.answer:
            return await self._finish(outcome, trace, result.answer)
        if result.status in (Status.UNAVAILABLE, Status.STALLED) and not result.findings:
            # No action was ever decided on. That's usually a question the
            # model would rather just answer — or a model that isn't there,
            # which the conversation path reports truthfully.
            return await self._converse(text, objective, outcome, trace, stream)
        return await self._compose(text, objective, result, outcome, trace, stream)

    # ------------------------------------------------------------------
    async def _operate(self, text: str, objective: Objective, cards: list[ToolCard], ctx,
                       pending: PendingClarification | None, emit, trace: Trace) -> OperatorResult:
        """Carry out a short action or question on the operator, with the
        tools shortlisted for it and a turn-sized budget."""

        def before(tool: str, arguments: dict) -> None:
            if emit:
                emit(f"{_humanise(tool)}…")

        def vet(tool: str, arguments: dict) -> str | None:
            # Last chance to ask, at the point where the consequence is. An
            # objective that looked like a read can still end up proposing a
            # send; the reference is no more resolved than it was.
            card = self.catalog.card(tool)
            if card is not None and (card.mutates or card.confirms):
                return self._ambiguity_question(objective, pending, consequential=True)
            return None

        operator = Operator(self.deps, slot=Slot.OPERATOR, trace=trace)
        context = ctx if ctx is not None else self.deps.tool_context()
        tools = _with_companions([card.name for card in cards], self.deps.registry)
        budget = Budget(steps=self.max_steps, wall_s=max(60.0, 20.0 * self.max_steps),
                        model_calls=self.max_steps + 4)
        situation = self.state.describe_for_model(include_turns=2)
        library = self.deps.skills
        observed: list[str] = []
        if library is not None:
            recipe = await self._recipe(library, objective, text, context, emit)
            if isinstance(recipe, OperatorResult):
                return recipe
            if recipe is not None:
                skill, outcome = recipe
                observed = outcome.observations
                situation = "\n\n".join(part for part in (
                    situation, f"A recipe (“{skill.title}”) started on this. {outcome.account()}",
                    f"What's on screen now:\n{outcome.view}" if outcome.view else "") if part)
        with self.deps.telemetry.span("intelligence.operate"):
            return await operator.run(
                objective.goal or text, context, tools=tools,
                objective=objective, budget=budget, background=False, said=text,
                situation=situation, context=self._context, state=self.state, before=before,
                vet=vet, skills=library.offer(objective, text) if library is not None else [],
                observed=observed,
                tips=library.knowledge(objective, text) if library is not None else "")

    async def _recipe(self, library, objective: Objective, text: str, ctx, emit):
        """Run the skill that fits, if one does: a finished OperatorResult,
        ``(skill, outcome)`` when it stopped partway, or None."""
        from ..skills.runner import SkillRunner, summary_for

        found = library.direct(objective, text)
        if found is None:
            return None
        skill, params = found
        with self.deps.telemetry.span("intelligence.skill", skill=skill.id):
            outcome = await SkillRunner(self.deps, ctx, report=emit).run(skill, params)
        library.record(skill, outcome.ok)
        if outcome.ok:
            return OperatorResult(status=Status.FINISHED, answer=summary_for(skill, params, outcome),
                                  findings=outcome.done, steps=outcome.actions,
                                  tool_calls=outcome.actions, used_skills=[skill.id])
        if outcome.declined:
            return OperatorResult(status=Status.DECLINED, reason=outcome.reason, findings=outcome.done,
                                  steps=outcome.actions, tool_calls=outcome.actions,
                                  used_skills=[skill.id])
        return skill, outcome

    # -- endings -----------------------------------------------------------
    async def _compose(self, text: str, objective: Objective, result: OperatorResult,
                       outcome: AgentOutcome, trace: Trace, stream=None) -> AgentOutcome:
        """Say what happened, from what actually happened.

        The user never sees the loop's own bookkeeping, and never a success
        claim that verification disproved: findings record "not verified"
        for those, and the prompt says to be plain about what wasn't done.
        """
        if not result.findings:
            return await self._finish(outcome, trace,
                                      "I wasn't able to make progress on that, sir.")
        ending = {
            Status.GAVE_UP: f"It couldn't be done: {result.reason}",
            Status.BUDGET: f"Work stopped before the end: {result.reason}",
            Status.STALLED: "Work stopped before the end.",
        }.get(result.status, "")
        latest = result.latest[:_LATEST_CHARS]
        prompt = FINAL_PROMPT.format(
            goal=objective.goal,
            observations="\n".join(f"- {o}" for o in result.findings[-12:]),
            latest=f"\nThe last result in full:\n{latest}\n" if latest else "",
            ending=f"\nHow it ended: {ending}\n" if ending else "",
        )
        answer = await self._generate(
            [ChatMessage("system", self._persona()), ChatMessage("user", prompt)],
            outcome, stream, max_tokens=400, temperature=0.3)
        if not answer:
            # No model to phrase it with. The tools already wrote summaries fit
            # to be spoken, so use the last one that worked.
            answer = next((f.split(": ", 1)[-1].strip()
                           for f in reversed(result.findings) if "\u2192 ok:" in f), "")
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


def _quick_route(triage, text: str):
    """The fast-path route for the interpreter's plain restatement, if any."""
    from ..router.quick import QuickCommands
    from ..router.schema import RouteKind

    command = (triage.normalized_command or "").strip()
    if not command or command.lower() == (text or "").strip().lower():
        return None
    objective = triage.objective
    if objective is not None and objective.complexity == Complexity.MULTI_STEP:
        return None
    decision = QuickCommands().match(command)
    if decision is None or decision.kind == RouteKind.CONTROL:
        return None
    decision.reason = f"interpreted as “{command[:60]}”"
    return decision


#: A tool that acts on handles is no use without the tool that shows them,
#: and the handle-based app tools are what a label-based one really wants.
_COMPANIONS: tuple[tuple[frozenset[str], tuple[str, ...]], ...] = (
    (frozenset({"click_page_element", "fill_page_field", "submit_page_form", "press_page_key",
                "scroll_page"}), ("read_page_manifest",)),
    (frozenset({"click_element", "click_control", "type_into", "choose_option", "drag_control",
                "choose_menu_item", "wait_for_element"}),
     ("read_window", "click_control", "type_into", "choose_menu_item")),
    (frozenset({"click_mark", "find_on_screen"}), ("mark_screen", "click_mark")),
)


def _with_companions(tools: list[str], registry) -> list[str]:
    out = list(tools)
    for triggers, companions in _COMPANIONS:
        if any(tool in triggers for tool in tools):
            out.extend(c for c in companions if c not in out and registry.get(c) is not None)
    return out


def _changes_state(cards: list[ToolCard]) -> bool:
    """Would the best-matching tool for this objective change something?"""
    return bool(cards) and (cards[0].mutates or cards[0].confirms)


def _humanise(tool: str) -> str:
    return tool.replace("_", " ").capitalize()


def _kind_noun(kind: str) -> str:
    return {"person": "person", "email": "message", "url": "page", "app": "application",
            "file": "file", "result": "result"}.get(kind, "one")


def _join(options: list[str]) -> str:
    if len(options) == 2:
        return f"{options[0]} or {options[1]}"
    return ", ".join(options[:-1]) + f", or {options[-1]}"
