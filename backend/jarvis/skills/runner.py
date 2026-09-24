"""Running a skill: its steps, grounded in what's on screen, one tool call each.

Every step goes through the tool registry — the same validation, permission
gate, consequence check and confirmation as a model's own call — so a skill
can't do anything the operator couldn't. What it saves is the thinking:
a recipe that knows Amazon's search URL and its "Add to Basket" button takes
no model calls at all.

When a step can't be grounded ("there's no Add to Basket button — this
product needs a size first"), the run stops and says exactly how far it got;
the operator carries on from there with the page in front of it.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ..core.logging import get_logger
from . import grounding
from .model import Skill, SkillError, kind_of, render

log = get_logger("jarvis.skills")

#: Scrolls a ``scroll_until`` step makes before giving up.
MAX_SCROLLS = 6


@dataclass
class SkillOutcome:
    ok: bool
    #: What was done, step by step, in words.
    done: list[str] = field(default_factory=list)
    #: Why it stopped, when it didn't finish.
    reason: str = ""
    declined: bool = False
    #: The latest page or window listing.
    view: str = ""
    #: Everything shown along the way — proof for whoever carries on.
    observations: list[str] = field(default_factory=list)
    #: The ``done_when`` phrase that showed, when it did.
    evidence: str = ""
    actions: int = 0
    #: The actions as the operator records them (tool, arguments, ok).
    trail: list[tuple[str, dict, bool]] = field(default_factory=list)

    def account(self) -> str:
        """How far it got, for the operator's brief or the user."""
        done = "; ".join(self.done) if self.done else "nothing yet"
        return f"Done so far: {done}." + (f" Stopped because {self.reason}." if self.reason else "")


