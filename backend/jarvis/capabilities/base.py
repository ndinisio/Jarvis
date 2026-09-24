"""Capabilities.

A capability is a coherent area of competence — email, research, the screen,
the machine itself — that owns a few tools and knows how to use them. There is
one orchestration system (the orchestrator) and a handful of capabilities; there
is deliberately *not* a swarm of autonomous agents, because independent agents
only earn their keep when a task genuinely needs isolated context.

Most capabilities are a thin, model-assisted mapping from a sentence to a tool
call, so they share :class:`ToolPlanCapability`. The ones that genuinely differ
— research, diagnostics, email triage, conversation — implement their own flow.
"""

from __future__ import annotations

import abc
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ..core.logging import get_logger
from ..models.base import ChatMessage
from ..models.registry import Slot
from ..router.schema import RouteDecision
from ..tasks.manager import Task
from ..tools.base import ToolContext, ToolResult

log = get_logger("jarvis.capabilities")


@dataclass
class Request:
    text: str
    args: dict[str, Any] = field(default_factory=dict)
    decision: RouteDecision | None = None
    ctx: ToolContext = None  # type: ignore[assignment]
    task: Task | None = None
    #: Prompt context assembled by the ContextBuilder.
    context: str = ""
    #: Emit a streaming delta to the UI (and, buffered, to speech).
    emit: Callable[[str], None] | None = None

    def stream(self, delta: str) -> None:
        if self.emit:
            self.emit(delta)


@dataclass
class Response:
    text: str = ""
    #: Overrides what gets spoken when the written answer is long.
    spoken: str | None = None
    display: dict[str, Any] | None = None
    #: True when the text was already streamed token by token.
    streamed: bool = False
    #: Durable facts worth writing to memory.
    remember: list[str] = field(default_factory=list)
    error: str | None = None
    data: Any = None
    #: A question for the user that the work is waiting on. The next thing
    #: they say is taken as the answer, and the work resumes with it.
    clarification: str | None = None

    @property
    def speech(self) -> str:
        return self.spoken if self.spoken is not None else self.text


class Capability(abc.ABC):
    name: str = "capability"
    description: str = ""
    #: Should the orchestrator acknowledge immediately and run this in the
    #: background?
    long_running: bool = False

    def __init__(self, deps):
        self.deps = deps

    @property
    def registry(self):
        return self.deps.registry

    @property
    def models(self):
        return self.deps.models

    @abc.abstractmethod
    async def handle(self, request: Request) -> Response: ...

    # -- helpers -----------------------------------------------------------
    async def call_tool(self, name: str, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        return await self.registry.call(name, args, ctx)

    async def phrase(self, instruction: str, facts: str, request: Request,
                     slot: str = Slot.FAST, max_tokens: int = 160) -> str:
        """Ask a model to phrase a result that is already known.

        Used sparingly: most tools already return a spoken-quality summary, and
        a template beats a model both in latency and in consistency.
        """
        from ..core.personality import Personality

        personality = Personality(self.deps.config)
        messages = [
            ChatMessage("system", personality.system_prompt()),
            ChatMessage("user", f"{instruction}\n\nFacts:\n{facts}\n\n"
                                "Reply with one or two sentences, no preamble."),
        ]
        try:
            completion = await self.models.complete(slot, messages, max_tokens=max_tokens,
                                                    temperature=0.3)
            return completion.text.strip()
        except Exception as exc:
            log.debug("phrase() fell back to the raw facts: %s", exc)
            return facts


class ToolPlanCapability(Capability):
    """Chooses one tool from a small set and runs it.

    The model sees only this capability's tools — a handful of lines — rather
    than the whole registry, which is what makes a 1–3B local model reliable
    enough for the job.
    """

    #: Tools this capability may use.
    tools: tuple[str, ...] = ()
    #: Used when the model can't decide.
    default_tool: str = ""
    planner_slot: str = Slot.FAST

    async def handle(self, request: Request) -> Response:
        tool_name = request.args.get("tool") or ""
        args: dict[str, Any] = {}

        if tool_name and tool_name in self.tools:
            args = {k: v for k, v in request.args.items() if k != "tool"}
        else:
            plan = await self.plan(request)
            tool_name = plan.get("tool", "") or self.default_tool
            args = plan.get("args", {}) or {}

        if tool_name not in self.tools:
            tool_name = self.default_tool
        if not tool_name:
            return Response(text="I'm not sure what you'd like me to do there.")

        result = await self.call_tool(tool_name, args, request.ctx)
        return self.respond(result, request)

    def respond(self, result: ToolResult, request: Request) -> Response:
        if not result.ok:
            return Response(text=result.summary or "That didn't work.", error=result.error,
                            display=result.display)
        return Response(text=result.summary, display=result.display, data=result.data)

    async def plan(self, request: Request) -> dict[str, Any]:
        """Ask the fast model which tool to use and with what arguments."""
        listing = self.registry.describe_for_model(self.tools)
        prompt = (
            f"Choose the single best tool for the user's request.\n\nTools:\n{listing}\n\n"
            f'Request: "{request.text}"\n\n'
            'Reply with JSON only: {"tool": "<name>", "args": {...}}'
        )
        try:
            data = await self.models.complete_json(
                self.planner_slot,
                [
                    ChatMessage("system", "You select tools. You reply with JSON and nothing else."),
                    ChatMessage("user", prompt),
                ],
                max_tokens=160,
                timeout_s=12.0,
            )
        except Exception as exc:
            log.debug("%s planner unavailable: %s", self.name, exc)
            return {}
        if not isinstance(data, dict):
            return {}
        args = data.get("args")
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except json.JSONDecodeError:
                args = {}
        return {"tool": str(data.get("tool", "")).strip(), "args": args if isinstance(args, dict) else {}}
