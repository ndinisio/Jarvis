"""Memory: recall, remember, forget.

Deliberately literal. "Remember that X" stores X; "forget that" removes the
most recent thing; "what do you remember about me" reads back what is on file.
No model is involved in recall — it is a database query, and the user is
entitled to an exact answer about what is stored.
"""

from __future__ import annotations

import re

from ..core.events import EventType
from ..core.logging import get_logger
from .base import Capability, Request, Response

log = get_logger("jarvis.capabilities.memory")

_PREFERENCE_PATTERNS = (
    (re.compile(r"\bcall me ([\w .'-]{2,30})", re.I), "preferred_name"),
    (re.compile(r"\bmy name is ([\w .'-]{2,30})", re.I), "preferred_name"),
    (re.compile(r"\bi prefer (?:the )?(\w+) voice", re.I), "preferred_voice"),
)


class MemoryCapability(Capability):
    name = "memory"
    description = "What JARVIS remembers about the user."

    async def handle(self, request: Request) -> Response:
        intent = request.args.get("intent", "recall")
        if intent == "remember":
            return await self._remember(request)
        if intent == "forget":
            return await self._forget(request)
        return self._recall()

    def _recall(self) -> Response:
        store = self.deps.memory
        snapshot = store.snapshot()
        return Response(
            text=store.describe(),
            display={
                "kind": "memory",
                "title": "Memory",
                "preferences": snapshot["preferences"],
                "facts": snapshot["facts"],
                "path": snapshot["path"],
            },
        )

    async def _remember(self, request: Request) -> Response:
        text = (request.args.get("text") or request.text).strip()
        if not text:
            return Response(text="What would you like me to remember?")
        for pattern, key in _PREFERENCE_PATTERNS:
            match = pattern.search(text)
            if match:
                await self.deps.memory.set_preference(key, match.group(1).strip())
                self.deps.bus.publish(EventType.MEMORY, action="preference", key=key,
                                      value=match.group(1).strip())
                return Response(text=f"Noted — {key.replace('_', ' ')} is "
                                     f"{match.group(1).strip()}.")
        fact_id = await self.deps.memory.remember(text, importance=0.7, source="explicit")
        self.deps.bus.publish(EventType.MEMORY, action="remember", id=fact_id, text=text)
        return Response(text="Noted, sir. I'll keep that in mind.")

    async def _forget(self, request: Request) -> Response:
        text = (request.args.get("text") or "").strip()
        store = self.deps.memory
        if not text or text.lower() in {"that", "it", "the last thing", "this"}:
            facts = store.facts(limit=1)
            if not facts:
                return Response(text="There's nothing on file to forget.")
            await store.forget_fact(facts[0].id)
            self.deps.bus.publish(EventType.MEMORY, action="forget", text=facts[0].text)
            return Response(text=f"Forgotten: {facts[0].text}")
        removed = await store.forget(text)
        if not removed:
            preferences = store.preferences()
            for key in list(preferences):
                if key.replace("_", " ") in text.lower():
                    await store.forget_preference(key)
                    return Response(text=f"Forgotten your {key.replace('_', ' ')}.")
            return Response(text="I don't have anything matching that.")
        self.deps.bus.publish(EventType.MEMORY, action="forget", count=len(removed))
        if len(removed) == 1:
            return Response(text=f"Forgotten: {removed[0]}")
        return Response(text=f"Forgotten {len(removed)} entries.")
