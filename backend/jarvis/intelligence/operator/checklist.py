"""What "done" means, checked against what JARVIS actually saw.

The failure this exists for: a model that has found the right product says
"complete" one step before clicking Add to Basket — the last step is the one
that needed an element handle, and claiming success was easier than finding
it. So finishing is no longer a claim. The task carries a checklist — the
interpreter's success criteria ("a pack of AA batteries is in the Amazon
basket"), or the goal itself when it gave none — and each item has to be
marked done with a quote copied from something JARVIS observed (a result, a
page listing, a dialog). ``finish`` is refused while any item lacks proof,
and the refusal says which one: the model gets told to go and do it.

A quick question answered in the foreground ("what's on my calendar?") with
no explicit criteria isn't held to this: nothing changes state, and the
answer is grounded by the results it's composed from. A multi-step errand
always is.
"""

from __future__ import annotations

from dataclasses import dataclass

from .observation import ObservationLog

#: More than this and a criteria list stops being a checklist.
MAX_CRITERIA = 6


@dataclass
class Criterion:
    text: str
    done: bool = False
    evidence: str = ""


class Checklist:
    def __init__(self, criteria: list[str], *, gated: bool = True, explicit: bool = True):
        self.items = [Criterion(text) for text in criteria if text.strip()][:MAX_CRITERIA]
        #: Whether finishing requires every item proven.
        self.gated = gated and bool(self.items)
        #: Whether the criteria came from the interpreter, or stand in for it.
        self.explicit = explicit

    @classmethod
    def for_objective(cls, objective, goal: str, *, background: bool) -> Checklist:
        explicit = [c.strip() for c in (getattr(objective, "success_criteria", None) or [])
                    if isinstance(c, str) and c.strip()]
        if explicit:
            return cls(explicit, gated=True, explicit=True)
        return cls([goal], gated=background, explicit=False)

    # -- state ------------------------------------------------------------
    def unmet(self) -> list[int]:
        """1-based numbers of the items not yet proven."""
        return [index for index, item in enumerate(self.items, 1) if not item.done]

    def all_done(self) -> bool:
        return not self.unmet()

    def mark(self, item: int, evidence: str, seen: ObservationLog) -> str | None:
        """Mark item *item* done on the strength of *evidence*.

        Returns ``None`` on success, or the problem — phrased for the model,
        since it goes straight back to it as the tool's result.
        """
        if not isinstance(item, int) or not 1 <= item <= len(self.items):
            return f"There is no item {item}; the checklist has items 1 to {len(self.items)}."
        evidence = (evidence or "").strip()
        if not evidence:
            return (f"Item {item} needs proof: a short quote copied exactly from a result or page "
                    "you were shown.")
        if not seen.supports(evidence):
            return (f"Nothing you've been shown says “{evidence[:120]}”. Quote the words exactly as "
                    f"they appeared — or, if item {item} hasn't actually happened yet, do it first.")
        target = self.items[item - 1]
        target.done, target.evidence = True, evidence[:200]
        return None

    def mark_remaining(self, quotes: list[str], seen: ObservationLog) -> list[str]:
        """Apply *quotes* to the unmet items in order; return the problems."""
        problems: list[str] = []
        for item, quote in zip(self.unmet(), quotes):
            problem = self.mark(item, quote, seen)
            if problem:
                problems.append(problem)
        return problems

    # -- rendering --------------------------------------------------------
    def render(self) -> str:
        lines = []
        for index, item in enumerate(self.items, 1):
            if item.done:
                lines.append(f"{index}. [done] {item.text} — shown by “{item.evidence[:80]}”")
            else:
                lines.append(f"{index}. [ ] {item.text}")
        return "\n".join(lines)

    def as_dicts(self) -> list[dict]:
        return [{"text": item.text, "done": item.done, "evidence": item.evidence}
                for item in self.items]

    def report(self) -> tuple[list[str], list[str]]:
        """``(done, not_done)`` item texts, for an honest account."""
        return ([item.text for item in self.items if item.done],
                [item.text for item in self.items if not item.done])
