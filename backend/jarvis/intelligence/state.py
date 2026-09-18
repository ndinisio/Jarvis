"""Short-term conversational state.

Not a memory system — a *bounded working set* describing what is currently
going on, so a follow-up like "anything from my brother?" or "go to the BBC"
has something concrete to refer to.

Everything here is derived automatically from tool results by generic
extractors keyed on tool category and result shape. No tool needs to know this
module exists, and no extractor is written for a particular phrase or demo.
"""

from __future__ import annotations

import datetime as dt
import re
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

#: Screen understanding goes stale quickly — the user moves windows.
SCREEN_TTL_S = 180.0
#: How much of each context survives into a prompt.
MAX_ENTITIES = 40
MAX_OBSERVATIONS = 12
MAX_TURNS = 12


@dataclass(slots=True)
class Entity:
    """Something mentioned or produced that a later turn might refer to."""

    kind: str          # person | email | url | app | file | result | process | event
    value: str         # the canonical handle (address, url, path, name)
    label: str = ""    # human-facing description
    source: str = ""   # tool that produced it
    turn: int = 0
    extra: dict[str, Any] = field(default_factory=dict)

    def describe(self) -> str:
        return f"{self.kind}: {self.label or self.value}"


@dataclass(slots=True)
class Observation:
    """A tool result, compressed to what a later reasoning step needs."""

    tool: str
    arguments: dict[str, Any]
    ok: bool
    summary: str
    turn: int
    ts: float = field(default_factory=time.time)
    data: Any = None

    def describe(self) -> str:
        status = "ok" if self.ok else "failed"
        return f"{self.tool} [{status}]: {self.summary[:160]}"


@dataclass
class BrowserContext:
    app: str = ""
    url: str = ""
    title: str = ""
    intended: str = ""        # what the user asked for, to verify against
    updated: float = 0.0

    def describe(self) -> str:
        if not (self.url or self.app):
            return ""
        where = self.title or self.url
        return f"browser: {self.app or 'browser'}" + (f" on {where}" if where else "")


@dataclass
class EmailContext:
    messages: list[dict[str, Any]] = field(default_factory=list)
    focused: dict[str, Any] | None = None
    unread: int = 0
    updated: float = 0.0

    def describe(self) -> str:
        if not self.messages and not self.focused:
            return ""
        parts = [f"email: {len(self.messages)} message(s) in view"]
        if self.focused:
            parts.append(f"focused on “{self.focused.get('subject', '')[:60]}” "
                         f"from {self.focused.get('sender', '')[:40]}")
        return ", ".join(parts)

    def senders(self) -> list[str]:
        return [m.get("sender", "") for m in self.messages if m.get("sender")]


@dataclass
class ResearchContext:
    query: str = ""
    sources: list[dict[str, Any]] = field(default_factory=list)
    report: str = ""
    refinements: list[str] = field(default_factory=list)
    updated: float = 0.0

    def describe(self) -> str:
        if not self.query:
            return ""
        text = f"research: “{self.query}” with {len(self.sources)} source(s)"
        if self.refinements:
            text += f", refined by {'; '.join(self.refinements[-2:])}"
        return text


@dataclass
class ScreenContext:
    description: str = ""
    elements: list[str] = field(default_factory=list)
    path: str = ""
    app: str = ""
    updated: float = 0.0

    @property
    def fresh(self) -> bool:
        return bool(self.description) and (time.time() - self.updated) < SCREEN_TTL_S

    def describe(self) -> str:
        if not self.fresh:
            return ""
        age = int(time.time() - self.updated)
        text = f"screen (inspected {age}s ago): {self.description[:220]}"
        if self.elements:
            text += f"\nvisible elements: {', '.join(self.elements[:8])}"
        return text


@dataclass
class PendingClarification:
    question: str
    purpose: str = ""          # what the answer unblocks
    slot: str = ""             # argument name the answer fills
    objective_goal: str = ""   # the objective that was waiting
    tool: str = ""
    arguments: dict[str, Any] = field(default_factory=dict)
    candidates: list[str] = field(default_factory=list)
    asked_turn: int = 0


@dataclass
class Turn:
    role: str
    text: str
    turn: int


