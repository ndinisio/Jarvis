"""The orchestrator.

One turn of conversation, end to end:

    transcript → route → execute → respond → speak → remember

The rules it enforces are the ones the whole product rests on:

* **Latency is proportional to complexity.** A greeting is answered from the
  phrasebook in microseconds; a system fact comes from macOS in milliseconds; a
  research request is acknowledged immediately and continues in the background.
* **Long work never blocks the conversation.** Anything long-running becomes a
  task, so the user can keep talking while it runs.
* **Speech is a summary, not a recital.** The written answer goes to the UI; the
  spoken one is trimmed to something worth hearing.
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass
from typing import Any

from ..capabilities.base import Capability, Request, Response
from ..router.router import Router
from ..router.schema import RouteDecision, RouteKind
from ..tasks.manager import Task
from .context import ContextBuilder
from .errors import Cancelled, ConfirmationDeclined, JarvisError
from .events import AssistantState, EventType
from .logging import get_logger
from .personality import Personality, speakable

log = get_logger("jarvis.orchestrator")

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
        self._last_draft: dict[str, Any] | None = None
        self._turn_lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # public entry point
    # ------------------------------------------------------------------
    async def handle(self, text: str, *, source: str = "text") -> TurnResult:
        text = (text or "").strip()
        if not text:
            return TurnResult("", "", RouteDecision(RouteKind.CONTROL, "noop"), 0.0)

        turn = self.deps.telemetry.mark("turn.total", source=source)
        bus = self.deps.bus
        bus.publish(EventType.TRANSCRIPT, text=text, final=True, source=source)

        # Anything the user says interrupts speech — that is the whole point of
        # barge-in, and it must happen before routing so it feels instant.
        if self.voice is not None and self.deps.config.voice.barge_in:
            await self.voice.stop_speaking()

        await self.deps.memory.add_message("user", text, {"source": source})
        bus.emit_state(AssistantState.THINKING)

        try:
            decision = await self.router.route(
                text, context=self._recent_context(), allow_model=True
            )
        except Exception as exc:
            log.exception("routing failed")
            decision = RouteDecision(RouteKind.CAPABILITY, "conversation", {"query": text},
                                     confidence=0.3, reason=f"router error: {exc}")

        bus.publish(EventType.ROUTE, **decision.as_dict())
        log.info("route %s:%s via %s (%.1f ms) — %s", decision.kind, decision.name,
                 decision.path, decision.latency_ms, text[:60])

        try:
            result = await self._dispatch(text, decision, source)
        except ConfirmationDeclined as exc:
            result = TurnResult(exc.user_message, exc.user_message, decision, 0.0)
        except Cancelled:
            result = TurnResult(self.personality.cancelled(), self.personality.cancelled(),
                                decision, 0.0)
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
            return await self._handle_control(text, decision)
        if decision.kind == RouteKind.TOOL:
            return await self._handle_tool(text, decision)
        return await self._handle_capability(text, decision)

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
        return await self._handle_capability(text, decision)

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
        self.deps.permissions.cancel_all("user cancelled")
        if cancelled_task is not None:
            message = f"Stopped — {cancelled_task.title.lower()}."
        elif stopped_speech:
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
            return await self._respond(result.summary, decision, display=result.display,
                                       error=result.error)
        if decision.name not in _DIRECT_SPEECH_TOOLS and len(result.summary) > 220:
            spoken = speakable(result.summary, 260)
        self.context.note_tool_result(decision.name, result.summary)
        return await self._respond(result.summary, decision, spoken=spoken,
                                   display=result.display)

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

    async def _run_capability(self, capability: Capability, text: str,
                              decision: RouteDecision, task: Task | None) -> Response:
        ctx = self._tool_context(task)
        streamed: list[str] = []
        spoken_buffer: list[str] = []

        def emit(delta: str) -> None:
            streamed.append(delta)
            self.deps.bus.publish(EventType.ASSISTANT_DELTA, delta=delta,
                                  task_id=task.id if task else None)
            if self.voice is not None and task is None:
                spoken_buffer.append(delta)
                buffered = "".join(spoken_buffer)
                # Speak at sentence boundaries so TTS never gets fragments.
                if re.search(r"[.!?]\s$|[.!?]$", buffered) and len(buffered) > 40:
                    spoken_buffer.clear()
                    self.voice.enqueue(speakable(buffered))

        request = Request(
            text=text, args=decision.args, decision=decision, ctx=ctx, task=task,
            context=self.context.build(text), emit=emit,
        )
        response = await capability.handle(request)
        if spoken_buffer and self.voice is not None:
            remainder = "".join(spoken_buffer).strip()
            if remainder:
                self.voice.enqueue(speakable(remainder))
            response.streamed = True
        return response

    async def _finish_capability(self, response: Response, decision: RouteDecision) -> TurnResult:
        for fact in response.remember:
            await self.deps.memory.remember(fact, source=decision.name)
        self._capture_draft(response.display)
        spoken = response.spoken if response.spoken is not None else speakable(response.text)
        return await self._respond(
            response.text, decision, spoken=spoken, display=response.display,
            error=response.error, already_streamed=response.streamed,
        )

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

        self.context.note_tool_result(decision.name, text[:400])
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

        if speak and spoken_text and self.voice is not None:
            # Speaking is queued, never awaited: the turn is finished when the
            # answer exists, not when the sentence has finished playing. Barge-in
            # and "stop" drain the same queue.
            self.voice.enqueue(spoken_text)
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
    }
    return names.get(decision.name, decision.name.replace("_", " ").capitalize())


def _kind_for(decision: RouteDecision) -> str:
    mapping = {
        "research": "research", "diagnostics": "system", "email": "mail",
        "calendar": "calendar", "screen": "screen", "analyse_screen": "screen",
        "read_calendar": "calendar", "check_email": "mail", "run_diagnostics": "system",
    }
    return mapping.get(decision.name, decision.kind)


def _is_night() -> bool:
    import datetime as dt

    hour = dt.datetime.now().hour
    return hour >= 21 or hour < 5
