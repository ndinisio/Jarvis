"""What a skill is: a recipe for one common errand, written as a person would.

A skill says *where* it applies (sites, apps, a few intent words), what it
needs (parameters, usually the thing the errand is about), the steps — in
terms of what's on screen, never handles — and what proves it worked::

    id: amazon-add-to-basket
    title: Add a product to the Amazon basket
    sites: [amazon]
    words: [basket, cart]
    params:
      query: {description: what to buy, from: target}
    steps:
      - go: "https://www.amazon.co.uk/s?k={query|url}"
      - click: {role: link, href: /dp/, best_match: "{query}"}
      - click: {text: [Add to Basket, Add to Cart], role: button}
    done_when: [Added to Basket, Added to Cart]
    summary: "I've added {query} to your Amazon basket."

Step kinds: ``go`` (open a URL), ``open`` (a URL scheme like ``maps://``),
``app`` (open an app), ``click``, ``fill`` (with ``with`` and ``submit``),
``key``, ``menu`` (a menu path), ``type``, ``wait`` (text on the page),
``scroll_until`` (scroll until something appears), ``expect`` (something
must be showing) and ``tool`` (any tool, with templated arguments — built-in
skills only).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote_plus

STEP_KINDS = frozenset({"go", "open", "app", "click", "fill", "key", "menu", "type", "wait",
                        "scroll_until", "expect", "tool"})
#: What may sit beside a step's action: ``fill`` takes ``with`` and
#: ``submit``; ``tool`` takes ``args``; any step may be ``optional``;
#: ``scroll_until`` takes ``max``.
STEP_OPTIONS = frozenset({"with", "submit", "optional", "max", "args"})
_SLOT = re.compile(r"\{(\w+)(?:\|(\w+))?\}")


class SkillError(ValueError):
    pass


@dataclass
class Param:
    name: str
    description: str = ""
    required: bool = True
    #: Where a direct run finds it: "target" (the objective's first target),
    #: "domain" (the site the user named, when it's one of this skill's),
    #: "app", or "" (only when the operator supplies it).
    source: str = ""
    #: Used when the source has nothing ("www.amazon.co.uk").
    default: str = ""


@dataclass
class Skill:
    id: str
    title: str
    steps: list[dict[str, Any]]
    description: str = ""
    surface: str = "web"
    sites: list[str] = field(default_factory=list)
    apps: list[str] = field(default_factory=list)
    words: list[str] = field(default_factory=list)
    #: Word groups that must each be present ("basket or cart" and "add or put").
    requires: list[list[str]] = field(default_factory=list)
    #: Words that rule it out ("add" for a skill that only shows the basket).
    excludes: list[str] = field(default_factory=list)
    #: The app needn't be named ("directions to the airport" means Maps).
    implied: bool = False
    params: list[Param] = field(default_factory=list)
    done_when: list[str] = field(default_factory=list)
    summary: str = ""
    source: str = "builtin"
    #: Learned skills: how it has gone.
    uses: int = 0
    failures_in_row: int = 0

    @property
    def tool_name(self) -> str:
        return "skill_" + re.sub(r"[^a-z0-9]+", "_", self.id.lower()).strip("_")[:58]

    def parameter_schema(self) -> dict[str, Any]:
        """What the operator is asked for — parameters with a default are
        filled in, not asked about."""
        asked = [p for p in self.params if not p.default]
        return {"type": "object",
                "properties": {p.name: {"type": "string", "description": p.description or p.name}
                               for p in asked},
                "required": [p.name for p in asked if p.required]}

    def as_dict(self) -> dict[str, Any]:
        return {"id": self.id, "title": self.title, "description": self.description,
                "surface": self.surface, "sites": self.sites, "apps": self.apps,
                "words": self.words, "requires": self.requires, "excludes": self.excludes,
                "implied": self.implied, "params": {p.name: {"description": p.description,
                                                         "required": p.required, "from": p.source,
                                                         "default": p.default}
                                                for p in self.params},
                "steps": self.steps, "done_when": self.done_when, "summary": self.summary,
                "source": self.source, "uses": self.uses, "failures_in_row": self.failures_in_row}


def load(data: dict[str, Any], *, source: str = "builtin") -> Skill:
    """Validate one skill definition (from YAML or a learned JSON file)."""
    if not isinstance(data, dict):
        raise SkillError("a skill must be a mapping")
    skill_id = str(data.get("id") or "").strip()
    title = str(data.get("title") or "").strip()
    steps = data.get("steps")
    if not skill_id or not title:
        raise SkillError("a skill needs an id and a title")
    if not isinstance(steps, list) or not steps:
        raise SkillError(f"{skill_id}: a skill needs steps")
    for index, step in enumerate(steps, 1):
        if not isinstance(step, dict):
            raise SkillError(f"{skill_id}: step {index} must be a mapping")
        actions = [key for key in step if key in STEP_KINDS]
        unknown = [key for key in step if key not in STEP_KINDS and key not in STEP_OPTIONS]
        if unknown:
            raise SkillError(f"{skill_id}: step {index} has an unknown action {unknown[0]!r}")
        if len(actions) != 1:
            raise SkillError(f"{skill_id}: step {index} must have exactly one action")
        if actions[0] == "tool" and source != "builtin":
            raise SkillError(f"{skill_id}: only built-in skills may call tools directly")
    params = []
    for name, spec in (data.get("params") or {}).items():
        spec = spec if isinstance(spec, dict) else {"description": str(spec)}
        params.append(Param(name=str(name), description=str(spec.get("description") or ""),
                            required=spec.get("required", True) is not False,
                            source=str(spec.get("from") or ""),
                            default=str(spec.get("default") or "")))
    return Skill(
        id=skill_id, title=title, steps=list(steps),
        description=str(data.get("description") or title),
        surface=str(data.get("surface") or ("native" if data.get("apps") else "web")),
        sites=[str(s).lower() for s in data.get("sites") or []],
        apps=[str(a) for a in data.get("apps") or []],
        words=[str(w).lower() for w in data.get("words") or []],
        requires=[[str(w).lower() for w in group] for group in data.get("requires") or []
                  if isinstance(group, list) and group],
        excludes=[str(w).lower() for w in data.get("excludes") or []],
        implied=bool(data.get("implied")),
        params=params,
        done_when=[str(t) for t in data.get("done_when") or []],
        summary=str(data.get("summary") or ""),
        source=source,
        uses=int(data.get("uses") or 0),
        failures_in_row=int(data.get("failures_in_row") or 0),
    )


def kind_of(step: dict[str, Any]) -> str:
    return next(key for key in step if key in STEP_KINDS)


def render(value: Any, params: dict[str, str]) -> Any:
    """Fill ``{name}`` and ``{name|url}`` slots in strings, lists and dicts."""
    if isinstance(value, str):
        def slot(match: re.Match) -> str:
            name, transform = match.group(1), match.group(2)
            if name not in params:
                raise SkillError(f"missing {name}")
            text = str(params[name])
            return quote_plus(text) if transform == "url" else text
        return _SLOT.sub(slot, value)
    if isinstance(value, list):
        return [render(item, params) for item in value]
    if isinstance(value, dict):
        return {key: render(item, params) for key, item in value.items()}
    return value