class ConversationState:
    """The working set. Bounded, inspectable, and cheap to render for a model."""

    def __init__(self) -> None:
        self.turn = 0
        self.objective: str = ""
        self.previous_objective: str = ""
        self.active_app: str = ""
        self.browser = BrowserContext()
        self.email = EmailContext()
        self.research = ResearchContext()
        self.screen = ScreenContext()
        self.last_file: str = ""
        self.entities: deque[Entity] = deque(maxlen=MAX_ENTITIES)
        self.observations: deque[Observation] = deque(maxlen=MAX_OBSERVATIONS)
        self.turns: deque[Turn] = deque(maxlen=MAX_TURNS)
        self.pending_clarification: PendingClarification | None = None
        self.last_action: dict[str, Any] | None = None

    # -- recording ---------------------------------------------------------
    def begin_turn(self, text: str) -> int:
        self.turn += 1
        self.turns.append(Turn("user", text, self.turn))
        return self.turn

    def note_assistant(self, text: str) -> None:
        """Record the reply. Idempotent, because both the agent (which knows the
        answer first) and the orchestrator (which replies for every path) report
        it, and neither should have to know whether the other already did."""
        if not text:
            return
        last = self.turns[-1] if self.turns else None
        if last is not None and last.role == "assistant" and last.text == text:
            return
        self.turns.append(Turn("assistant", text, self.turn))

    def set_objective(self, goal: str) -> None:
        if goal and goal != self.objective:
            self.previous_objective = self.objective
            self.objective = goal

    def note_observation(self, tool: str, arguments: dict[str, Any], ok: bool,
                         summary: str, data: Any = None, category: str = "") -> Observation:
        observation = Observation(tool=tool, arguments=dict(arguments or {}), ok=ok,
                                  summary=summary or "", turn=self.turn, data=data)
        self.observations.append(observation)
        self.last_action = {"tool": tool, "arguments": dict(arguments or {}), "ok": ok,
                            "summary": summary}
        if ok:
            self._absorb(tool, arguments or {}, data, category)
        return observation

    # -- generic context extraction ---------------------------------------
    def _absorb(self, tool: str, arguments: dict[str, Any], data: Any, category: str) -> None:
        """Turn a tool result into context.

        Dispatch is on *category* and the shape of the payload, so a new tool in
        an existing category contributes context without any change here.
        """
        payload = data if isinstance(data, dict) else {}

        if category == "browser" or "url" in payload:
            url = str(payload.get("url") or arguments.get("url") or "")
            if url:
                self.browser.url = url
                self.browser.title = str(payload.get("title") or "")
                self.browser.app = str(payload.get("browser") or self.browser.app or "")
                self.browser.updated = time.time()
                self.add_entity("url", url, label=self.browser.title or url, source=tool)

        if category == "macos" and tool in {"open_application", "activate_application"}:
            name = str(payload.get("application") or arguments.get("name") or "")
            if name:
                self.active_app = name
                if _looks_like_browser(name):
                    self.browser.app = name
                    self.browser.updated = time.time()
                self.add_entity("app", name, label=name, source=tool)

        if category == "email":
            messages = payload.get("messages")
            if isinstance(messages, list) and messages:
                self.email.messages = messages[:20]
                self.email.updated = time.time()
                for message in self.email.messages:
                    sender = str(message.get("sender") or "")
                    if sender:
                        self.add_entity("person", sender, label=_person_label(sender),
                                        source=tool, extra={"message_id": message.get("id")})
                        self.add_entity("email", str(message.get("id") or sender),
                                        label=str(message.get("subject") or "")[:70],
                                        source=tool, extra=message)
            if isinstance(payload.get("unread"), int):
                self.email.unread = payload["unread"]
            if payload.get("body") and arguments.get("id"):
                focused = next((m for m in self.email.messages
                                if str(m.get("id")) == str(arguments["id"])), None)
                self.email.focused = focused or {"id": arguments["id"],
                                                 "body": payload.get("body", "")[:400]}

        if category == "research" or "sources" in payload:
            sources = payload.get("sources")
            if isinstance(sources, list) and sources:
                self.research.sources = sources[:12]
                self.research.query = str(arguments.get("query") or self.research.query)
                self.research.report = str(payload.get("report") or self.research.report)
                self.research.updated = time.time()
                for index, source in enumerate(sources[:12], start=1):
                    self.add_entity("result", str(source.get("url", "")),
                                    label=f"[{index}] {str(source.get('title', ''))[:60]}",
                                    source=tool, extra={"index": index, **source})

        if category == "screen":
            answer = str(payload.get("answer") or "")
            if answer:
                self.screen.description = answer
                self.screen.elements = _extract_ui_elements(answer)
                self.screen.path = str(payload.get("path") or "")
                self.screen.app = self.active_app
                self.screen.updated = time.time()
                for element in self.screen.elements:
                    self.add_entity("screen_element", element, label=element, source=tool)

        if category == "files":
            path = str(payload.get("path") or arguments.get("path") or "")
            if path:
                self.last_file = path
                self.add_entity("file", path, label=path.rsplit("/", 1)[-1], source=tool)

        if category == "system" and isinstance(payload.get("processes"), list):
            for process in payload["processes"][:6]:
                name = str(process.get("name", ""))
                if name:
                    self.add_entity("process", name, label=name, source=tool, extra=process)

    def add_entity(self, kind: str, value: str, label: str = "", source: str = "",
                   extra: dict[str, Any] | None = None) -> Entity:
        value = (value or "").strip()
        entity = Entity(kind=kind, value=value, label=label or value, source=source,
                        turn=self.turn, extra=extra or {})
        # Re-mention refreshes recency rather than duplicating.
        for existing in list(self.entities):
            if existing.kind == kind and existing.value == value:
                self.entities.remove(existing)
                break
        self.entities.append(entity)
        return entity

    def entities_of(self, *kinds: str) -> list[Entity]:
        """Most recent first."""
        return [e for e in reversed(self.entities) if not kinds or e.kind in kinds]

    # -- clarification -----------------------------------------------------
    def ask(self, clarification: PendingClarification) -> None:
        clarification.asked_turn = self.turn
        self.pending_clarification = clarification

    def take_clarification(self) -> PendingClarification | None:
        pending, self.pending_clarification = self.pending_clarification, None
        return pending

    # -- prompting ---------------------------------------------------------
    def describe_for_model(self, include_turns: int = 4) -> str:
        """A compact, deterministic snapshot. Ordered most-useful-first."""
        # The clock leads because "tomorrow at four" cannot be turned into an
        # ISO timestamp without it, and a model asked to guess the date will
        # cheerfully invent one.
        blocks: list[str] = [dt.datetime.now().strftime("now: %A %d %B %Y, %H:%M")]
        if self.objective:
            line = f"current objective: {self.objective}"
            if self.previous_objective:
                line += f" (previous: {self.previous_objective})"
            blocks.append(line)
        if self.active_app:
            blocks.append(f"frontmost application: {self.active_app}")
        for context in (self.browser, self.email, self.research, self.screen):
            described = context.describe()
            if described:
                blocks.append(described)
        if self.last_file:
            blocks.append(f"last file: {self.last_file}")

        recent = [e for e in self.entities_of() if e.kind in
                  {"person", "url", "result", "app", "file", "email"}][:8]
        if recent:
            blocks.append("recently referenced:\n" +
                          "\n".join(f"- {e.describe()}" for e in recent))

        if self.observations:
            blocks.append("recent actions:\n" + "\n".join(
                f"- {o.describe()}" for o in list(self.observations)[-4:]))

        if self.pending_clarification:
            blocks.append(f"awaiting an answer to: {self.pending_clarification.question}")

        if include_turns and self.turns:
            history = list(self.turns)[-include_turns * 2:]
            blocks.append("conversation:\n" + "\n".join(
                f"{t.role}: {t.text[:180]}" for t in history))
        return "\n\n".join(blocks)

    def snapshot(self) -> dict[str, Any]:
        """For the developer panel and tests."""
        return {
            "turn": self.turn,
            "objective": self.objective,
            "previous_objective": self.previous_objective,
            "active_app": self.active_app,
            "browser": {"app": self.browser.app, "url": self.browser.url,
                        "title": self.browser.title},
            "email": {"count": len(self.email.messages), "unread": self.email.unread,
                      "focused": bool(self.email.focused)},
            "research": {"query": self.research.query, "sources": len(self.research.sources)},
            "screen": {"fresh": self.screen.fresh, "elements": self.screen.elements[:6]},
            "entities": [e.describe() for e in self.entities_of()[:10]],
            "pending_clarification": (self.pending_clarification.question
                                      if self.pending_clarification else None),
        }

    def reset(self) -> None:
        self.__init__()