class SkillRunner:
    def __init__(self, deps, ctx, *, report: Callable[[str], None] | None = None):
        self.deps = deps
        self.registry = deps.registry
        self.ctx = ctx
        self.report = report

    async def run(self, skill: Skill, params: dict[str, str], *, view: str = "") -> SkillOutcome:
        outcome = SkillOutcome(ok=False, view=view)
        web = skill.surface != "native"
        for index, step in enumerate(skill.steps, 1):
            self.ctx.raise_if_cancelled()
            kind = kind_of(step)
            optional = bool(step.get("optional"))
            try:
                spec = render(step[kind], params)
                extra = {key: render(value, params) for key, value in step.items() if key != kind}
            except SkillError as exc:
                outcome.reason = f"step {index} needs {exc}"
                return outcome
            try:
                progressed = await self._step(kind, spec, extra, web, outcome)
            except _Stop as stop:
                if optional and not stop.declined:
                    continue
                outcome.reason = stop.reason
                outcome.declined = stop.declined
                return outcome
            if progressed and self.report:
                self.report(progressed)
        if skill.done_when:
            outcome.evidence = grounding.mentions(outcome.view, skill.done_when) or next(
                (phrase for text in reversed(outcome.observations)
                 for phrase in [grounding.mentions(text, skill.done_when)] if phrase), "")
            if not outcome.evidence:
                outcome.reason = f"the steps ran but nothing showed “{skill.done_when[0]}”"
                return outcome
        outcome.ok = True
        return outcome

    # ------------------------------------------------------------------
    async def _step(self, kind: str, spec: Any, extra: dict, web: bool, outcome: SkillOutcome) -> str:
        if kind == "go":
            await self._call("browse_to", {"url": str(spec)}, outcome, f"opened {spec}")
            await self._look(web, outcome)
            return f"Opened {spec}"
        if kind == "open":
            await self._call("open_url", {"url": str(spec)}, outcome, f"opened {spec}")
            return f"Opened {spec}"
        if kind == "app":
            await self._call("open_application", {"name": str(spec)}, outcome, f"opened {spec}")
            await self._look(False, outcome, app=str(spec))
            return f"Opened {spec}"
        if kind in {"click", "fill"}:
            target = await self._ground(spec, web, outcome, fillable=(kind == "fill"))
            if kind == "click":
                tool = "click_page_element" if web else "click_control"
                await self._call(tool, {"handle": target.handle, "label": target.text}, outcome,
                                 f"clicked {target.describe()}")
                await self._look(web, outcome)
                return f"Clicked “{target.text}”"
            text = str(extra.get("with") or "")
            submit = bool(extra.get("submit"))
            if web:
                arguments = {"handle": target.handle, "label": target.text, "text": text, "submit": submit}
                await self._call("fill_page_field", arguments, outcome, f"typed “{text}” into {target.describe()}")
            else:
                await self._call("type_into", {"handle": target.handle, "text": text, "submit": submit},
                                 outcome, f"typed “{text}” into {target.describe()}")
            await self._look(web, outcome)
            return f"Typed “{text}”"
        if kind == "key":
            await self._call("press_page_key" if web else "press_key", {"key": str(spec)}, outcome,
                             f"pressed {spec}")
            await self._look(web, outcome)
            return f"Pressed {spec}"
        if kind == "menu":
            path = [str(p) for p in (spec if isinstance(spec, list) else [spec])]
            await self._call("choose_menu_item", {"path": path}, outcome, "chose " + " › ".join(path))
            await self._look(False, outcome)
            return "Chose " + " › ".join(path)
        if kind == "type":
            await self._call("type_text", {"text": str(spec)}, outcome, f"typed “{spec}”")
            return f"Typed “{spec}”"
        if kind == "wait":
            if web:
                await self._call("wait_for_page", {"text": str(spec), "timeout_s": 10}, outcome,
                                 f"waited for “{spec}”")
            else:
                await self._call("wait_for_element", {"label": str(spec), "timeout_s": 10}, outcome,
                                 f"waited for “{spec}”")
            await self._look(web, outcome)
            return ""
        if kind == "scroll_until":
            wanted = spec if isinstance(spec, dict) else {"text": str(spec)}
            limit = int(extra.get("max") or MAX_SCROLLS)
            for _ in range(limit):
                if _find(outcome.view, wanted) is not None:
                    return ""
                await self._call("scroll_page" if web else "scroll", {"direction": "down"}, outcome,
                                 "scrolled down")
                await self._look(web, outcome)
            if _find(outcome.view, wanted) is None:
                raise _Stop(f"{_describe(wanted)} never appeared, even after scrolling")
            return ""
        if kind == "expect":
            phrases = [str(p) for p in (spec if isinstance(spec, list) else [spec])]
            if not grounding.mentions(outcome.view, phrases):
                raise _Stop(f"the page didn't show “{phrases[0]}”")
            return ""
        if kind == "tool":
            name = str(spec)
            arguments = extra.get("args") or {}
            await self._call(name, dict(arguments), outcome, f"ran {name}")
            return f"Ran {name.replace('_', ' ')}"
        raise _Stop(f"it doesn't know how to “{kind}”")    # pragma: no cover - load() prevents it

    async def _call(self, tool: str, arguments: dict[str, Any], outcome: SkillOutcome, done: str) -> Any:
        if self.registry.get(tool) is None:
            raise _Stop(f"{tool} isn't available here")
        result = await self.registry.call(tool, arguments, self.ctx)
        outcome.actions += 1
        outcome.trail.append((tool, dict(arguments), bool(result.ok)))
        if not result.ok:
            declined = (result.error or "").startswith("confirmation_declined")
            raise _Stop(result.summary or f"{tool} didn't work", declined=declined)
        text = result.observation or result.summary
        if text:
            outcome.observations.append(text)
        outcome.done.append(done)
        return result

    async def _look(self, web: bool, outcome: SkillOutcome, app: str = "") -> None:
        tool = "read_page_manifest" if web else "read_window"
        if self.registry.get(tool) is None:
            return
        arguments = {"app": app} if app and not web else {}
        looked = await self.registry.call(tool, arguments, self.ctx)
        if looked.ok and looked.observation:
            outcome.view = looked.observation
            outcome.observations.append(looked.observation)

    async def _ground(self, spec: Any, web: bool, outcome: SkillOutcome, *, fillable: bool):
        wanted = spec if isinstance(spec, dict) else {"text": str(spec)}
        found = _find(outcome.view, wanted, fillable=fillable)
        if found is None:
            await self._look(web, outcome)           # the page may have moved on
            found = _find(outcome.view, wanted, fillable=fillable)
        if found is None:
            raise _Stop(f"there's no {_describe(wanted)} on screen")
        return found


class _Stop(Exception):
    def __init__(self, reason: str, *, declined: bool = False):
        super().__init__(reason)
        self.reason = reason
        self.declined = declined


def _find(view: str, wanted: dict[str, Any], *, fillable: bool = False):
    return grounding.find(view, text=wanted.get("text") or "", role=wanted.get("role") or "",
                          href=str(wanted.get("href") or ""),
                          best_match=str(wanted.get("best_match") or ""),
                          fillable=fillable or bool(wanted.get("fillable")))


def _describe(wanted: dict[str, Any]) -> str:
    text = wanted.get("text")
    if isinstance(text, list):
        text = " / ".join(str(t) for t in text)
    role = wanted.get("role") or ("result" if wanted.get("best_match") else "control")
    if isinstance(role, list):
        role = role[0]
    if wanted.get("best_match"):
        return f"{role} matching “{wanted['best_match']}”"
    return f"{role} “{text}”" if text else str(role)


def summary_for(skill: Skill, params: dict[str, str], outcome: SkillOutcome) -> str:
    """What to tell the user when a skill finished."""
    if skill.summary:
        try:
            return render(skill.summary, params)
        except SkillError:
            pass
    if outcome.evidence:
        return f"Done — the page shows “{outcome.evidence}”."
    return "Done."

