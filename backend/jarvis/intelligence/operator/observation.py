"""What the operator has seen — and the only place its proof may come from.

Two jobs:

* **Looking again after acting.** After any web action the page is read
  again straight away, so the next decision sees the page as it now is,
  with the element handles it needs. No step is spent asking to look.
* **Keeping the record.** Every result and every page listing the model was
  shown goes into an :class:`ObservationLog`. When the model says a checklist
  item is done, the quote it gives as proof must be *in* that log — copied
  from something JARVIS actually saw, not asserted.
"""

from __future__ import annotations

import re

#: Web actions after which the page is looked at again automatically…
WEB_ACTIONS = frozenset({
    "browse_to", "open_url", "click_page_element", "fill_page_field", "submit_page_form",
    "press_page_key", "scroll_page", "page_go_back", "wait_for_page", "ask_user_to_take_over",
})
#: …and app actions after which the app's window is.
APP_ACTIONS = frozenset({
    "click_control", "type_into", "choose_option", "choose_menu_item", "drag_control",
    "click_mark", "click_element", "press_key", "type_text", "scroll", "open_application",
    "activate_application",
})
OBSERVE_AFTER = WEB_ACTIONS | APP_ACTIONS

#: The tool that looks again, the actions that call for it, and the tools
#: that make looking worthwhile — reading a page whose elements the model
#: has no tool to act on only fills the context.
OBSERVERS: dict[str, tuple[frozenset[str], frozenset[str], str]] = {
    "read_page_manifest": (WEB_ACTIONS, frozenset({
        "click_page_element", "fill_page_field", "submit_page_form", "press_page_key"}),
        "The page now"),
    "read_window": (APP_ACTIONS, frozenset({
        "click_control", "type_into", "choose_option", "drag_control"}), "The window now"),
}

#: Words that carry no evidence on their own.
_STOPWORDS = frozenset({
    "a", "an", "and", "are", "as", "at", "be", "been", "but", "by", "for", "from", "has",
    "have", "in", "is", "it", "its", "of", "on", "or", "so", "that", "the", "their", "them",
    "then", "there", "these", "this", "to", "was", "were", "will", "with", "your", "you",
    "i", "my", "me", "we", "our",
})

#: Typographic characters pages and tool summaries use, and their plain forms.
_PLAIN = str.maketrans({"“": '"', "”": '"', "‘": "'", "’": "'", "–": "-", "—": "-",
                        " ": " ", "…": "..."})


def normalise(text: str) -> str:
    return " ".join((text or "").translate(_PLAIN).lower().split())


def _significant(text: str) -> list[str]:
    return [w for w in re.findall(r"[a-z0-9£$€%]+(?:[.'][a-z0-9]+)*", text)
            if len(w) > 1 and w not in _STOPWORDS]


def _phrase_enough(claim: str) -> bool:
    """A quote specific enough to prove something: a phrase, not a keyword.
    "AA batteries" appears on the search results page too; "Added to
    Basket" is a phrase that only appears once it has happened."""
    return len(claim.split()) >= 3 or len(claim) >= 15


class ObservationLog:
    """Everything shown to the model during one run, normalised for matching."""

    def __init__(self) -> None:
        self._texts: list[str] = []

    def add(self, text: str) -> None:
        cleaned = normalise(text)
        if cleaned and (not self._texts or self._texts[-1] != cleaned):
            self._texts.append(cleaned)

    def __len__(self) -> int:
        return len(self._texts)

    def supports(self, evidence: str) -> bool:
        """Is *evidence* a quote of something that was actually observed?

        Accepted when the quote — or a part of it in quotation marks — appears
        verbatim (after normalising case, whitespace and typographic quotes)
        in one observation and is specific enough to mean something; or, to
        tolerate light rewording, when at least four of its significant words
        and 80% of them all come from one observation.
        """
        claim = normalise(evidence).strip(" .!:;,'\"")
        if not claim:
            return False
        if _phrase_enough(claim) and any(claim in text for text in self._texts):
            return True
        for fragment in re.findall(r'"([^"]+)"', normalise(evidence)):
            fragment = fragment.strip(" .!:;,")
            if _phrase_enough(fragment) and any(fragment in text for text in self._texts):
                return True
        words = _significant(claim)
        if len(words) >= 4:
            return any(_near_each_other(words, _significant(text)) for text in self._texts)
        return False


def _near_each_other(words: list[str], text: list[str]) -> bool:
    """At least 80% of *words* inside one short stretch of *text*.

    Proximity is the point: a page of search results contains "AA",
    "batteries", "Amazon" and "Basket" somewhere, so the words alone would
    "prove" that batteries are in the basket before anything was added.
    Light rewording of one real sentence still passes.
    """
    wanted = set(words)
    need = -(-len(wanted) * 4 // 5)              # ceil(80%)
    window = max(12, 3 * len(wanted))
    for start, word in enumerate(text):
        if word not in wanted:
            continue
        found = {w for w in text[start:start + window] if w in wanted}
        if len(found) >= need:
            return True
    return False


def first_line(text: str, limit: int = 160) -> str:
    """One line of a result, for older history: enough to remember what
    happened, without carrying the whole page listing forward."""
    line = next((part.strip() for part in (text or "").splitlines() if part.strip()), "")
    return line[:limit] + ("…" if len(line) > limit else "")