def attach(state: ConversationState, registry) -> None:
    """Feed every tool result on ``registry`` into ``state``.

    Registered once, by whoever owns the state. Context then accumulates from
    *all* tool use — the fast path's, a capability's, the agent's — rather than
    only from calls the agent happened to make itself.
    """

    def absorb(tool: str, arguments: dict[str, Any], result, category: str) -> None:
        state.note_observation(tool, arguments, result.ok, result.summary,
                               result.data, category)

    registry.observe(absorb)


_BROWSERS = ("safari", "chrome", "firefox", "edge", "arc", "brave", "chromium", "opera")


def _looks_like_browser(name: str) -> bool:
    lowered = (name or "").lower()
    return any(browser in lowered for browser in _BROWSERS)


def _person_label(sender: str) -> str:
    sender = (sender or "").strip()
    if "<" in sender:
        return sender.split("<")[0].strip().strip('"') or sender
    return sender.split("@")[0] if "@" in sender else sender


#: Words that denote an interactive thing a vision description might mention.
_UI_NOUNS = (
    "search bar", "search field", "search box", "address bar", "url bar", "text field",
    "text box", "button", "menu", "tab", "link", "checkbox", "dropdown", "sidebar",
    "toolbar", "dialog", "input field", "password field", "send button", "submit button",
)


def _extract_ui_elements(description: str) -> list[str]:
    """Pull interactive elements out of a vision description.

    Generic noun matching, not a lookup table for any particular screen: the
    point is that a later "click the search bar" has something to bind to.
    """
    found: list[str] = []
    lowered = (description or "").lower()
    for noun in _UI_NOUNS:
        if noun in lowered and noun not in found:
            found.append(noun)
    # Quoted labels are usually buttons or fields worth remembering.
    for quoted in re.findall(r"[\"“']([A-Za-z][\w \-]{1,28})[\"”']", description or ""):
        candidate = quoted.strip()
        if candidate and candidate.lower() not in found and len(found) < 12:
            found.append(candidate)
    return found[:12]
