"""Reference resolution.

Turns "it", "that email", "the second result", "the browser", "him" into
something concrete from :class:`ConversationState`.

The mechanisms are general — kind inference from the noun phrase, ordinals,
deixis, recency scoring and label matching — so a reference the tests never
mention still resolves. When several candidates are genuinely plausible the
resolver says so instead of picking one, and the agent asks.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .state import ConversationState, Entity

#: Noun → entity kind. Extending this adds a vocabulary, not a special case.
_KIND_NOUNS: dict[str, tuple[str, ...]] = {
    "email": ("email", "mail", "message", "reply", "inbox"),
    "person": ("person", "sender", "contact", "him", "her", "them", "they", "he", "she",
               "brother", "sister", "mum", "mom", "dad", "boss", "colleague", "friend",
               "wife", "husband", "partner", "manager", "client"),
    "url": ("page", "site", "website", "url", "link", "tab"),
    "app": ("app", "application", "browser", "window", "program"),
    "file": ("file", "document", "note", "folder", "doc"),
    "result": ("result", "source", "article", "hit", "search result"),
    "process": ("process", "task", "program"),
    "screen_element": ("bar", "field", "button", "box", "menu", "icon", "element", "input"),
}

_PRONOUNS = {"it", "that", "this", "them", "those", "these", "there", "one", "ones"}

_ORDINALS = {
    "first": 1, "1st": 1, "second": 2, "2nd": 2, "third": 3, "3rd": 3, "fourth": 4,
    "4th": 4, "fifth": 5, "5th": 5, "last": -1, "latest": -1, "newest": -1, "final": -1,
}

#: Relationship words name a person JARVIS has no directory for.
_RELATIONSHIPS = {"brother", "sister", "mum", "mom", "mother", "dad", "father", "boss",
                  "wife", "husband", "partner", "manager", "colleague", "friend", "client"}

#: Ordinary words that never distinguish one candidate from another.
_FILLER = {"the", "and", "for", "from", "with", "about", "please", "just", "only",
           "any", "all", "some", "you", "your", "can", "could", "would", "what",
           "which", "who", "whom", "was", "were", "are", "did", "does", "get", "got",
           "open", "show", "tell", "give", "send", "read", "make", "new", "latest",
           "most", "more", "other", "another", "same", "there", "here", "now", "then"}


@dataclass
class Resolution:
    value: str | None = None
    kind: str = "unknown"
    label: str = ""
    confidence: float = 0.0
    candidates: list[Entity] = field(default_factory=list)
    reason: str = ""
    entity: Entity | None = None

    @property
    def resolved(self) -> bool:
        return self.value is not None and self.confidence >= 0.5

    @property
    def ambiguous(self) -> bool:
        return not self.resolved and len(self.candidates) > 1

    def candidate_labels(self, limit: int = 5) -> list[str]:
        seen: list[str] = []
        for candidate in self.candidates:
            label = candidate.label or candidate.value
            if label and label not in seen:
                seen.append(label)
            if len(seen) >= limit:
                break
        return seen


class ReferenceResolver:
    """Binds a reference phrase to something in the working set."""

    def resolve(self, reference: str, state: ConversationState,
                kind_hint: str = "") -> Resolution:
        text = (reference or "").strip().lower()
        if not text:
            return Resolution(reason="empty reference")

        kind = kind_hint or self._infer_kind(text)
        pool = self._pool(state, kind)

        # 1. A relationship word names a person we have no directory for. Offer
        #    the people actually in context rather than inventing one.
        relationship = self._relationship(text)
        if relationship:
            people = state.entities_of("person")
            if len(people) == 1:
                person = people[0]
                return Resolution(person.value, "person", person.label, 0.55, people,
                                  f"only one person in context for “{relationship}”", person)
            salient = self._mentioned(people, state)
            if len(salient) == 1:
                person = salient[0]
                return Resolution(person.value, "person", person.label, 0.7, people,
                                  f"“{relationship}” — the one we've been discussing", person)
            return Resolution(None, "person", "", 0.0, salient or people,
                              f"“{relationship}” is not in any directory I have")

        # 2. Ordinals index an ordered result set ("the second result").
        ordinal = self._ordinal(text)
        if ordinal is not None:
            ordered = self._ordered(state, kind)
            if ordered:
                try:
                    picked = ordered[ordinal - 1] if ordinal > 0 else ordered[-1]
                except IndexError:
                    return Resolution(None, kind, "", 0.0, ordered,
                                      f"there are only {len(ordered)} of those")
                return Resolution(picked.value, picked.kind, picked.label, 0.9, ordered,
                                  f"ordinal {ordinal}", picked)

        # 3. A distinguishing word beats everything else: "the one from Ada"
        #    names Ada, even though "one" is a pronoun.
        if self._discriminating(text):
            matches = self._by_label(text, pool or list(state.entities_of()))
            if len(matches) == 1:
                best = matches[0]
                return Resolution(best.value, best.kind, best.label, 0.85, matches,
                                  "matched a distinguishing word", best)
            if matches:
                return Resolution(None, kind, "", 0.0, matches, "several things match")

        # 4. Context objects answer "the browser" / "that page" directly.
        direct = self._from_context(text, kind, state)
        if direct is not None:
            return direct

        # 5. A bare pronoun takes the most recent salient thing of that kind.
        if self._is_pronoun(text):
            if not pool:
                return Resolution(None, kind, "", 0.0, [], "nothing of that kind in context")
            best = pool[0]
            confidence = 0.8 if len(pool) == 1 or kind != "unknown" else 0.6
            return Resolution(best.value, best.kind, best.label, confidence, pool[:5],
                              "most recent referent", best)

        # 6. Otherwise fall back to a looser label match.
        matches = self._by_label(text, pool or list(state.entities_of()))
        if len(matches) == 1:
            best = matches[0]
            return Resolution(best.value, best.kind, best.label, 0.8, matches,
                              "label match", best)
        if matches:
            salient = self._mentioned(matches, state)
            if len(salient) == 1:
                best = salient[0]
                return Resolution(best.value, best.kind, best.label, 0.7, matches,
                                  "the only one we've been discussing", best)
            return Resolution(None, kind, "", 0.0, matches, "several things match")

        # Nothing matched the words, but the conversation may have settled on
        # one of them anyway — "him", right after talking about Tom.
        salient = self._mentioned(pool, state)
        if len(salient) == 1:
            best = salient[0]
            return Resolution(best.value, best.kind, best.label, 0.7, pool[:5],
                              "the only one we've been discussing", best)
        return Resolution(None, kind, "", 0.0, pool[:5], "no match in context")

    # -- helpers -----------------------------------------------------------
    @staticmethod
    def _mentioned(candidates: list[Entity], state: ConversationState,
                   turns: int = 3) -> list[Entity]:
        """Candidates the conversation has actually been talking about.

        Being in context is not the same as being salient: three people are in
        the inbox, but one of them was named out loud two sentences ago. That is
        what makes "him" mean Tom rather than whoever happened to be added last,
        and it is a general signal — linguistic recency — not a rule about mail.
        """
        recent = " ".join(t.text.lower() for t in list(state.turns)[-turns * 2:])
        if not recent.strip():
            return []
        hits: list[Entity] = []
        for candidate in candidates:
            words = {w for w in re.findall(r"[a-z]{3,}", (candidate.label or "").lower())
                     if w not in _FILLER}
            if words and any(re.search(rf"\b{re.escape(w)}\b", recent) for w in words):
                hits.append(candidate)
        return hits

    @staticmethod
    def _infer_kind(text: str) -> str:
        for kind, nouns in _KIND_NOUNS.items():
            for noun in nouns:
                if re.search(rf"\b{re.escape(noun)}\b", text):
                    return kind
        return "unknown"

    @staticmethod
    def _relationship(text: str) -> str:
        for word in _RELATIONSHIPS:
            if re.search(rf"\b{word}\b", text):
                return word
        return ""

    @staticmethod
    def _ordinal(text: str) -> int | None:
        for word, index in _ORDINALS.items():
            if re.search(rf"\b{re.escape(word)}\b", text):
                return index
        match = re.search(r"\b(\d{1,2})(?:st|nd|rd|th)\b", text)
        return int(match.group(1)) if match else None

    @staticmethod
    def _discriminating(text: str) -> set[str]:
        """Words that could pick one candidate out of several.

        Everything structural — pronouns, ordinals, relationship words, the
        kind nouns themselves and ordinary filler — is removed. What remains is
        content the user supplied to narrow things down.
        """
        structural = set(_PRONOUNS) | set(_ORDINALS) | set(_RELATIONSHIPS) | _FILLER
        for nouns in _KIND_NOUNS.values():
            for noun in nouns:
                structural.update(noun.split())
        words = {w for w in re.findall(r"[a-z0-9@.\-]+", text) if len(w) > 2}
        return words - structural

    @staticmethod
    def _is_pronoun(text: str) -> bool:
        words = set(re.findall(r"[a-z']+", text))
        return bool(words & _PRONOUNS) and len(words) <= 4

    @staticmethod
    def _pool(state: ConversationState, kind: str) -> list[Entity]:
        return state.entities_of(kind) if kind != "unknown" else state.entities_of()

    @staticmethod
    def _ordered(state: ConversationState, kind: str) -> list[Entity]:
        """Entities that carry an explicit position, oldest-first."""
        indexed = [e for e in state.entities_of(kind if kind != "unknown" else "result")
                   if "index" in e.extra]
        if indexed:
            return sorted(indexed, key=lambda e: e.extra.get("index", 0))
        pool = ReferenceResolver._pool(state, kind)
        return list(reversed(pool))

    @staticmethod
    def _from_context(text: str, kind: str, state: ConversationState) -> Resolution | None:
        if kind == "app" and re.search(r"\bbrowser\b", text) and state.browser.app:
            return Resolution(state.browser.app, "app", state.browser.app, 0.9, [],
                              "the browser in use")
        if kind == "url" and state.browser.url:
            return Resolution(state.browser.url, "url", state.browser.title or state.browser.url,
                              0.85, [], "the page currently open")
        if kind == "email" and state.email.focused:
            focused = state.email.focused
            return Resolution(str(focused.get("id", "")), "email",
                              str(focused.get("subject", ""))[:70], 0.85, [],
                              "the message in focus")
        if kind == "file" and state.last_file:
            return Resolution(state.last_file, "file", state.last_file.rsplit("/", 1)[-1],
                              0.8, [], "the file last touched")
        if kind == "screen_element" and state.screen.fresh:
            for element in state.screen.elements:
                if element.lower() in text or _overlaps(text, element.lower()):
                    return Resolution(element, "screen_element", element, 0.8, [],
                                      "element seen on screen")
        return None

    @staticmethod
    def _by_label(text: str, pool: list[Entity]) -> list[Entity]:
        words = {w for w in re.findall(r"[a-z0-9@.\-]+", text) if len(w) > 2}
        if not words:
            return []
        scored: list[tuple[int, Entity]] = []
        for entity in pool:
            haystack = f"{entity.label} {entity.value}".lower()
            hits = sum(1 for word in words if word in haystack)
            if hits:
                scored.append((hits, entity))
        scored.sort(key=lambda pair: pair[0], reverse=True)
        if not scored:
            return []
        best = scored[0][0]
        return [entity for score, entity in scored if score == best]


def _overlaps(text: str, element: str) -> bool:
    """Loose containment so "the search bar" matches "search field"."""
    element_words = {w for w in element.split() if len(w) > 2}
    text_words = {w for w in re.findall(r"[a-z]+", text) if len(w) > 2}
    return bool(element_words & text_words)
