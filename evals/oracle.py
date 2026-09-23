"""The oracle: a deterministic stand-in for a perfectly competent model.

It is given each task's semantic recipe ("search for X", "click the product
called Y", "click Add to Basket") but it must *ground* every step in what
JARVIS actually shows the model — it can only click an element whose handle
appears in the prompt it was sent. So an oracle run measures the
architecture, not a model's IQ: if the oracle can't finish a task, no model
could, because the information needed was never put in front of it.

Progress (did the last action work?) comes from the harness watching real
tool results, not from parsing prose, so the oracle stays independent of how
any particular prompt words its history.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from jarvis.models.base import ModelProvider

#: One observed element, as JARVIS renders it for a model:
#: ``[jv12] button "Add to Basket"`` optionally followed by attributes.
ELEMENT_LINE = re.compile(r'\[(?P<handle>[A-Za-z]*\d+)\]\s+(?P<role>[\w-]+)\s+"(?P<text>[^"\n]*)"(?P<rest>[^\n]*)')

_MARKERS = (
    ("triage", "You decide what the user wants. Two modes only."),
    ("understand", "You work out what the user wants."),
    ("decompose", "Break this into a short sequence of concrete milestones"),
    ("step", "Decide the single next action for one part of a larger task."),
    ("decision", "Decide the single next action towards the objective."),
    ("plan", "Break the objective into the fewest steps"),
    ("recovery", "An action did not achieve the objective."),
    ("classify", "Classify the user's request into exactly one capability."),
)


@dataclass
class Element:
    handle: str
    role: str
    text: str
    rest: str

    def haystack(self) -> str:
        return f"{self.text} {self.rest}".lower()


@dataclass
class OracleBrain:
    recipe: list[dict[str, Any]] = field(default_factory=list)
    cursor: int = 0
    issued: str | None = None
    misses: int = 0
    failures: int = 0
    gave_up: str = ""
    decisions: list[dict[str, Any]] = field(default_factory=list)

    def begin(self, recipe: list[dict[str, Any]]) -> None:
        self.recipe = list(recipe)
        self.cursor = 0
        self.issued = None
        self.misses = self.failures = 0
        self.gave_up = ""
        self.decisions = []

    # -- feedback from the harness ------------------------------------------------
    def on_tool_result(self, tool: str, ok: bool) -> None:
        if tool != self.issued or tool == "read_page_manifest":
            return
        if ok:
            self.cursor += 1
            self.misses = self.failures = 0
        else:
            self.failures += 1

    # -- deciding -------------------------------------------------------------------
    def next_action(self, prompt: str) -> dict[str, Any]:
        decision = self._decide(prompt)
        self.decisions.append(decision)
        self.issued = decision.get("tool") if decision.get("action") == "tool_call" else None
        return decision

    def _decide(self, prompt: str) -> dict[str, Any]:
        if self.gave_up:
            return {"action": "give_up", "reason": self.gave_up}
        if self.cursor >= len(self.recipe):
            return {"action": "complete", "reason": "every step of the task is done"}
        if self.failures >= 2:
            return self._give_up(f"step {self.cursor + 1} kept failing")
        step = self.recipe[self.cursor]
        if "go" in step:
            return _call("browse_to", {"url": step["go"]}, "open the page")
        if "click" in step:
            element = find_element(prompt, step["click"], step.get("role"))
            if element is None:
                return self._look_again(step["click"])
            # ``label`` in a recipe deliberately misdescribes the element —
            # the label-spoofing safety task. Execution addresses the handle.
            label = step.get("label") or element.text or step["click"]
            return _call("click_page_element", {"handle": element.handle, "label": label},
                         f"click {step['click']}")
        if "fill" in step:
            element = find_element(prompt, step["fill"], step.get("role"), fillable=True)
            if element is None:
                return self._look_again(step["fill"])
            return _call("fill_page_field", {"handle": element.handle, "label": element.text or step["fill"],
                                             "text": str(step.get("text", "")),
                                             "submit": bool(step.get("submit", False))},
                         f"type into {step['fill']}")
        if "select" in step:
            element = find_element(prompt, step["select"], "select", fillable=True)
            if element is None:
                return self._look_again(step["select"])
            return _call("fill_page_field", {"handle": element.handle, "label": step["select"],
                                             "text": str(step["option"])},
                         f"choose {step['option']}")
        return self._give_up(f"unknown recipe step {step}")

    def _look_again(self, target: str) -> dict[str, Any]:
        if self.issued == "read_page_manifest":
            self.misses += 1
        if self.misses >= 2:
            return self._give_up(f"“{target}” isn't in anything I've been shown")
        return _call("read_page_manifest", {}, f"look for {target}")

    def _give_up(self, reason: str) -> dict[str, Any]:
        self.gave_up = reason
        return {"action": "give_up", "reason": reason}


def find_element(prompt: str, target: str, role: str | None = None, *,
                 fillable: bool = False) -> Element | None:
    """The most recently shown element best matching *target*."""
    wanted = target.lower().strip()
    best: tuple[int, int, Element] | None = None
    for position, match in enumerate(ELEMENT_LINE.finditer(prompt)):
        element = Element(match["handle"], match["role"].lower(), match["text"], match["rest"])
        if role and element.role != role.lower():
            continue
        if fillable and element.role not in {"field", "select", "textbox", "searchbox", "combobox",
                                              "textarea", "input"}:
            continue
        text = element.text.lower().strip()
        if text == wanted:
            rank = 3
        elif wanted in element.haystack():
            rank = 2
        elif text and text in wanted:
            rank = 1
        else:
            continue
        if best is None or (rank, position) >= (best[0], best[1]):
            best = (rank, position, element)
    return best[2] if best else None


def _call(tool: str, arguments: dict[str, Any], reason: str) -> dict[str, Any]:
    return {"action": "tool_call", "tool": tool, "arguments": arguments, "reason": reason}


class OracleProvider(ModelProvider):
    """Speaks JARVIS's JSON-in-prompt protocol on the oracle's behalf."""

    name = "oracle"
    local = True

    def __init__(self) -> None:
        self.brain = OracleBrain()
        self.calls: list[dict[str, Any]] = []

    async def available(self) -> bool:
        return True

    async def list_models(self) -> list[str]:
        return ["oracle"]

    async def stream_chat(self, messages, model, **kwargs):
        prompt = "\n".join(m.content for m in messages)
        stage, reply = self.respond(prompt, json_mode=bool(kwargs.get("json_mode")))
        self.calls.append({"stage": stage, "prompt_chars": len(prompt)})
        yield reply

    def respond(self, prompt: str, *, json_mode: bool) -> tuple[str, str]:
        stage = next((name for name, marker in _MARKERS if marker in prompt), "prose")
        if stage == "triage":
            text = _user_text(prompt)
            evidence = " ".join(text.lower().split()[:2]) or "do it"
            return stage, json.dumps({
                "mode": "action", "confidence": 0.95, "action_evidence": [evidence],
                "requires_tools": True, "reason": "oracle",
                "objective": {"goal": text, "kind": "automation", "targets": [],
                              "complexity": "multi_step", "confidence": "confident", "missing": []},
            })
        if stage == "understand":
            text = _user_text(prompt)
            return stage, json.dumps({
                "goal": text, "kind": "automation", "targets": [], "constraints": [], "references": [],
                "needs_tools": True, "complexity": "multi_step", "confidence": "confident",
                "refines_previous": False, "is_correction": False, "missing": [],
            })
        if stage == "decompose":
            goal = _between(prompt, "Request:", "\n") or "the task"
            return stage, json.dumps({"milestones": [goal.strip()]})
        if stage in {"step", "decision"}:
            decision = self.brain.next_action(prompt)
            if stage == "decision" and decision["action"] == "give_up":
                decision = {"action": "respond", "content": f"I couldn't finish: {decision['reason']}."}
            return stage, json.dumps(decision)
        if stage == "plan":
            return stage, json.dumps({"steps": [{"intent": "carry out the task", "tool_hint": None}],
                                      "rationale": "oracle"})
        if stage == "recovery":
            return stage, json.dumps({"strategy": "retry", "reason": "try again with what I can see"})
        if stage == "classify":
            return stage, json.dumps({"capability": "conversation", "confidence": 0.5})
        if json_mode:
            return stage, "{}"
        if self.brain.gave_up:
            return stage, f"I couldn't finish that: {self.brain.gave_up}."
        return stage, "Done."


def _user_text(prompt: str) -> str:
    match = re.search(r'User (?:said|just said): "(.*)"', prompt)
    return match.group(1).strip() if match else ""


def _between(text: str, start: str, end: str) -> str:
    index = text.find(start)
    if index < 0:
        return ""
    rest = text[index + len(start):]
    stop = rest.find(end, 1)
    return rest[:stop] if stop >= 0 else rest
