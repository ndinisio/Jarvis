"""Background task management.

Long work (research, diagnostics, mail sweeps) runs here so the conversation
never blocks. Each task carries an id, a live status, progress, a step trail
and a cancellation token; "stop that" reaches the running coroutine through the
same token the tools poll.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from ..core.events import EventBus, EventType
from ..core.logging import get_logger

log = get_logger("jarvis.tasks")


class TaskStatus:
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"

    TERMINAL = {SUCCEEDED, FAILED, CANCELLED}


@dataclass
class Task:
    id: str
    kind: str
    title: str
    status: str = TaskStatus.PENDING
    progress: float = 0.0
    steps: list[dict[str, Any]] = field(default_factory=list)
    started: float = field(default_factory=time.time)
    finished: float | None = None
    result: Any = None
    error: str | None = None
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)
    _runner: asyncio.Task | None = field(default=None, repr=False)

    @property
    def elapsed(self) -> float:
        return (self.finished or time.time()) - self.started

    @property
    def cancellable(self) -> bool:
        return self.status in {TaskStatus.PENDING, TaskStatus.RUNNING}

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "title": self.title,
            "status": self.status,
            "progress": round(self.progress, 3),
            "steps": self.steps[-12:],
            "started": self.started,
            "finished": self.finished,
            "elapsed_s": round(self.elapsed, 2),
            "error": self.error,
            "cancellable": self.cancellable,
            "result": _summarise(self.result),
        }


class TaskManager:
    def __init__(self, bus: EventBus, memory=None, max_concurrent: int = 4):
        self._bus = bus
        self._memory = memory
        self._tasks: dict[str, Task] = {}
        self._semaphore = asyncio.Semaphore(max_concurrent)

    # -- lifecycle ---------------------------------------------------------
    def create(self, kind: str, title: str) -> Task:
        task = Task(id=uuid.uuid4().hex[:10], kind=kind, title=title)
        self._tasks[task.id] = task
        self._bus.publish(EventType.TASK_CREATED, **task.as_dict())
        return task

    def spawn(self, kind: str, title: str,
              coro_factory: Callable[[Task], Awaitable[Any]]) -> Task:
        """Create a task and start running it immediately."""
        task = self.create(kind, title)
        task._runner = asyncio.create_task(self._run(task, coro_factory), name=f"jarvis-{kind}")
        return task

    async def _run(self, task: Task, coro_factory: Callable[[Task], Awaitable[Any]]) -> None:
        async with self._semaphore:
            if task.cancel_event.is_set():
                self._finish(task, TaskStatus.CANCELLED)
                return
            task.status = TaskStatus.RUNNING
            self._publish_update(task)
            try:
                task.result = await coro_factory(task)
                status = TaskStatus.CANCELLED if task.cancel_event.is_set() else TaskStatus.SUCCEEDED
                self._finish(task, status)
            except asyncio.CancelledError:
                self._finish(task, TaskStatus.CANCELLED)
                raise
            except Exception as exc:
                log.exception("task %s (%s) failed", task.id, task.kind)
                task.error = f"{type(exc).__name__}: {exc}"
                self._finish(task, TaskStatus.FAILED)

    def _finish(self, task: Task, status: str) -> None:
        task.status = status
        task.finished = time.time()
        task.progress = 1.0 if status == TaskStatus.SUCCEEDED else task.progress
        self._bus.publish(EventType.TASK_FINISHED, **task.as_dict())
        if self._memory is not None:
            asyncio.create_task(
                self._memory.log_task(
                    task.id, task.kind, task.title, status, task.started, task.finished,
                    str(_summarise(task.result) or task.error or ""),
                )
            )

    # -- progress ----------------------------------------------------------
    def step(self, task: Task, message: str, progress: float | None = None,
             **meta: Any) -> None:
        entry = {"message": message, "ts": time.time(), **meta}
        task.steps.append(entry)
        if progress is not None:
            task.progress = max(0.0, min(1.0, progress))
        self._publish_update(task, step=entry)

    def _publish_update(self, task: Task, step: dict | None = None) -> None:
        payload = task.as_dict()
        if step:
            payload["step"] = step
        self._bus.publish(EventType.TASK_UPDATED, **payload)

    # -- control -----------------------------------------------------------
    def cancel(self, task_id: str) -> bool:
        task = self._tasks.get(task_id)
        if task is None or not task.cancellable:
            return False
        task.cancel_event.set()
        self.step(task, "Cancelling…")
        if task._runner is not None:
            # Give the coroutine a moment to notice the flag, then force it.
            asyncio.get_event_loop().call_later(1.5, _force_cancel, task)
        return True

    def cancel_all(self) -> int:
        return sum(1 for task in list(self._tasks.values()) if self.cancel(task.id))

    def cancel_latest(self) -> Task | None:
        active = [t for t in self._tasks.values() if t.cancellable]
        if not active:
            return None
        newest = max(active, key=lambda t: t.started)
        self.cancel(newest.id)
        return newest

    # -- inspection --------------------------------------------------------
    def get(self, task_id: str) -> Task | None:
        return self._tasks.get(task_id)

    def active(self) -> list[Task]:
        return [t for t in self._tasks.values() if t.status == TaskStatus.RUNNING]

    def all(self, limit: int = 40) -> list[Task]:
        return sorted(self._tasks.values(), key=lambda t: t.started, reverse=True)[:limit]

    def snapshot(self) -> list[dict[str, Any]]:
        return [t.as_dict() for t in self.all()]

    def prune(self, keep: int = 60) -> None:
        finished = [t for t in self._tasks.values() if t.status in TaskStatus.TERMINAL]
        for task in sorted(finished, key=lambda t: t.finished or 0)[: max(0, len(finished) - keep)]:
            self._tasks.pop(task.id, None)

    async def shutdown(self) -> None:
        self.cancel_all()
        runners = [t._runner for t in self._tasks.values() if t._runner and not t._runner.done()]
        for runner in runners:
            runner.cancel()
        if runners:
            await asyncio.gather(*runners, return_exceptions=True)


def _force_cancel(task: Task) -> None:
    if task._runner is not None and not task._runner.done():
        task._runner.cancel()


def _summarise(result: Any) -> Any:
    if result is None:
        return None
    if isinstance(result, str):
        return result[:2000]
    if isinstance(result, dict):
        return {k: _summarise(v) for k, v in list(result.items())[:20]}
    if isinstance(result, list):
        return [_summarise(v) for v in result[:20]]
    if isinstance(result, (int, float, bool)):
        return result
    return str(result)[:500]
