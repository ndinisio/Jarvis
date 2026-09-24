"""The orchestrator.

One turn of conversation, end to end:

    transcript → route → execute → respond → speak → remember

The rules it enforces are the ones the whole product rests on:

* **Latency is proportional to complexity.** A greeting is answered from the
  phrasebook in microseconds; a system fact comes from macOS in milliseconds; a
  quick-matched capability (mail, calendar, research, diagnostics) is
  acknowledged immediately and continues in the background.
* **Long work never blocks the conversation.** A quick-matched capability that
  takes seconds becomes a task, so the user can keep talking while it runs.
* **Speech is a summary, not a recital.** The written answer goes to the UI; the
  spoken one is trimmed to something worth hearing.

**Two paths, on purpose (V1.2, revised V1.3).** A deterministic quick match is
a certainty, not a guess, so it still runs straight through: "what time is it"
costs a regular expression and a system call, as it did in V1.1. Everything
the quick path declines goes to the intelligence agent, whose own
``IntentTriage`` is now the single semantic authority for chat vs. action
(V1.3) — the route itself no longer guesses a capability or whether to
background the work; that guess used to be spoken as a premature
acknowledgement before the agent had even decided what was being asked. The
agent understands the request in context, decides what to do, watches what
happens and repairs or asks when it goes wrong. Turning ``intelligence.enabled``
off restores V1.1 behaviour exactly — capability classification and a single
action — which makes the new layer measurable against the old one rather than
merely asserted to be better.

Conversational state is owned here and fed by *every* tool call through the
registry observer, so the context behind "reply to the second one" exists
regardless of which path answered the turn before.
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass
from typing import Any

from ..capabilities.base import Capability, Request, Response
from ..intelligence.state import ConversationState, PendingClarification, attach
from ..router.router import Router
from ..router.schema import RouteDecision, RouteKind, RoutePath
from ..tasks.manager import Task
from .context import ContextBuilder
from .errors import Cancelled, ConfirmationDeclined, JarvisError
from .events import AssistantState, EventType
from .logging import get_logger
from .personality import Personality, speakable
from .tracing import current_turn_id, new_turn_id

log = get_logger("jarvis.orchestrator")

#: Quick-matched tools whose destination still needs checking, not just their
#: execution: a deterministic regex can tell "go to bbc.co.uk" apart from
#: chat with certainty, but it cannot tell a 404 from the real page. Needing
#: no model to understand the request is a different property from needing no
#: verification of the result (V1.3 §7).
_VERIFIED_QUICK_TOOLS = {"browse_to"}

#: Tools whose own summary is already a good spoken answer.
_DIRECT_SPEECH_TOOLS = {
    "get_time", "get_battery", "get_storage", "get_memory", "get_cpu", "get_network",
    "get_system_info", "get_processes", "open_application", "close_application",
    "activate_application", "read_clipboard", "write_clipboard", "append_clipboard",
    "set_volume", "browse_to", "open_url", "capture_screen", "create_note", "list_files",
    "workspace_info", "send_notification", "list_applications", "read_calendar",
}


@dataclass
class TurnResult:
    text: str
    spoken: str
    decision: RouteDecision
    duration_ms: float
    task_id: str | None = None
    error: str | None = None


class Orchestrator:
    def __init__(self, deps, router: Router, capabilities: dict[str, Capability],
                 personality: Personality, voice=None):
        self.deps = deps
        self.router = router
        self.capabilities = capabilities
        self.personality = personality
        self.voice = voice
        self.context = ContextBuilder(deps.memory, deps.config)
        self.state = ConversationState()
        self._last_draft: dict[str, Any] | None = None
        self._turn_lock = asyncio.Lock()
        self._agent = None
        self._agent_signature: tuple | None = None
        #: Cancel tokens of foreground turns still running, so "stop"
        #: reaches an action in progress, not only background tasks.
        self._foreground: set[asyncio.Event] = set()
        self.rebind()

    def rebind(self) -> None:
        """Bind to the current tool registry.

        Called at start-up and again whenever configuration rebuilds the
        registry — capabilities can be switched on and off, which replaces the
        tool set. Without this the state observer, and the agent's shortlist,
        would go on pointing at tools that no longer exist.
        """
        attach(self.state, self.deps.registry)
        self._agent = None
        self._agent_signature = None

    # ------------------------------------------------------------------
    # public entry point
    # ------------------------------------------------------------------
    async def handle(self, text: str, *, source: str = "text") -> TurnResult:
        text = (text or "").strip()
        if not text:
            return TurnResult("", "", RouteDecision(RouteKind.CONTROL, "noop"), 0.0)

        # One id per logical request, current for the rest of this turn and
        # for any background task it spawns (asyncio.create_task copies the
        # current context) — see core/tracing.py. This is what lets a stage
        # seen more than once for the same id be told apart from two
        # genuinely separate requests, instead of guessing from timestamps.
        turn_id = new_turn_id()
        log.info("turn_id=%s stage=received source=%s text=%r", turn_id, source, text[:60])

        turn = self.deps.telemetry.mark("turn.total", source=source)
        bus = self.deps.bus
        bus.publish(EventType.TRANSCRIPT, text=text, final=True, source=source)

        # Anything the user says interrupts speech — that is the whole point of
        # barge-in, and it must happen before routing so it feels instant.
        if self.voice is not None and self.deps.config.voice.barge_in:
            await self.voice.stop_speaking()

        await self.deps.memory.add_message("user", text, {"source": source})
        self.state.begin_turn(text)
        bus.emit_state(AssistantState.THINKING)

        try:
            decision = await self.router.route(
                text, context=self._recent_context(), allow_model=True,
                # With no agent and no triage downstream (V1.1 mode), the
                # router itself must still pick a capability — otherwise
                # everything not quick-matched degrades to plain conversation.
                capability_routing=not self.deps.config.intelligence.enabled,
            )
        except Exception as exc:
            log.exception("routing failed")
            decision = RouteDecision(RouteKind.CAPABILITY, "conversation", {"query": text},
                                     confidence=0.3, reason=f"router error: {exc}")

        bus.publish(EventType.ROUTE, **decision.as_dict())
        log.info("turn_id=%s stage=route route=%s:%s via %s (%.1f ms) — %s", turn_id,
                 decision.kind, decision.name, decision.path, decision.latency_ms, text[:60])

        try:
            result = await self._dispatch(text, decision, source)
        except ConfirmationDeclined as exc:
            result = TurnResult(exc.user_message, exc.user_message, decision, 0.0)
        except Cancelled:
            said = self.personality.cancelled()      # one phrase: shown and spoken alike
            result = TurnResult(said, said, decision, 0.0)
        except JarvisError as exc:
            log.warning("turn failed: %s (%s)", exc.user_message, exc.detail)
            result = TurnResult(exc.user_message, exc.user_message, decision, 0.0,
                                error=exc.detail)
        except Exception as exc:
            log.exception("unhandled turn failure")
            message = "Something went wrong with that request, sir."
            result = TurnResult(message, message, decision, 0.0, error=str(exc))
            bus.publish(EventType.ERROR, message=message, detail=str(exc))

        span = turn.stop(route=f"{decision.kind}:{decision.name}", path=decision.path)
        result.duration_ms = span.duration_ms if span else 0.0
        bus.emit_state(AssistantState.IDLE)
        return result

    # ------------------------------------------------------------------
    # dispatch
    # ------------------------------------------------------------------
    async def _dispatch(self, text: str, decision: RouteDecision, source: str) -> TurnResult:
        if decision.kind == RouteKind.CONTROL:
            log.info("turn_id=%s stage=dispatch path=control", current_turn_id())
            return await self._handle_control(text, decision)
        if self._deterministic(decision):
            # A quick match already knows the answer. Sending it through a model
            # would cost seconds and learn nothing.
            if decision.kind == RouteKind.TOOL:
                log.info("turn_id=%s stage=dispatch path=quick_tool tool=%s",
                         current_turn_id(), decision.name)
                return await self._handle_tool(text, decision)
            log.info("turn_id=%s stage=dispatch path=quick_capability capability=%s",
                     current_turn_id(), decision.name)
            return await self._handle_capability(text, decision)
        log.info("turn_id=%s stage=dispatch path=agent", current_turn_id())
        return await self._handle_intelligently(text, decision)

    def _deterministic(self, decision: RouteDecision) -> bool:
        """Should this turn skip the agent?

        Only when the route was a certainty rather than an inference. A quick
        pattern match maps a sentence onto a specific action — "what time is
        it", "open Safari", "mute", "remember that…" — and paying for a model
        there would be latency bought with nothing. Everything else, which is
        most of what anyone actually says, is the agent's.

        A quick match that turns out to be the wrong action doesn't end the turn
        there; see :meth:`_rescue`. With the intelligence layer switched off,
        everything takes V1.1's route.
        """
        if not self.deps.config.intelligence.enabled:
            return True
        return decision.path == RoutePath.QUICK

    # -- control -----------------------------------------------------------
    async def _handle_control(self, text: str, decision: RouteDecision) -> TurnResult:
        name = decision.name
        if name == "cancel":
            return await self._cancel_everything(decision)
        if name == "greeting":
            return await self._respond(self.personality.greeting(), decision)
        if name == "wake":
            return await self._respond(self.personality.wake_response(), decision)
        if name == "thanks":
            return await self._respond(
                self.personality._apply_honorific("A pleasure, sir."), decision
            )
        if name == "presence":
            return await self._respond("Here, sir.", decision)
        if name == "farewell":
            return await self._respond("Goodnight, sir." if _is_night() else "Until later, sir.",
                                       decision)
        if name == "arithmetic":
            value = decision.args.get("value")
            return await self._respond(f"That's {value}.", decision)
        if name == "affirm":
            return await self._handle_affirm(decision)
        if name == "decline":
            resolved = self._resolve_pending(False)
            return await self._respond(
                "Left alone." if resolved else "Understood.", decision
            )
        return await self._dispatch_non_control(text, decision)

    async def _dispatch_non_control(self, text: str, decision: RouteDecision) -> TurnResult:
        """A control name this orchestrator doesn't implement is ordinary work."""
        if not self.deps.config.intelligence.enabled:
            return await self._handle_capability(text, decision)
        return await self._handle_intelligently(text, decision)

    async def _handle_affirm(self, decision: RouteDecision) -> TurnResult:
        if self._resolve_pending(True):
            return await self._respond("Confirmed.", decision, speak=True)
        if self._last_draft:
            draft = self._last_draft
            self._last_draft = None
            result = await self.deps.registry.call("send_email", draft, self._tool_context())
            return await self._respond(result.summary, decision)
        return await self._respond("Nothing is waiting on your approval, sir.", decision)

    def _resolve_pending(self, approved: bool) -> bool:
        pending = self.deps.permissions.pending()
        if not pending:
            return False
        return self.deps.permissions.resolve(pending[-1]["id"], approved)

    async def _cancel_everything(self, decision: RouteDecision) -> TurnResult:
        stopped_speech = False
        if self.voice is not None:
            stopped_speech = await self.voice.stop_speaking()
        cancelled_task = self.deps.tasks.cancel_latest()
        stopped_turns = 0
        for token in list(self._foreground):
            if not token.is_set():
                token.set()
                stopped_turns += 1
        self.deps.permissions.cancel_all("user cancelled")
        if cancelled_task is not None:
            message = f"Stopped — {cancelled_task.title.lower()}."
        elif stopped_speech and not stopped_turns:
            message = ""  # Interrupting speech needs no commentary.
        else:
            message = self.personality.cancelled()
        return await self._respond(message, decision, speak=bool(message))

    # -- single tool -------------------------------------------------------
    async def _handle_tool(self, text: str, decision: RouteDecision) -> TurnResult:
        tool = self.deps.registry.get(decision.name)
        expected = tool.spec.expected_ms if tool else 0
        long_running = decision.long_running or expected > 2500

        if long_running:
            return await self._run_in_background(
                text, decision,
                title=_title_for(decision),
                runner=lambda task: self._execute_tool(decision, task),
            )

        result = await self._execute_tool(decision, None)
        spoken = result.summary
        if not result.ok:
            rescued = await self._rescue(text, decision, result)
            if rescued is not None:
                return rescued
            return await self._respond(result.summary, decision, display=result.display,
                                       error=result.error)
        if decision.name in _VERIFIED_QUICK_TOOLS:
            handled = await self._verify_quick_tool(text, decision, result)
            if handled is not None:
                return handled
        if decision.name not in _DIRECT_SPEECH_TOOLS and len(result.summary) > 220:
            spoken = speakable(result.summary, 260)
        return await self._respond(result.summary, decision, spoken=spoken,
                                   display=result.display)

    async def _rescue(self, text: str, decision: RouteDecision,
                      result) -> TurnResult | None:
        """Hand a failed quick-path guess to the agent.

        The fast path is a shortcut, not a dead end. "Open the BBC" matches the
        open-an-application pattern, and in V1.1 that was the end of it: no such
        application, no answer. The tool itself knows the difference between
        "that isn't mine to do" and "I tried and it failed" — only the first
        sets :attr:`ToolResult.wrong_tool`, and only the first is reconsidered.
        A refused launch is still reported plainly, because that is the true
        answer and guessing again would be noise.
        """
        if not result.wrong_tool:
            return None
        tool = self.deps.registry.get(decision.name)
        if tool is None or tool.spec.changes_state:
            # Defence in depth. A tool that sets ``wrong_tool`` is saying it did
            # nothing, but the whole safety model rests on honest declarations,
            # and re-attempting something that *did* have an effect is how you
            # send an email twice. A state-changing tool is reported instead.
            return None
        if self._intelligence() is None:
            return None
        log.info("quick match %s failed (%s); reconsidering", decision.name, result.error)
        retry = RouteDecision(RouteKind.CAPABILITY, decision.name, decision.args,
                              confidence=0.4, path=RoutePath.FALLBACK,
                              reason=f"{decision.name} failed: {result.error}")
        return await self._handle_intelligently(text, retry, allow_quick=False)

    async def _verify_quick_tool(self, text: str, decision: RouteDecision,
                                 result) -> TurnResult | None:
        """A deterministic match is a certainty about *which* tool, not about
        the outcome — "opened" is not "verified open". Reuses the same
        :class:`~jarvis.intelligence.verify.Verifier` the agent path uses, so
        a 404 or a wrong destination is caught here exactly as it would be
        there, and handed to the agent to recover rather than spoken as
        success (V1.3 §7).
        """
        from ..intelligence.schema import Objective
        from ..intelligence.verify import Verifier

        turn_id = current_turn_id()
        target = str(decision.args.get("query") or decision.args.get("url") or "")
        objective = Objective(goal=target, targets=[target] if target else [])
        log.info("turn_id=%s stage=verification_start tool=%s", turn_id, decision.name)
        verification = await Verifier(self.deps).verify(decision.name, decision.args, result,
                                                         objective, self.state)
        log.info("turn_id=%s stage=verification_end tool=%s verified=%s skipped=%s",
                 turn_id, decision.name, verification.verified, verification.skipped)
        if verification.verified or verification.skipped:
            return None
        if self._intelligence() is None:
            return None
        log.info("turn_id=%s quick match %s did not verify (%s); reconsidering",
                 turn_id, decision.name, verification.problem)
        retry = RouteDecision(RouteKind.CAPABILITY, decision.name, decision.args,
                              confidence=0.4, path=RoutePath.FALLBACK,
                              reason=f"{decision.name} did not verify: {verification.problem}")
        return await self._handle_intelligently(text, retry, allow_quick=False)

    async def _execute_tool(self, decision: RouteDecision, task: Task | None):
        ctx = self._tool_context(task)
        return await self.deps.registry.call(decision.name, decision.args, ctx)

    # -- capability --------------------------------------------------------
    async def _handle_capability(self, text: str, decision: RouteDecision) -> TurnResult:
        capability = self.capabilities.get(decision.name)
        if capability is None:
            capability = self.capabilities["conversation"]
            decision = RouteDecision(RouteKind.CAPABILITY, "conversation", {"query": text},
                                     confidence=decision.confidence, path=decision.path,
                                     reason=f"{decision.name} unavailable")
        long_running = decision.long_running or capability.long_running

        if long_running:
            return await self._run_in_background(
                text, decision, title=_title_for(decision),
                runner=lambda task: self._run_capability(capability, text, decision, task),
            )

        response = await self._run_capability(capability, text, decision, None)
        return await self._finish_capability(response, decision)

    def _stream_sink(self, task: Task | None):
        """Return ``(emit, flush)`` for a streamed answer.

        Deltas go to the UI immediately and to speech at sentence boundaries, so
        TTS is never handed a fragment. ``flush`` speaks whatever is left over
        and reports whether anything was streamed at all.
        """
        spoken_buffer: list[str] = []
        streamed: list[str] = []

        def emit(delta: str) -> None:
            streamed.append(delta)
            self.deps.bus.publish(EventType.ASSISTANT_DELTA, delta=delta,
                                  task_id=task.id if task else None)
            if self.voice is not None and task is None:
                spoken_buffer.append(delta)
                buffered = "".join(spoken_buffer)
                if re.search(r"[.!?]\s$|[.!?]$", buffered) and len(buffered) > 40:
                    spoken_buffer.clear()
                    self.voice.enqueue(speakable(buffered))

        def flush() -> bool:
            if spoken_buffer and self.voice is not None:
                remainder = "".join(spoken_buffer).strip()
                if remainder:
                    self.voice.enqueue(speakable(remainder))
            return bool(streamed)

        return emit, flush

    async def _run_capability(self, capability: Capability, text: str,
                              decision: RouteDecision, task: Task | None) -> Response:
        ctx = self._tool_context(task)
        emit, flush = self._stream_sink(task)
        request = Request(
            text=text, args=decision.args, decision=decision, ctx=ctx, task=task,
            context=self.context.build(text), emit=emit,
        )
        response = await capability.handle(request)
        if flush():
            response.streamed = True
        return response

    async def _finish_capability(self, response: Response, decision: RouteDecision) -> TurnResult:
        for fact in response.remember:
            await self.deps.memory.remember(fact, source=decision.name)
        self._note_capability_result(response, decision)
        self._await_answer(response)
        self._capture_draft(response.display)
        spoken = response.spoken if response.spoken is not None else speakable(response.text)
        return await self._respond(
            response.text, decision, spoken=spoken, display=response.display,
            error=response.error, already_streamed=response.streamed,
        )

    # -- intelligence ------------------------------------------------------
    async def _handle_intelligently(self, text: str, decision: RouteDecision, *,
                                    allow_quick: bool = True) -> TurnResult:
        """Run the turn through the agent loop.

        The route no longer decides what to do, or whether to keep the user
        waiting: that guess came from the same heuristic/keyword scoring V1.3
        stopped trusting for chat-vs-action, and guessing wrong meant speaking
        an acknowledgement ("I'll look into it") for work that turned out to
        be a one-line answer. The agent's own IntentTriage decides chat vs.
        action first; everything runs to completion, with visibility coming
        from streaming and activity events rather than a premature guess.
        Quick-matched capabilities (email, calendar, research, diagnostics)
        keep their own deterministic backgrounding untouched — see
        :meth:`_handle_capability`.
        """
        agent = self._intelligence()
        if agent is None:  # configuration turned it off mid-flight
            return await self._handle_capability(text, decision)

        outcome = await self._run_agent(agent, text, None, allow_quick=allow_quick)
        if outcome.handoff == "automation":
            return await self._handoff_to_automation(text, decision, outcome)
        if outcome.handoff == "quick" and outcome.quick_decision is not None:
            return await self._run_interpreted_command(text, outcome.quick_decision)
        return await self._finish_capability(_response_from_outcome(outcome), decision)

    async def _run_interpreted_command(self, text: str, decision: RouteDecision) -> TurnResult:
        """Run the deterministic command the interpreter restated the request
        as — the same path a literally-phrased command takes."""
        self.deps.bus.publish(EventType.ROUTE, **decision.as_dict())
        log.info("turn_id=%s stage=dispatch path=interpreted_quick %s (%s)", current_turn_id(),
                 decision.name, decision.reason)
        if decision.kind == RouteKind.TOOL:
            return await self._handle_tool(text, decision)
        return await self._handle_capability(text, decision)

    async def _handoff_to_automation(self, text: str, decision: RouteDecision, outcome) -> TurnResult:
        """A multi-step errand needs a real Task — cancel_event, progress, an
        errand-sized budget — so it goes through the exact backgrounding
        machinery a quick-matched capability already gets
        (:meth:`_handle_capability` → :meth:`_run_in_background`). It runs on
        the same operator loop a foreground action does; see
        ``capabilities/automation.py``.
        """
        automation_decision = RouteDecision(
            kind=RouteKind.CAPABILITY, name="automation",
            args={"query": text, "objective": outcome.objective,
                  "situation": self.state.describe_for_model(include_turns=2)},
            confidence=decision.confidence, path=decision.path,
            reason="multi-step errand", long_running=True,
        )
        return await self._handle_capability(text, automation_decision)

    async def _run_agent(self, agent, text: str, task: Task | None, *, allow_quick: bool = True):
        # A foreground turn gets its own cancel token, so "stop" reaches an
        # action already under way (see _cancel_everything).
        token = asyncio.Event() if task is None else None
        ctx = (self._tool_context(task) if token is None
               else self.deps.tool_context(cancel_event=token))
        stream, flush = self._stream_sink(task)

        def activity(message: str) -> None:
            self.deps.bus.publish(EventType.ACTIVITY, message=message,
                                  task_id=task.id if task else None)

        if token is not None:
            self._foreground.add(token)
        try:
            outcome = await agent.run(text, ctx, task=task, emit=activity, stream=stream,
                                      context=self.context.build(text), allow_quick=allow_quick)
        finally:
            if token is not None:
                self._foreground.discard(token)
        flush()
        return outcome

    def _intelligence(self):
        """The agent, rebuilt only when its configuration actually changes."""
        conf = self.deps.config.intelligence
        if not conf.enabled:
            return None
        signature = (conf.max_steps, conf.reasoning_slot, conf.trace)
        if self._agent is None or self._agent_signature != signature:
            from ..intelligence.agent import IntelligenceAgent

            self._agent = IntelligenceAgent(
                self.deps, self.deps.models, self.state,
                max_steps=conf.max_steps, reasoning_slot=conf.reasoning_slot,
                publish_trace=conf.trace,
            )
            self._agent_signature = signature
        return self._agent

    def _note_capability_result(self, response: Response, decision: RouteDecision) -> None:
        """Absorb a capability's structured result into conversational context.

        Capabilities compose their own work and some of it — research reading
        pages, for instance — never passes through the tool registry, so the
        observer wouldn't see it. The payload has the same shape either way, and
        the point of requirement 19 is that a result is context regardless of
        which machinery produced it.
        """
        if response.data is None or decision.kind == RouteKind.CONTROL:
            return
        self.state.note_observation(decision.name, decision.args, not response.error,
                                    response.spoken or response.text[:200], response.data,
                                    decision.name)

    # -- background --------------------------------------------------------
    async def _run_in_background(self, text: str, decision: RouteDecision, title: str,
                                 runner) -> TurnResult:
        kind = _kind_for(decision)
        acknowledgement = self.personality.acknowledgement(long_running=True, kind=kind)
        self.deps.bus.emit_state(
            AssistantState.RESEARCHING if decision.name == "research" else AssistantState.EXECUTING
        )
        # Create the task first, so the activity panel shows the work starting at
        # the same moment the acknowledgement is spoken.
        task = self.deps.tasks.create(kind, title)
        await self._respond(acknowledgement, decision, speak=True, store=False,
                            keep_state=True, task_id=task.id)

        async def run(task_ref: Task):
            started = time.perf_counter()
            outcome = await runner(task_ref)
            duration = (time.perf_counter() - started) * 1000.0
            self.deps.telemetry.record(f"task.{task_ref.kind}", duration, ok=True)
            await self._deliver_background(outcome, decision, task_ref)
            return outcome

        task._runner = asyncio.create_task(self.deps.tasks._run(task, run))
        return TurnResult(acknowledgement, acknowledgement, decision, 0.0, task_id=task.id)

    async def _deliver_background(self, outcome, decision: RouteDecision, task: Task) -> None:
        """Report a finished background task: speak a summary, show the detail."""
        from ..tools.base import ToolResult

        if task.cancel_event.is_set():
            return
        if isinstance(outcome, ToolResult):
            text = outcome.summary
            display = outcome.display
            spoken = speakable(outcome.summary, 300)
            error = outcome.error
        elif isinstance(outcome, Response):
            text = outcome.text
            display = outcome.display
            spoken = outcome.spoken if outcome.spoken is not None else speakable(outcome.text, 340)
            error = outcome.error
        else:
            text = str(outcome or "")
            display = None
            spoken = speakable(text, 300)
            error = None

        if isinstance(outcome, Response):
            self._note_capability_result(outcome, decision)
            self._await_answer(outcome)
        self._capture_draft(display)
        await self._respond(text, decision, spoken=spoken, display=display, error=error,
                            task_id=task.id)

    # ------------------------------------------------------------------
    # replying
    # ------------------------------------------------------------------
    async def _respond(self, text: str, decision: RouteDecision, *, spoken: str | None = None,
                       display: dict | None = None, error: str | None = None,
                       speak: bool = True, store: bool = True, task_id: str | None = None,
                       already_streamed: bool = False, keep_state: bool = False) -> TurnResult:
        bus = self.deps.bus
        spoken_text = spoken if spoken is not None else speakable(text)

        bus.publish(
            EventType.ASSISTANT_MESSAGE,
            text=text,
            spoken=spoken_text,
            route=f"{decision.kind}:{decision.name}",
            path=decision.path,
            error=error,
            task_id=task_id,
            streamed=already_streamed,
        )
        if display:
            bus.publish(EventType.RESULT_PANEL, task_id=task_id, **display)

        if store and text:
            await self.deps.memory.add_message("assistant", text,
                                               {"route": f"{decision.kind}:{decision.name}"})
            self.state.note_assistant(text)

        log.info("turn_id=%s stage=response_emit streamed=%s chars=%d",
                 current_turn_id(), already_streamed, len(text))

        if speak and spoken_text and self.voice is not None and not already_streamed:
            # Speaking is queued, never awaited: the turn is finished when the
            # answer exists, not when the sentence has finished playing. Barge-in
            # and "stop" drain the same queue.
            #
            # `not already_streamed` is load-bearing, not a style choice: a
            # streamed answer (_stream_sink's emit()/flush(), used by every
            # agent-routed reply) has already been queued for speech
            # sentence-by-sentence as it was generated. Without this guard,
            # every one of those replies was queued a second time here, in
            # full, right after the first — a real macOS runtime report
            # confirmed responses were audibly spoken twice. Enqueueing here
            # is still correct, and still needed, for anything that was
            # never streamed: quick-path tool replies and background-task
            # delivery, neither of which sets already_streamed.
            self.voice.enqueue(spoken_text)
        elif speak and spoken_text and already_streamed:
            log.info("turn_id=%s stage=tts_skip reason=already_streamed", current_turn_id())
        if not keep_state:
            bus.emit_state(AssistantState.IDLE)
        return TurnResult(text, spoken_text, decision, 0.0, task_id=task_id, error=error)

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    def _tool_context(self, task: Task | None = None):
        return self.deps.tool_context(task=task)

    def _recent_context(self) -> str:
        messages = self.deps.memory.recent_messages(4)
        if not messages:
            return ""
        return "\n".join(f"{m['role']}: {m['text'][:160]}" for m in messages[-4:])

    def _await_answer(self, response: Response) -> None:
        """Work that stopped on a question for the user resumes when they
        answer it: the next turn is read as the answer (see
        ``Understanding._apply_clarification``)."""
        if not response.clarification:
            return
        data = response.data if isinstance(response.data, dict) else {}
        goal = str(data.get("goal") or "")
        self.state.ask(PendingClarification(question=response.clarification,
                                            objective_goal=goal, purpose=goal))

    def remember_draft(self, draft: dict[str, Any]) -> None:
        self._last_draft = draft

    def _capture_draft(self, display: dict | None) -> None:
        """Hold on to an unsent draft so "send it" has something to send.

        The draft itself is harmless; sending it still goes through the HIGH-risk
        confirmation gate, so remembering it never bypasses anything.
        """
        if not display or display.get("kind") != "draft" or display.get("title") == "Sent":
            return
        if display.get("to") and display.get("body"):
            self._last_draft = {
                "to": display["to"],
                "subject": display.get("subject", ""),
                "body": display["body"],
                "cc": display.get("cc", []),
            }


