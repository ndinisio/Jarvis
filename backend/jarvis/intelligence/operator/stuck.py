"""Noticing when the operator is going round in circles.

Two signals, both cheap and deterministic:

* **The same action on the same screen, twice.** If the page (or window)
  looks exactly as it did the last time the model made this exact call, the
  call didn't change anything and won't the second time either. The model is
  told so, with the moves that usually get unstuck: look further down, look
  elsewhere on the page, go back, search instead of browsing.
* **Three failures in a row.** The approach isn't working. The next decision
  is made with thinking switched on and an explicit instruction to step back
  and choose a different way — a re-plan, rather than a fourth attempt at the
  same thing.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter

#: Consecutive failed actions that call for a re-plan.
REPLAN_AFTER = 3


def _digest(text: str) -> str:
    return hashlib.sha1((text or "").encode("utf-8", "ignore")).hexdigest()[:16]


def _hint(tool: str) -> str:
    return (f"You've already done exactly this ({tool}) with the screen looking exactly "
            "the same, and it changed nothing. Do something different: scroll_page to see "
            "more, look for another way on the page, page_go_back, or search instead of "
            "browsing.")


class StuckDetector:
    def __init__(self) -> None:
        self._seen: Counter[tuple[str, str]] = Counter()
        self._failures_in_row = 0
        self.replans = 0

    @staticmethod
    def _key(screen: str, tool: str, arguments: dict) -> tuple[str, str]:
        return (_digest(screen), f"{tool}:{json.dumps(arguments, sort_keys=True, default=str)}")

    def already_seen(self, screen: str, tool: str, arguments: dict) -> str | None:
        """Read-only: would this exact action, on this exact screen, be a
        repeat? Checked *before* running the action, so a repeat can be
        refused outright instead of run again and merely complained about
        afterwards — see :meth:`record`, which is what actually counts it."""
        if not screen:
            return None
        return _hint(tool) if self._seen[self._key(screen, tool, arguments)] >= 1 else None

    def record(self, screen: str, tool: str, arguments: dict, ok: bool) -> str | None:
        """Note one action taken while *screen* was showing. Returns a hint
        for the model when the action is a repeat that achieved nothing.

        An empty *screen* means there's no way to tell whether the action
        changed anything (pressing the down arrow twice in an app is
        normal), so only failures are counted."""
        self._failures_in_row = 0 if ok else self._failures_in_row + 1
        if not screen:
            return None
        key = self._key(screen, tool, arguments)
        self._seen[key] += 1
        return _hint(tool) if self._seen[key] >= 2 else None

    @property
    def needs_replan(self) -> bool:
        return self._failures_in_row >= REPLAN_AFTER

    def replanned(self) -> str:
        """Called when a re-plan is issued; returns the instruction for it."""
        self._failures_in_row = 0
        self.replans += 1
        return ("The last few attempts have all failed. Stop and think: what else could get this "
                "done? Choose a genuinely different approach rather than another variation of the "
                "same one.")
