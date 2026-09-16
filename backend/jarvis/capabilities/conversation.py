"""Ordinary conversation.

Streams from the general model, with a context window assembled from identity,
preferences, relevant memories and the last few turns — never the whole history.
Short exchanges are answered by the fast model, because a two-word reply does
not need an 8B model.
"""

from __future__ import annotations

import re

from ..core.errors import ModelUnavailable
from ..core.logging import get_logger
from ..models.base import ChatMessage
from ..models.registry import Slot
from .base import Capability, Request, Response

log = get_logger("jarvis.capabilities.conversation")

#: Requests this short and simple are handled by the fast model.
_SHORT_CHAT = re.compile(
    r"^(?:.{0,60})$"
)
_COMPLEX_HINTS = (
    "explain", "why", "how does", "compare", "write", "draft", "plan", "analyse", "analyze",
    "summarise", "summarize", "code", "debug", "translate", "essay", "difference",
)


class ConversationCapability(Capability):
    name = "conversation"
    description = "General conversation and knowledge."

    async def handle(self, request: Request) -> Response:
        slot = self._choose_slot(request.text)
        messages = [
            ChatMessage("system", self._system_prompt(request)),
        ]
        messages.extend(self._history(request))
        messages.append(ChatMessage("user", request.text))

        parts: list[str] = []
        try:
            async for delta in self.models.stream(slot, messages):
                if request.ctx and request.ctx.cancelled():
                    break
                parts.append(delta)
                request.stream(delta)
        except ModelUnavailable as exc:
            return Response(
                text="The local AI service isn't available. Start Ollama and I'll pick up "
                     "where we left off.",
                error=exc.detail or exc.user_message,
            )
        except Exception as exc:
            log.exception("conversation failed")
            return Response(text="That request didn't complete.", error=str(exc))

        text = "".join(parts).strip()
        if not text:
            return Response(text="I don't have an answer to that, sir.")
        return Response(text=text, streamed=True)

    def _choose_slot(self, text: str) -> str:
        lowered = text.lower()
        if any(hint in lowered for hint in _COMPLEX_HINTS) or len(text) > 90:
            return Slot.GENERAL
        if _SHORT_CHAT.match(text.strip()):
            return Slot.FAST
        return Slot.GENERAL

    def _system_prompt(self, request: Request) -> str:
        from ..core.personality import Personality

        personality = Personality(self.deps.config)
        return personality.system_prompt(request.context)

    def _history(self, request: Request) -> list[ChatMessage]:
        turns = self.deps.config.memory.context_turns
        if not self.deps.config.memory.enabled or turns <= 0:
            return []
        messages = []
        for entry in self.deps.memory.recent_messages(turns * 2):
            if entry["role"] in {"user", "assistant"} and entry["text"].strip():
                messages.append(ChatMessage(entry["role"], entry["text"][:1500]))
        return messages[-turns * 2 :]
