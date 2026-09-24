"""Finding an element by what it is, in a listing JARVIS just produced.

Skills — built-in or learned — never store handles: ``[jv12]`` means
nothing on the next visit. They store what a person would say: *the "Add to
Basket" button*, *the product link that best matches "AA batteries"*, *the
search field*. Grounding turns that description into the handle it has
*now*, from the latest page listing (``tools/browser/observe.py``) or window
listing (``surfaces/native/ax.py``), both of which render elements as::

    [jv12] button "Add to Basket"
    [ax7] search field "Search" placeholder="Search notes"
    [jv31] link "Duracell Plus AA Batteries, Pack of 24" → www.amazon.co.uk/dp/B0AABAT24A
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: One listed element. Roles may be several words ("search field").
ELEMENT_LINE = re.compile(
    r'^\s*\[(?P<handle>[A-Za-z]*\d+)\]\s+(?P<role>[a-z][a-z -]*?)\s+"(?P<text>[^"\n]*)"(?P<rest>[^\n]*)$',
    re.M)
_HREF = re.compile(r"→\s*(\S+)")
_WORD = re.compile(r"[a-z0-9]+")
_STOP = frozenset({"a", "an", "the", "of", "for", "and", "to", "in", "on", "with", "my", "some",
                   "pack", "me", "please"})
#: Roles that take typing, on the web and in apps.
FILLABLE = frozenset({"field", "select", "textbox", "searchbox", "combobox", "textarea", "input",
                      "search field", "text area", "date field", "time field"})


@dataclass(frozen=True)
class Listed:
    handle: str
    role: str
    text: str
    rest: str
    position: int

    @property
    def href(self) -> str:
        match = _HREF.search(self.rest)
        return match.group(1) if match else ""

    def describe(self) -> str:
        return f'{self.role} "{self.text}"'


def parse(listing: str) -> list[Listed]:
    return [Listed(m["handle"], m["role"].strip().lower(), m["text"], m["rest"], i)
            for i, m in enumerate(ELEMENT_LINE.finditer(listing or ""))]


def element(listing: str, handle: str) -> Listed | None:
    """What *handle* was in *listing* — for learning what was clicked."""
    wanted = handle.strip().strip("[]").lower()
    return next((e for e in parse(listing) if e.handle.lower() == wanted), None)


def words(text: str) -> set[str]:
    return {w for w in _WORD.findall((text or "").lower()) if w not in _STOP and len(w) > 1}


def _normal(text: str) -> str:
    return " ".join((text or "").lower().replace("…", "...").split()).strip(" .:")


def find(listing: str, *, text: str | list[str] = "", role: str | list[str] = "",
         href: str = "", best_match: str = "", fillable: bool = False) -> Listed | None:
    """The element best fitting the description, or None.

    * ``text`` — one label or several alternatives ("Add to Basket" / "Add
      to Cart"): an exact label beats one that starts with it, which beats one
      containing it; later in the listing wins a tie (the latest dialog).
    * ``role`` — one role or several; ``fillable`` — anything you can type in.
    * ``href`` — a fragment its link must contain ("/dp/" on Amazon).
    * ``best_match`` — the one sharing the most words with this text (the
      search result for "AA batteries"), first in the listing on a tie —
      never one sharing none.
    """
    roles = {role} if isinstance(role, str) and role else set(role or [])
    roles = {r.lower() for r in roles}
    texts = [text] if isinstance(text, str) else list(text or [])
    texts = [_normal(t) for t in texts if t and t.strip()]
    candidates = []
    for item in parse(listing):
        if roles and item.role not in roles:
            continue
        if fillable and item.role not in FILLABLE:
            continue
        if href and href.lower() not in item.href.lower():
            continue
        candidates.append(item)
    if texts:
        best: tuple[int, int, Listed] | None = None
        for item in candidates:
            label = _normal(item.text)
            rank = 0
            for wanted in texts:
                if label == wanted:
                    rank = max(rank, 3)
                elif label.startswith(wanted):
                    rank = max(rank, 2)
                elif wanted in label:
                    rank = max(rank, 1)
            if rank and (best is None or (rank, item.position) >= (best[0], best[1])):
                best = (rank, item.position, item)
        candidates = [best[2]] if best else []
    if best_match:
        wanted_words = words(best_match)
        scored = [(len(wanted_words & words(item.text)), -item.position, item) for item in candidates]
        scored = [s for s in scored if s[0] > 0]
        if not scored:
            return None
        # All of the query's words present beats most of them.
        return max(scored, key=lambda s: (s[0], s[1]))[2]
    return candidates[0] if candidates else None


def mentions(listing: str, phrases: list[str]) -> str:
    """The first of *phrases* the listing shows (dialogs and page text
    included), or ""."""
    haystack = _normal(listing)
    for phrase in phrases:
        if phrase and _normal(phrase) in haystack:
            return phrase
    return ""
