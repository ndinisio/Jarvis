"""The conversation the operator sees, kept inside the model's context window.

Layout of every request, front to back:

1. the system prompt — fixed for the whole run;
2. the task brief (goal, checklist, what's known about the user) — fixed;
3. the history: each model turn's tool calls and their results. The latest
   ``keep_full`` turns carry their results in full — including the page
   listing the next decision acts on. Older ones are cut to one line each:
   enough to remember what happened, without carrying stale page listings
   (with stale element handles) forward;
4. a status note — the checklist's current state, steps used, and any hint —
   rebuilt for every request and never stored.

Because (1) and (2) never change and a turn's results only ever shrink once
(from full to one line), everything before the last few messages is
identical from one request to the next: a local model's prompt cache reuses
it. When even the shortened history won't fit the budget, the oldest turns
are dropped whole — calls and results together, so no tool result is ever
left without the call it answers — and summarised as one line each in the
status note.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ...models.base import ChatMessage, ToolCall

#: A rough, deliberately pessimistic characters-per-token figure for English
#: prose, JSON and page listings.
CHARS_PER_TOKEN = 3.2


@dataclass
class Turn:
    """One model turn: the calls it made and what came back."""

    calls: list[ToolCall]
    full: list[str]
    short: list[str]
    text: str = ""
    digest: str = ""


@dataclass
class Conversation:
    system: str
    brief: str
    #: Characters available for everything we send (context minus the reply).
    budget_chars: int
    keep_full: int = 2
    turns: list[Turn] = field(default_factory=list)
    dropped: list[str] = field(default_factory=list)

    def add(self, turn: Turn) -> None:
        self.turns.append(turn)

    def messages(self, status: str = "", *, fixed_overhead: int = 0) -> list[ChatMessage]:
        """The request to send now. *fixed_overhead* is what the tool
        schemas will cost, in characters, on top of the messages."""
        keep_full = self.keep_full
        while True:
            messages = self._render(status, keep_full)
            size = fixed_overhead + sum(len(m.content) + 40 * len(m.tool_calls) for m in messages)
            if size <= self.budget_chars:
                return messages
            if keep_full > 1:
                keep_full -= 1          # first, show only the very latest result in full
                continue
            if len(self.turns) > 1:
                oldest = self.turns.pop(0)
                self.dropped.append(oldest.digest or "; ".join(oldest.short))
                continue
            # A single turn that still doesn't fit: cut its results down.
            last = self.turns[-1] if self.turns else None
            if last is None or all(len(text) <= 400 for text in last.full):
                return messages
            last.full = [text[: max(400, len(text) // 2)] for text in last.full]

    def _render(self, status: str, keep_full: int) -> list[ChatMessage]:
        messages = [ChatMessage("system", self.system), ChatMessage("user", self.brief)]
        recent_from = len(self.turns) - keep_full
        for index, turn in enumerate(self.turns):
            messages.append(ChatMessage("assistant", turn.text, tool_calls=list(turn.calls)))
            texts = turn.full if index >= recent_from else turn.short
            for call, text in zip(turn.calls, texts):
                messages.append(ChatMessage("tool", text, tool_call_id=call.id, name=call.name))
        note = self._status(status)
        if note:
            messages.append(ChatMessage("user", note))
        return messages

    def _status(self, status: str) -> str:
        parts = []
        if self.dropped:
            earlier = self.dropped[-8:]
            skipped = len(self.dropped) - len(earlier)
            parts.append("Earlier steps (summarised):" + (f" [{skipped} more before these]" if skipped else "")
                         + "\n" + "\n".join(f"- {line}" for line in earlier))
        if status.strip():
            parts.append(status.strip())
        return "\n\n".join(parts)


def budget_for(num_ctx: int, max_tokens: int) -> int:
    """Characters available for a request on a model with *num_ctx* tokens
    of context that must leave room for a *max_tokens* reply."""
    tokens = max(2048, (num_ctx or 8192) - max(256, max_tokens or 900))
    return int(tokens * CHARS_PER_TOKEN)
