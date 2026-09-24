"""Learning a skill from an errand that worked.

When the operator finishes an errand and proves it — every checklist item
shown on screen — the actions it took can become a recipe for next time:

* each click or field is stored as what it *was* ("the button “Add to
  Basket”"), taken from the listing the model was looking at, never its
  handle;
* the errand's subject ("AA batteries") becomes a parameter wherever it
  appears — in the search URL, in what was typed, and in "the result that
  best matches it" when the model clicked a result named after it;
* the proof becomes the recipe's success check ("Added to Basket"),
  stripped of anything specific to this one run;
* a scroll before a click becomes "scroll until that appears".

Only plain operating steps are learned. A run that sent an email, deleted
something or took a detour it couldn't describe isn't turned into a recipe
— a skill replays without a model watching each step, so it should only
ever be the kind of thing that's safe to repeat. (And it still goes through
the permission gate step by step when it does.)
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote, quote_plus, urlparse

from . import grounding
from .model import Skill, SkillError, load

WEB_STEPS = frozenset({"browse_to", "click_page_element", "fill_page_field", "submit_page_form",
                       "press_page_key", "scroll_page", "wait_for_page"})
NATIVE_STEPS = frozenset({"open_application", "activate_application", "click_control", "type_into",
                          "choose_menu_item", "press_key", "type_text"})
#: Steps that don't change anything by themselves.
_MOVES = frozenset({"go", "key", "wait", "scroll_until", "app"})
_GOAL_STOP = frozenset({"a", "an", "the", "my", "me", "some", "to", "for", "of", "in", "on", "and",
                        "it", "please", "can", "you", "could", "would", "get", "find", "with",
                        "from", "into", "i", "need", "want", "pack", "one", "just", "at", "up"})


def learn(objective: Any, trail: list[dict[str, Any]], evidence: list[str],
          registry: Any) -> Skill | None:
    """A learned skill from a proven run, or None when it shouldn't be one."""
    targets = [str(t).strip() for t in getattr(objective, "targets", None) or [] if str(t).strip()]
    query = targets[0] if targets else ""
    steps: list[dict[str, Any]] = []
    surface = site = app = ""
    scroll_pending = uses_query = False

    for entry in trail:
        if not entry.get("ok"):
            continue
        tool, args, view = entry["tool"], dict(entry.get("arguments") or {}), entry.get("view") or ""
        kind = "web" if tool in WEB_STEPS else "native" if tool in NATIVE_STEPS else ""
        if not kind:
            spec = getattr(registry.get(tool), "spec", None) if registry is not None else None
            if spec is None or spec.changes_state:
                return None                    # not a plain operating step
            continue
        if surface and kind != surface:
            return None
        surface = kind
        if tool == "browse_to":
            url = str(args.get("url") or "")
            if not url:
                return None
            site = site or _host(url)
            templated, used = _template(url, query, url=True)
            uses_query |= used
            steps.append({"go": templated})
        elif tool in {"click_page_element", "click_control", "submit_page_form"}:
            item = grounding.element(view, str(args.get("handle") or ""))
            if item is None or (tool == "submit_page_form" and item.role not in {"button", "link"}):
                return None
            spec, used = _describe(item, query)
            uses_query |= used
            if scroll_pending:
                steps.append({"scroll_until": spec})
                scroll_pending = False
            steps.append({"click": spec})
        elif tool in {"fill_page_field", "type_into"}:
            item = grounding.element(view, str(args.get("handle") or ""))
            if item is None:
                return None
            text, used = _template(str(args.get("text") or ""), query)
            uses_query |= used
            spec = {"text": item.text, "role": item.role} if item.text else {"role": item.role}
            if scroll_pending:
                steps.append({"scroll_until": spec})
                scroll_pending = False
            steps.append({"fill": spec, "with": text, "submit": bool(args.get("submit"))})
        elif tool in {"press_page_key", "press_key"}:
            steps.append({"key": str(args.get("key") or "")})
        elif tool == "scroll_page":
            scroll_pending = True
        elif tool == "wait_for_page":
            if args.get("text"):
                text, used = _template(str(args["text"]), query)
                uses_query |= used
                steps.append({"wait": text})
        elif tool in {"open_application", "activate_application"}:
            app = app or str(args.get("name") or "")
            steps.append({"app": str(args.get("name") or "")})
        elif tool == "choose_menu_item":
            steps.append({"menu": [str(p) for p in args.get("path") or []]})
        elif tool == "type_text":
            text, used = _template(str(args.get("text") or ""), query)
            uses_query |= used
            steps.append({"type": text})

    acting = [s for s in steps if next(iter(s)) not in _MOVES]
    if not surface or len(steps) < 2 or not acting:
        return None
    place = _site_name(site) if surface == "web" else (app or str(getattr(objective, "app", "") or ""))
    if not place:
        return None
    words = _intent_words(str(getattr(objective, "goal", "") or ""), query, place)
    if not words:
        return None
    data = {
        "id": f"learned-{_slug(place)}-{'-'.join(words[:3])}",
        "title": "Learned: " + _template(str(getattr(objective, "goal", "") or ""), query)[0][:80],
        "description": f"A recipe JARVIS learned: {getattr(objective, 'goal', '')}".strip()[:160],
        "surface": surface,
        "sites": [place.lower()] if surface == "web" else [],
        "apps": [place] if surface == "native" else [],
        "words": words,
        "params": ({"query": {"description": "what it's for this time", "from": "target"}}
                   if uses_query else {}),
        "steps": steps,
        "done_when": proof_phrases(evidence, query),
    }
    try:
        return load(data, source="learned")
    except SkillError:
        return None


