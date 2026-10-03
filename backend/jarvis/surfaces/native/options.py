"""Which of a menu's items a request means.

A pop-up's items are alternatives, and the name a person (or a model) asks for is rarely the
exact string the app shows: macOS titles a folder "⁨Desktop⁩ — iCloud" when iCloud holds the
Desktop, with the name wrapped in invisible bidirectional isolates (U+2068/U+2069) and a
qualifier after it. Matching has to see through the isolates and the qualifier — and has to stay
honest when more than one item fits, because choosing the wrong one of two is worse than asking.

The rule, tried tier by tier, stopping at the first tier that has any match:

1. **exactly** — the folded labels are equal;
2. **by its name** — the label with its qualifier taken off (" — iCloud", " – …", " - …", " (…)")
   equals the request: "Desktop" is "Desktop — iCloud";
3. **starting with** the request;
4. **containing** it.

Greyed-out items (section headings, mostly) are set aside while any enabled item fits at that
tier or a looser one. One match is the answer. Several are *ambiguous* and refused — with a way
to say which: ``occurrence`` picks the Nth match, in menu order. Two items with identical labels
are two matches: nothing the request could say tells them apart, and the app may mean two
different things by one name (two accounts, two folders on different disks).

Everything here is pure logic over titles; ``NativeSurface`` supplies the items.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any

#: A qualifier: what follows the name after a spaced dash, or in trailing parentheses.
_QUALIFIER = re.compile(r"\s+[—–-]\s+|\s+\(")


@dataclass
class Option:
    """One titled item of a menu, in menu order."""

    element: Any
    title: str
    enabled: bool = True
    #: 1-based position among the menu's titled items.
    index: int = 0
    #: The heading it sits under — the nearest greyed-out item above it — if there is one.
    section: str = ""


def fold(text: str) -> str:
    """Case, spacing, ellipsis style, trailing colon/dots and invisible formatting characters
    (bidi isolates and marks, zero-width joiners) set aside, and em and en dashes read as hyphens:
    "⁨Desktop⁩ — iCloud" and "desktop - icloud" are the same label, and so are "Where:" and "where"."""
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Cf")
    text = " ".join(text.casefold().replace("…", "...").replace("—", "-").replace("–", "-").split())
    return text.rstrip(".: ").strip()


def name_of(label: str) -> str:
    """A folded label without its qualifier: "desktop — icloud" → "desktop"."""
    found = _QUALIFIER.search(label)
    return label[:found.start()].rstrip(".: ") if found else label


@dataclass
class Resolution:
    option: Option | None = None
    #: Every item that fit at the winning tier (enabled ones only, when any are).
    matches: list[Option] = field(default_factory=list)
    tier: str = ""
    ambiguous: bool = False
    #: ``occurrence`` asked for a match there isn't.
    out_of_range: bool = False


def _tiers() -> tuple[tuple[str, Any], ...]:
    return (
        ("exactly", lambda label, wanted: label == wanted),
        ("by its name", lambda label, wanted: name_of(label) == wanted),
        ("starting with it", lambda label, wanted: label.startswith(wanted)),
        ("containing it", lambda label, wanted: wanted in label),
    )


def resolve(options: list[Option], wanted: str, *, occurrence: int | None = None,
            strict: bool = True) -> Resolution:
    """The item *wanted* means. With ``strict=False`` several items with the very same label
    resolve to the first (how a menu path has always been followed: two open windows with one
    title); every looser tier still refuses to guess."""
    target = fold(wanted)
    if not target:
        return Resolution()
    folded = [(option, fold(option.title)) for option in options]
    greyed: Resolution | None = None
    for tier, fits in _tiers():
        hits = [option for option, label in folded if label and fits(label, target)]
        if not hits:
            continue
        matches = [option for option in hits if option.enabled]
        if not matches:
            # Only greyed-out items fit at this tier (a heading with the name): a looser tier may
            # have the enabled one that was meant; if none does, this is what was asked for.
            greyed = greyed or Resolution(hits[0], hits, tier)
            continue
        if occurrence is not None:
            if 1 <= occurrence <= len(matches):
                return Resolution(matches[occurrence - 1], matches, tier)
            return Resolution(None, matches, tier, out_of_range=True)
        if len(matches) == 1 or (tier == "exactly" and not strict):
            return Resolution(matches[0], matches, tier)
        return Resolution(None, matches, tier, ambiguous=True)
    return greyed or Resolution()


def describe(number: int, option: Option) -> str:
    """"2. “Desktop — iCloud” (under “Favourites”)": the *number*th match, and what sets it apart."""
    where = f" (under “{option.section}”)" if option.section else f" (item {option.index} of the menu)"
    return f"{number}. “{option.title}”{where}"


def ordinal(position: int) -> str:
    suffix = "th" if 10 <= position % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(position % 10, "th")
    return f"{position}{suffix}"
