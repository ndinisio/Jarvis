"""All the skills JARVIS knows, and which one fits an errand.

Built-in skills ship as YAML next to this file; your own go in
``~/JARVIS/skills/*.yaml`` in the same format; learned ones are JSON in
``~/JARVIS/skills/learned``, one per site-and-intent. The library answers
three questions:

* **Which skill can just run?** (:meth:`direct`) — the errand names a site or
  app a skill covers, uses its words, and everything the skill needs is known
  (the product, the destination). Then no model is needed for the steps.
* **Which skills should the operator be offered?** (:meth:`offer`) — the same
  fit, but the operator supplies what's missing; it sees each as one more tool.
* **What's worth knowing about this site or app?** (:meth:`knowledge`) —
  short tips (``knowledge/*.md``) added to the operator's brief.

A learned skill that fails twice in a row is set aside: a recipe that no
longer matches the site is worse than none.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from ..core.logging import get_logger
from .model import Skill, SkillError, load

log = get_logger("jarvis.skills")

HERE = Path(__file__).resolve().parent
BUILTIN_DIR = HERE / "builtin"
KNOWLEDGE_DIR = HERE / "knowledge"
#: Consecutive failures after which a learned skill is set aside.
MAX_FAILURES = 2
#: How much of the knowledge packs a brief carries.
KNOWLEDGE_CHARS = 700


class SkillLibrary:
    def __init__(self, learned_dir: Path | None = None, *, builtin_dir: Path = BUILTIN_DIR,
                 knowledge_dir: Path = KNOWLEDGE_DIR, disabled: list[str] | tuple[str, ...] = (),
                 user_dir: Path | None = None):
        self.learned_dir = Path(learned_dir) if learned_dir else None
        #: Recipes the user wrote (``~/JARVIS/skills/*.yaml``): the same format
        #: as the built-in ones, minus direct tool calls.
        self.user_dir = Path(user_dir) if user_dir else None
        self.builtin_dir = Path(builtin_dir)
        self.knowledge_dir = Path(knowledge_dir)
        self.disabled = {d.strip().lower() for d in disabled}
        self._skills: dict[str, Skill] = {}
        self._knowledge: list[tuple[list[str], list[str], str]] = []
        self.reload()

    # -- loading ---------------------------------------------------------------------
    def reload(self) -> None:
        self._skills = {}
        for path in sorted(self.builtin_dir.glob("*.yaml")):
            for data in _yaml_documents(path):
                self._add(data, "builtin", path)
        if self.user_dir and self.user_dir.exists():
            for path in sorted(self.user_dir.glob("*.yaml")):
                for data in _yaml_documents(path):
                    self._add(data, "user", path)
        if self.learned_dir and self.learned_dir.exists():
            for path in sorted(self.learned_dir.glob("*.json")):
                try:
                    self._add(json.loads(path.read_text(encoding="utf-8")), "learned", path)
                except (OSError, ValueError) as exc:
                    log.warning("learned skill %s unreadable: %s", path.name, exc)
        self._knowledge = []
        for path in sorted(self.knowledge_dir.glob("*.md")):
            self._knowledge.append(_knowledge_pack(path))

    def _add(self, data: Any, source: str, path: Path) -> None:
        try:
            skill = load(data, source=source)
        except SkillError as exc:
            log.warning("skill in %s skipped: %s", path.name, exc)
            return
        self._skills[skill.id] = skill

    # -- lookup ------------------------------------------------------------------------
    def skills(self) -> list[Skill]:
        return [s for s in self._skills.values() if self._usable(s)]

    def get(self, name: str) -> Skill | None:
        """By id or by the tool name the operator sees."""
        skill = self._skills.get(name)
        if skill is None:
            skill = next((s for s in self._skills.values() if s.tool_name == name), None)
        return skill if skill is not None and self._usable(skill) else None

    def _usable(self, skill: Skill) -> bool:
        if skill.id.lower() in self.disabled:
            return False
        return not (skill.source == "learned" and skill.failures_in_row >= MAX_FAILURES)

    def relevant(self, objective: Any, text: str = "") -> list[Skill]:
        """Skills that fit, best first. None at all when the request is to
        buy something: no skill buys, so any skill that "fits" such a
        request would do something else and call it done."""
        about = _about(objective, text)
        if wants_to_buy(about):
            return []
        scored = []
        for skill in self.skills():
            score = _fit(skill, about)
            if score > 0:
                scored.append((score, skill.source == "learned", skill))
        scored.sort(key=lambda s: (s[0], s[1]), reverse=True)
        return [s for _, _, s in scored]

    def direct(self, objective: Any, text: str = "") -> tuple[Skill, dict[str, str]] | None:
        """The skill that can run without a model, and its parameters."""
        for skill in self.relevant(objective, text):
            params = self.parameters(skill, objective)
            if params is not None:
                return skill, params
        return None

    def offer(self, objective: Any, text: str = "", limit: int = 3) -> list[Skill]:
        return self.relevant(objective, text)[:limit]

    def parameters(self, skill: Skill, objective: Any, given: dict[str, Any] | None = None
                   ) -> dict[str, str] | None:
        """Every parameter's value, or None if a required one is unknown."""
        given = {k: str(v) for k, v in (given or {}).items() if v not in (None, "")}
        values: dict[str, str] = {}
        for param in skill.params:
            value = given.get(param.name, "")
            if not value and param.source == "target":
                targets = [t for t in getattr(objective, "targets", None) or [] if str(t).strip()]
                value = str(targets[0]).strip() if targets else ""
            elif not value and param.source == "domain":
                value = _domain(getattr(objective, "site", "") or "", skill.sites)
            elif not value and param.source == "app":
                value = str(getattr(objective, "app", "") or "").strip()
            value = value or param.default
            if not value and param.required:
                return None
            if value:
                values[param.name] = value
        return values

    def knowledge(self, objective: Any = None, text: str = "", url: str = "") -> str:
        """Tips for the sites and apps this errand involves."""
        about = _about(objective, text) + " " + (url or "").lower()
        chosen = [body for sites, apps, body in self._knowledge
                  if any(site in about for site in sites) or any(app.lower() in about for app in apps)]
        joined = "\n".join(chosen).strip()
        return joined[:KNOWLEDGE_CHARS]

    # -- learned skills ----------------------------------------------------------------------
    def save(self, skill: Skill) -> Path | None:
        if self.learned_dir is None or skill.source != "learned":
            return None
        self.learned_dir.mkdir(parents=True, exist_ok=True)
        path = self.learned_dir / f"{_slug(skill.id)}.json"
        path.write_text(json.dumps(skill.as_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
        self._skills[skill.id] = skill
        return path

    def record(self, skill: Skill, ok: bool) -> None:
        """How a run went — a learned skill that keeps failing is set aside."""
        skill.uses += 1
        skill.failures_in_row = 0 if ok else skill.failures_in_row + 1
        if skill.source == "learned":
            self.save(skill)

    def forget(self, skill_id: str) -> bool:
        skill = self._skills.get(skill_id)
        if skill is None or skill.source != "learned" or self.learned_dir is None:
            return False
        path = self.learned_dir / f"{_slug(skill.id)}.json"
        path.unlink(missing_ok=True)
        del self._skills[skill_id]
        return True

    def listing(self) -> list[dict[str, Any]]:
        return [{"id": s.id, "title": s.title, "source": s.source, "sites": s.sites, "apps": s.apps,
                 "uses": s.uses, "set_aside": not self._usable(s)}
                for s in sorted(self._skills.values(), key=lambda s: (s.source != "learned", s.id))]


# -- fitting ------------------------------------------------------------------------------------
def _about(objective: Any, text: str) -> str:
    parts = [text]
    if objective is not None:
        parts += [getattr(objective, "goal", "") or "", getattr(objective, "site", "") or "",
                  getattr(objective, "app", "") or "",
                  " ".join(str(t) for t in getattr(objective, "targets", None) or [])]
    return " ".join(parts).lower()


def _fit(skill: Skill, about: str) -> int:
    """0 when the skill doesn't apply; otherwise higher is better."""
    place = 0
    if skill.sites:
        if not any(site in about for site in skill.sites):
            return 0
        place = 2
    elif skill.apps:
        named = any(re.search(rf"(?<![a-z]){re.escape(app.lower())}(?![a-z])", about) for app in skill.apps)
        if not named and not skill.implied:
            return 0
        place = 2 if named else 1
    if any(_has(about, word) for word in skill.excludes):
        return 0
    if not all(any(_has(about, word) for word in group) for group in skill.requires):
        return 0
    hits = sum(1 for word in skill.words if _has(about, word))
    if skill.words and not hits:
        return 0
    return place + hits + len(skill.requires)


_PURCHASE = re.compile(
    r"\b(buy|purchase|pay for|pay|check ?out|place (?:an |the |my )?order|"
    r"order (?:the|a|an|some|me|it|them|one|two|three|four|five|\d))\b")
_NEGATION = re.compile(r"(?:don'?t|do not|not|never|without|no need to)\s+(?:\w+\s+)?$")


def wants_to_buy(about: str) -> bool:
    """Does the request ask to buy, pay or order — not "don't buy it yet"?"""
    for match in _PURCHASE.finditer(about.replace("’", "'")):
        if not _NEGATION.search(about[max(0, match.start() - 24):match.start()]):
            return True
    return False


def _has(about: str, word: str) -> bool:
    """*word* at the start of a word in *about* ("remind" finds "reminder")."""
    return re.search(rf"(?<![a-z]){re.escape(word.strip())}", about) is not None


def _domain(site: str, sites: list[str]) -> str:
    """A domain the user named, if it's one of the skill's ("amazon.com")."""
    site = site.strip().lower().removeprefix("https://").removeprefix("http://").split("/")[0]
    if "." not in site or not any(token in site for token in sites):
        return ""
    return site if site.startswith("www.") else f"www.{site}"


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:80] or "skill"


def _yaml_documents(path: Path) -> list[Any]:
    import yaml

    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        log.warning("skills file %s unreadable: %s", path.name, exc)
        return []
    if isinstance(data, dict) and "skills" in data:
        return list(data["skills"] or [])
    return [data] if data else []


def _knowledge_pack(path: Path) -> tuple[list[str], list[str], str]:
    """``sites:``/``apps:`` lines at the top, then the tips."""
    sites: list[str] = []
    apps: list[str] = []
    body: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.lower().startswith("sites:") and not body:
            sites = [s.strip().lower() for s in stripped[6:].split(",") if s.strip()]
        elif stripped.lower().startswith("apps:") and not body:
            apps = [a.strip() for a in stripped[5:].split(",") if a.strip()]
        elif stripped or body:
            body.append(line)
    return sites, apps, "\n".join(body).strip()
