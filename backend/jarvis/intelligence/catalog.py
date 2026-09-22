"""Tool selection.

The reasoning model never sees all 50 tools. It sees a shortlist, described in
the terms a decision needs — what the tool is for, what it needs, what comes
back, whether it changes anything and whether it will ask the user first.

Shortlisting scores every tool against the objective and the current context.
It is deliberately generic: a new tool becomes selectable by existing in the
registry with a sensible description, with no change here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from ..tools.base import ToolSpec
from .state import ConversationState

#: Tools worth offering for almost any objective — cheap, read-only context.
ALWAYS_OFFER = ("get_time", "get_system_info")

#: Context → tool categories that become more relevant when that context is live.
_CONTEXT_BOOSTS = {
    "browser": ("browser", "research"),
    "email": ("email",),
    "research": ("research",),
    "screen": ("screen", "macos"),
}

_STOPWORDS = {"the", "a", "an", "my", "me", "i", "to", "for", "of", "and", "or", "in",
              "on", "is", "it", "that", "this", "please", "can", "you", "what", "whats",
              "get", "give", "show", "tell", "do", "does", "with", "about", "from"}


@dataclass
class ToolCard:
    """A tool as the model sees it."""

    name: str
    purpose: str
    required: list[str] = field(default_factory=list)
    optional: list[str] = field(default_factory=list)
    types: dict[str, str] = field(default_factory=dict)
    risk: str = "low"
    returns: str = ""
    mutates: bool = False
    retryable: bool = True
    confirms: bool = False
    category: str = ""

    def render(self) -> str:
        """One compact line. Small models cope badly with verbose schemas."""
        args = []
        for name in self.required:
            args.append(f"{name}:{self.types.get(name, 'string')}")
        for name in self.optional:
            args.append(f"[{name}:{self.types.get(name, 'string')}]")
        signature = f"{self.name}({', '.join(args)})"
        notes = []
        if self.returns:
            notes.append(f"returns {self.returns}")
        if self.mutates:
            notes.append("changes state")
        if self.confirms:
            notes.append("asks the user first")
        suffix = f" — {'; '.join(notes)}" if notes else ""
        return f"- {signature}: {self.purpose}{suffix}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name, "purpose": self.purpose, "required": self.required,
            "optional": self.optional, "risk": self.risk, "returns": self.returns,
            "mutates": self.mutates, "retryable": self.retryable,
            "confirms": self.confirms, "category": self.category,
        }


class ToolCatalog:
    def __init__(self, registry):
        self._registry = registry

    # -- cards -------------------------------------------------------------
    def card(self, name: str) -> ToolCard | None:
        tool = self._registry.get(name)
        return self._card_for(tool.spec) if tool else None

    @staticmethod
    def _card_for(spec: ToolSpec) -> ToolCard:
        properties: dict[str, Any] = spec.parameters.get("properties", {})
        required = list(spec.parameters.get("required", []))
        optional = [key for key in properties if key not in required]
        return ToolCard(
            name=spec.name,
            purpose=spec.description,
            required=required,
            optional=optional,
            types={key: str(value.get("type", "string")) for key, value in properties.items()},
            risk=spec.risk,
            returns=spec.returns or _derive_returns(spec),
            mutates=spec.changes_state,
            retryable=spec.safe_to_retry,
            confirms=spec.needs_confirmation,
            category=spec.category,
        )

    def all_cards(self) -> list[ToolCard]:
        return [self._card_for(tool.spec) for tool in self._registry._tools.values()]

    # -- selection ---------------------------------------------------------
    def shortlist(self, objective, state: ConversationState | None = None,
                  limit: int = 12) -> list[ToolCard]:
        """Rank tools against the objective and current context."""
        terms = self._terms(objective)
        live = self._live_contexts(state)
        scored: list[tuple[float, ToolCard]] = []

        for card in self.all_cards():
            score = 0.0
            spec = self._registry.get(card.name).spec
            words = _tokens(f"{card.name} {card.purpose} {card.category}")
            score += 2.0 * _overlap(terms, words)
            example_words = _tokens(" ".join(spec.examples))
            score += 0.8 * _overlap(terms, example_words)
            if card.category in live:
                score += 1.5
            if card.name in ALWAYS_OFFER:
                score += 0.2
            # Prefer reading over changing when scores are otherwise equal: a
            # read is a safer first move and usually informs the next one.
            if not card.mutates:
                score += 0.3
            if score > 0:
                scored.append((score, card))

        scored.sort(key=lambda pair: pair[0], reverse=True)
        chosen = [card for _, card in scored[:limit]]
        for name in ALWAYS_OFFER:
            if not any(card.name == name for card in chosen):
                extra = self.card(name)
                if extra is not None:
                    chosen.append(extra)
        return chosen[: limit + len(ALWAYS_OFFER)]

    @staticmethod
    def _terms(objective) -> set[str]:
        text = " ".join(filter(None, [
            getattr(objective, "goal", "") or "",
            getattr(objective, "kind", "") or "",
            " ".join(getattr(objective, "targets", []) or []),
            " ".join(getattr(objective, "constraints", []) or []),
        ])).lower()
        return _tokens(text) - _STOPWORDS

    @staticmethod
    def _live_contexts(state: ConversationState | None) -> set[str]:
        if state is None:
            return set()
        live: set[str] = set()
        if state.browser.url or state.browser.app:
            live.update(_CONTEXT_BOOSTS["browser"])
        if state.email.messages:
            live.update(_CONTEXT_BOOSTS["email"])
        if state.research.sources:
            live.update(_CONTEXT_BOOSTS["research"])
        if state.screen.fresh:
            live.update(_CONTEXT_BOOSTS["screen"])
        return live

    @staticmethod
    def render(cards: list[ToolCard]) -> str:
        return "\n".join(card.render() for card in cards)

    # -- validation --------------------------------------------------------
    def validate_call(self, name: str, arguments: dict[str, Any]) -> tuple[bool, str, dict]:
        """Check a proposed call before executing it.

        Returns ``(ok, problem, cleaned_arguments)``. Problems are phrased for
        the model, so a failed validation can be fed back as an observation and
        corrected on the next step.
        """
        tool = self._registry.get(name)
        if tool is None:
            return False, f"there is no tool called {name}", {}
        try:
            cleaned = tool.validate(arguments or {})
        except ValueError as exc:
            return False, str(exc), {}
        return True, "", cleaned


def _tokens(text: str) -> set[str]:
    """Words of three or more letters, lightly stemmed.

    Matching whole tokens rather than substrings matters: "out" must not match
    inside "output volume", which is exactly the sort of noise that makes a
    shortlist useless.
    """
    words = set()
    for word in re.findall(r"[a-z]{3,}", (text or "").lower().replace("_", " ")):
        words.add(word)
        words.add(_stem(word))
    return words


def _stem(word: str) -> str:
    for suffix in ("ing", "ies", "es", "s"):
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            return word[: -len(suffix)] + ("y" if suffix == "ies" else "")
    return word


def _overlap(terms: set[str], words: set[str]) -> float:
    """Shared tokens, allowing a prefix match so "website" meets "web"."""
    score = 0.0
    for term in terms:
        if term in words:
            score += 1.0
            continue
        if len(term) >= 5 and any(w.startswith(term[:4]) for w in words if len(w) >= 4):
            score += 0.6
    return score


def _derive_returns(spec: ToolSpec) -> str:
    """A sensible default description of a tool's output."""
    by_category = {
        "system": "system measurements",
        "macos": "the result of the action",
        "browser": "page url, title and text",
        "email": "messages with sender, subject and preview",
        "calendar": "events with times and titles",
        "reminders": "reminders with a title, due date and list",
        "messages": "text messages with sender, text and date",
        "files": "file contents or a listing",
        "research": "sources with titles, urls and extracts",
        "screen": "a description of what is on screen",
        "clipboard": "the clipboard contents",
    }
    return by_category.get(spec.category, "a short result summary")
