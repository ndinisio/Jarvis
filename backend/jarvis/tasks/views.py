"""What each task is looking at, live, for its card.

After an action on a page in JARVIS Chrome, the task's card gets a small
picture of the page as it now is — so "adding the kettle to the basket" can
be watched, not just read about. Pictures are rendered small by Chrome itself
(``PlaywrightDriver.picture``), taken at most every ``MIN_INTERVAL_S`` per
task (always including the latest state), only while an interface is
connected, and never kept in the event history. Nothing is captured from the
user's own browser or from app windows.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import time

from ..core.events import EventType
from ..core.logging import get_logger

log = get_logger("jarvis.tasks")

#: Seconds between two pictures of the same task.
MIN_INTERVAL_S = 0.7


class TaskViews:
    def __init__(self, deps):
        self.deps = deps
        self._last: dict[str, float] = {}
        self._scheduled: set[str] = set()

    def attach(self) -> None:
        self.deps.bus.add_hook(self._on_event)

    def _on_event(self, event) -> None:
        if event.type != EventType.TOOL_RESULT:
            return
        task_id = event.payload.get("task_id")
        tool = self.deps.registry.get(str(event.payload.get("tool") or ""))
        if not task_id or tool is None or tool.spec.category != "browser":
            return
        if not self.deps.bus.subscriber_count or task_id in self._scheduled:
            return
        wait = max(0.0, MIN_INTERVAL_S - (time.monotonic() - self._last.get(task_id, 0.0)))
        self._scheduled.add(task_id)
        with contextlib.suppress(RuntimeError):            # no running loop
            asyncio.get_running_loop().create_task(self._capture(task_id, wait))

    async def _capture(self, task_id: str, wait: float) -> None:
        try:
            if wait:
                await asyncio.sleep(wait)
            task = self.deps.tasks.get(task_id)
            if task is None or not task.cancellable:
                return
            data = await self.deps.browsers.picture(task_id, scale=0.3, quality=55)
            self._last[task_id] = time.monotonic()
            if data:
                self.deps.bus.publish(EventType.TASK_VIEW, task_id=task_id,
                                      image="data:image/jpeg;base64," + base64.b64encode(data).decode())
        except Exception as exc:  # pragma: no cover - a picture is a nicety
            log.debug("task view for %s failed: %s", task_id, exc)
        finally:
            self._scheduled.discard(task_id)
