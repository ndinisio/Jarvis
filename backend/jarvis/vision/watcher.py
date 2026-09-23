"""The background screen watcher.

Constant "screen awareness" without constant vision-model inference: a cheap,
near-free primitive — which application and which window is frontmost — is
polled on a short, fixed interval, and only when that signal actually
changes does the watcher spend a real vision-model call (via
``tools/screen/tools.py: WatchScreenTool``, throttled again on its own,
independent cooldown). See ``core/config.py: ScreenAwarenessConfig``.

Modelled directly on ``voice/manager.py: VoiceManager`` — the established
pattern in this codebase for a self-managed background loop with its own
start()/stop()/reconfigure() lifecycle, guarded against a duplicate loop the
same way ``VoiceManager._start_lock`` is. Deliberately *not* a ``Task``: the
``TaskManager`` status model (PENDING/RUNNING/SUCCEEDED/FAILED/CANCELLED)
assumes completion and prunes finished tasks, which an indefinitely-running
coroutine never does — it would sit un-prunable and permanently occupy one
of a small number of concurrency slots.
"""

from __future__ import annotations

import asyncio
import contextlib
import time

from ..core.config import Config
from ..core.errors import ConfirmationDeclined
from ..core.events import EventType
from ..core.logging import get_logger
from ..core.narration import ActionNarrator
from ..security.permissions import RiskLevel

log = get_logger("jarvis.vision.watcher")


class ScreenWatcher:
    def __init__(self, deps):
        self._deps = deps
        self._narrator = ActionNarrator(deps, conf_attr="screen_awareness")
        self._watch_task: asyncio.Task | None = None
        #: Serialises start() the same way VoiceManager._start_lock does —
        #: the automatic startup call and a live config-flip could otherwise
        #: both pass the "already running" check before either had set
        #: _watch_task, each then starting its own loop.
        self._start_lock = asyncio.Lock()
        self._last_signal: tuple[str, str] | None = None
        self._last_vision_call: float | None = None

    @property
    def running(self) -> bool:
        return self._watch_task is not None and not self._watch_task.done()

    @staticmethod
    def _should_run(config: Config) -> bool:
        return bool(
            config.capabilities.screen_awareness
            and config.capabilities.screen
            and config.security.allow_screen_capture
        )

    # ------------------------------------------------------------------
    async def start(self) -> bool:
        if not self._should_run(self._deps.config):
            return False
        async with self._start_lock:
            if self._watch_task and not self._watch_task.done():
                return True
            if not await self._consent():
                return False
            granted, note = await self._deps.controller.check_permission("screen_recording")
            if not granted:
                log.info("screen watcher not starting: %s", note)
                self._deps.bus.publish(
                    EventType.NOTICE, level="warning",
                    message="Screen watching needs Screen Recording permission. Grant it in "
                            "System Settings → Privacy & Security, then try again.",
                )
                return False
            # Config may have changed while the two awaits above were in
            # flight — _consent() alone can wait up to
            # security.confirmation_timeout_s (90s by default) for a live
            # answer. Re-checking now, still under the lock, is what stops a
            # start() that's no longer wanted from creating the loop anyway.
            if not self._should_run(self._deps.config):
                return False
            self._watch_task = asyncio.create_task(self._watch_loop(), name="jarvis-screen-watch")
            return True

    async def stop(self) -> None:
        # A start() may be holding the lock while it waits on the consent
        # prompt; withdraw that question first, or turning the watcher off
        # would wait out the whole confirmation timeout.
        self._deps.permissions.withdraw("screen_awareness:start", reason="screen awareness turned off")
        # Shares start()'s lock so a stop() racing a start() that's mid-way
        # through consent/permission checks is properly ordered rather than
        # each mutating self._watch_task independently.
        async with self._start_lock:
            if self._watch_task:
                self._watch_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await self._watch_task
                self._watch_task = None

    async def restart(self) -> bool:
        await self.stop()
        return await self.start()

    def reconfigure(self, config: Config) -> None:
        """React to a live config change — start/stop as the flag flips.

        Everything else (poll cadence, cooldowns, narration) is read fresh
        from ``self._deps.config`` every iteration, so no restart is needed
        for those to take effect.
        """
        should_run = self._should_run(config)
        if should_run and not self.running:
            asyncio.create_task(self.start())
        elif not should_run and self.running:
            asyncio.create_task(self.stop())

    # ------------------------------------------------------------------
    async def _consent(self) -> bool:
        # No local cache here — permissions.require() already has its own
        # session-grant fast path (security/permissions.py), which is the
        # single source of truth a revoked grant actually reaches. A local
        # "already consented" flag would drift from that the moment
        # anything revokes the grant out from under it.
        conf = self._deps.config.screen_awareness
        summary = (
            "I'll start watching your screen in the background — a cheap check of which "
            f"app and window is frontmost roughly every {conf.poll_interval_s:g} seconds, "
            "and only when that changes will I actually look at the screen. What I see stays "
            "with me unless you ask" + (", or say something out loud." if conf.narrate else ".")
        )
        try:
            await self._deps.permissions.require(
                action="screen_awareness:start",
                risk=RiskLevel.MEDIUM,
                summary=summary,
                # A privacy consent, not a routine step: however much
                # autonomy JARVIS has, it always asks this.
                consent=True,
                details={"poll_interval_s": conf.poll_interval_s,
                         "min_vision_interval_s": conf.min_vision_interval_s},
            )
        except ConfirmationDeclined:
            return False
        return True

    async def _watch_loop(self) -> None:
        try:
            while True:
                await self._poll_once()
                await asyncio.sleep(self._deps.config.screen_awareness.poll_interval_s)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # pragma: no cover - last-resort guard
            log.exception("screen watcher loop failed")
            self._deps.bus.publish(EventType.ERROR, message="The screen watcher stopped unexpectedly.",
                                   detail=str(exc))

    async def _poll_once(self) -> None:
        try:
            app = await self._deps.controller.frontmost_app()
            window = await self._deps.controller.frontmost_window_id()
        except Exception as exc:
            log.debug("screen watcher poll failed: %s", exc)
            return
        signal = (app, window)
        if not app or signal == self._last_signal:
            return
        self._last_signal = signal

        now = time.monotonic()
        if (self._last_vision_call is not None
                and now - self._last_vision_call < self._deps.config.screen_awareness.min_vision_interval_s):
            return
        self._last_vision_call = now
        await self._capture()

    async def _capture(self) -> None:
        ctx = self._deps.tool_context()
        try:
            result = await self._deps.registry.call("watch_screen", {}, ctx)
        except Exception as exc:  # pragma: no cover - defensive, mirrors _poll_once
            log.debug("screen watcher capture failed: %s", exc)
            return
        if result.ok and self._deps.config.screen_awareness.narrate:
            self._narrator.phase(result.summary)