def _response_from_outcome(outcome) -> Response:
    """An AgentOutcome and a capability Response say the same things; making
    the agent look like a capability here means delivery, speech, memory and
    background reporting all stay in one place."""
    return Response(text=outcome.text, spoken=outcome.spoken, display=outcome.display,
                    error=outcome.error, streamed=outcome.streamed)


def _title_for(decision: RouteDecision) -> str:
    query = decision.args.get("query") or decision.args.get("question") or ""
    names = {
        "research": f"Researching {query[:60]}" if query else "Research",
        "diagnostics": "Running system diagnostics",
        "email": "Checking mail",
        "calendar": "Reading the calendar",
        "screen": "Analysing the screen",
        "analyse_screen": "Analysing the screen",
        "read_calendar": "Reading the calendar",
        "check_email": "Checking mail",
        "run_diagnostics": "Running system diagnostics",
        "automation": f"{query[:60]}" if query else "Working on that",
    }
    return names.get(decision.name, decision.name.replace("_", " ").capitalize())


def _kind_for(decision: RouteDecision) -> str:
    mapping = {
        "research": "research", "diagnostics": "system", "email": "mail",
        "calendar": "calendar", "screen": "screen", "analyse_screen": "screen",
        "read_calendar": "calendar", "check_email": "mail", "run_diagnostics": "system",
        "automation": "automation",
    }
    return mapping.get(decision.name, decision.kind)


def _is_night() -> bool:
    import datetime as dt

    hour = dt.datetime.now().hour
    return hour >= 21 or hour < 5