def proof_phrases(evidence: list[str], query: str = "") -> list[str]:
    """Short phrases from the proof that would show again next time —
    nothing naming this run's product or containing this run's numbers."""
    avoid = grounding.words(query)
    phrases: list[str] = []
    for quote_text in evidence:
        for sentence in re.split(r"[.!?\n]", quote_text or ""):
            # JARVIS's own account of an action ("Clicked “Add to Basket”")
            # says what was pressed, not that it worked.
            if sentence.strip().lower().startswith(("clicked", "typed", "pressed", "now on", "opened",
                                                    "went", "scrolled", "chose", "submitted")):
                continue
            for fragment in re.split(r"\s[—–-]\s|“|”|\"", sentence):
                fragment = " ".join(fragment.split()).strip(" :,;")
                count = len(fragment.split())
                if not 2 <= count <= 8 or re.search(r"\d", fragment):
                    continue
                if avoid & grounding.words(fragment):
                    continue
                if fragment not in phrases:
                    phrases.append(fragment)
    return phrases[:3]


def _describe(item: grounding.Listed, query: str) -> tuple[dict[str, Any], bool]:
    """How to find this element again: by its own words, or — when it was
    named after the errand's subject — as the best match for the next one."""
    wanted = grounding.words(query)
    shared = wanted & grounding.words(item.text)
    if wanted and len(shared) * 2 >= len(wanted):
        spec: dict[str, Any] = {"role": item.role, "best_match": "{query}"}
        fragment = _href_fragment(item.href)
        if fragment:
            spec["href"] = fragment
        return spec, True
    return ({"text": item.text, "role": item.role} if item.text else {"role": item.role}), False


def _template(text: str, query: str, *, url: bool = False) -> tuple[str, bool]:
    if not query or not text:
        return text, False
    slot = "{query|url}" if url else "{query}"
    forms = [quote_plus(query), quote(query), query.replace(" ", "+"), query] if url else [query]
    for form in forms:
        pattern = re.compile(re.escape(form), re.IGNORECASE)
        if pattern.search(text):
            return pattern.sub(lambda _m: slot, text), True
    return text, False


def _host(url: str) -> str:
    parsed = urlparse(url if "//" in url else f"//{url}")
    return (parsed.hostname or "").lower()


def _site_name(host: str) -> str:
    """"www.amazon.co.uk" → "amazon": the part people say."""
    labels = [label for label in host.split(".") if label and label != "www"]
    if not labels:
        return ""
    for label in labels:
        if label not in {"m", "en", "uk", "co", "com", "org", "net", "shop", "store"}:
            return label
    return labels[0]


def _href_fragment(href: str) -> str:
    if not href:
        return ""
    path = urlparse(href if "//" in href else f"//{href}").path
    segments = [s for s in path.split("/") if s]
    return f"/{segments[0]}/" if len(segments) >= 2 else ""


def _intent_words(goal: str, query: str, place: str) -> list[str]:
    avoid = grounding.words(query) | grounding.words(place) | _GOAL_STOP
    out: list[str] = []
    for word in re.findall(r"[a-z]+", goal.lower()):
        if len(word) > 2 and word not in avoid and word not in out:
            out.append(word)
    return out[:4]


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
