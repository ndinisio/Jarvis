"""Context assembly.

Local models are small and their context windows are precious, so JARVIS never
ships the whole conversation with every request. Context is built in layers and
only the layers that matter are included:

1. identity (the system prompt — added by the personality)
2. relevant user preferences
3. recent conversation (a few turns, truncated)
4. current task context
5. relevant tool results

Layer 3 is capped by ``memory.context_turns``; layer 2 and 5 are retrieved by
relevance, not dumped wholesale.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any

MAX_TOOL_RESULT_CHARS = 1200


@dataclass
class ContextBuilder:
    memory: Any
    config: Any
    #: Results from tools run during this exchange.
    tool_results: list[tuple[str, str]] = field(default_factory=list)

    def note_tool_result(self, tool: str, summary: str) -> None:
        if summary:
            self.tool_results.append((tool, summary[:MAX_TOOL_RESULT_CHARS]))
            del self.tool_results[:-4]

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

        if self.tool_results:
            blocks.append(
                "Results just gathered:\n"
                + "\n".join(f"- {tool}: {summary}" for tool, summary in self.tool_results)
            )

        return "\n\n".join(blocks)

    def reset(self) -> None:
        self.tool_results.clear()
