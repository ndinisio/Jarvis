"""Context assembly.

Local models are small and their context windows are precious, so JARVIS never
ships the whole conversation with every request. Context is built in layers and
only the layers that matter are included:

1. identity (the system prompt — added by the personality)
2. relevant user preferences
3. recent conversation (a few turns, truncated)
4. current task context

Layer 3 is capped by ``memory.context_turns``; layer 2 is retrieved by
relevance, not dumped wholesale.

This builder deliberately carries nothing about *what a tool just returned* —
that used to be a fifth layer here ("Results just gathered"), populated once
per tool call and never cleared, which meant a greeting minutes after a
finished research task still arrived with that task's results attached (V1.3
fix, see the intelligence-context regression tests). Recent actions already
have a correct home: :class:`~jarvis.intelligence.state.ConversationState`,
fed by every tool call through the registry observer, bounded, and read by
exactly the machinery that legitimately needs it — reference resolution, the
planner, the decision loop — not by conversation at large. Duplicating that
here, with no equivalent boundary, was the bug; the fix is not carrying it at
all rather than adding a lifecycle to a second copy.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any


@dataclass
class ContextBuilder:
    memory: Any
    config: Any

    def build(self, query: str, *, task_context: str = "",
              include_time: bool = True) -> str:
        blocks: list[str] = []

        if include_time:
            now = dt.datetime.now()
            blocks.append(f"Current date and time: {now:%A %d %B %Y, %H:%M}.")

        if self.config.memory.enabled:
            preferences = self.memory.preferences()
            if preferences:
                lines = [f"- {key.replace('_', ' ')}: {value}"
                         for key, value in list(preferences.items())[:6]]
                blocks.append("Known preferences:\n" + "\n".join(lines))

            facts = self.memory.relevant_facts(query, self.config.memory.max_facts_in_context)
            if facts:
                blocks.append(
                    "Relevant memory:\n" + "\n".join(f"- {fact.text}" for fact in facts)
                )

        if task_context:
            blocks.append(f"Current task:\n{task_context}")

        return "\n\n".join(blocks)
