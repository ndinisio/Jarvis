"""Speaking through long-running work.

A quiet multi-step task feels slower than it is — the user asked for
something and then heard nothing for half a minute. :class:`ActionNarrator`
turns the same per-step activity text a background task already produces
(see ``tasks/manager.py: TaskManager.step``) into short spoken lines,
reusing the existing speech queue (``voice/manager.py: VoiceManager.enqueue``)
rather than building a second pipeline. The background screen watcher
(``vision/watcher.py``) reuses it too, for its own optional narration —
see ``conf_attr`` below.

Two triggers, both throttled by one shared minimum gap so a slow step right
after an announcement never talks over it:

* :meth:`phase` — a moment worth announcing (an errand's checklist item
  proven). Always spoken, subject only to the throttle — the landmark lines
  ("A pack of AA batteries is in the basket — done.") a long task should
  always surface.
* :meth:`maybe_narrate` — one tool call. Spoken only when that call is, or
  was, actually slow — a fast, routine step (a manifest read, a sub-second
  click) stays silent, which is the deliberate anti-spam rule.
"""

from __future__ import annotations

import time

from .logging import get_logger

log = get_logger("jarvis.narration")


class ActionNarrator:
    def __init__(self, deps, *, conf_attr: str = "automation"):
        """*conf_attr* names the ``Config`` field holding this narrator's own
        ``narration_min_gap_s`` (and, for :meth:`maybe_narrate`,
        ``narration_action_threshold_s``) — ``AutomationConfig`` by default,
        so every existing call site is unaffected."""
        self._deps = deps
        self._conf_attr = conf_attr
        self._last_spoken = 0.0

    @property
    def _conf(self):
        return getattr(self._deps.config, self._conf_attr)

    def _voice(self):
        voice = getattr(self._deps, "voice", None)
        if voice is None or not self._deps.config.voice.enabled:
            return None
        return voice

    def phase(self, message: str) -> bool:
        """A moment worth announcing — always spoken, subject to the throttle."""
        return self._speak(message)

    def maybe_narrate(self, message: str, *, expected_ms: int = 0, elapsed_ms: float = 0.0) -> bool:
        """One tool call's step — spoken only if it is, or was, slow enough
        to be worth reassuring the user about."""
        threshold_ms = self._conf.narration_action_threshold_s * 1000.0
        if max(expected_ms, elapsed_ms) < threshold_ms:
            return False
        return self._speak(message)

    # -- shared throttle -----------------------------------------------------
    def _speak(self, message: str) -> bool:
        voice = self._voice()
        if voice is None or not message:
            return False
        now = time.monotonic()
        if now - self._last_spoken < self._conf.narration_min_gap_s:
            return False
        self._last_spoken = now
        from .personality import speakable

        try:
            voice.enqueue(speakable(message))
        except Exception:  # pragma: no cover - narration must never break the task
            log.debug("narration failed for: %s", message[:80])
            return False
        return True
