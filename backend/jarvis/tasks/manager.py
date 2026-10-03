"""Background task management.

Long work (research, diagnostics, mail sweeps) runs here so the conversation
never blocks. Each task carries an id, a live status, progress, a step trail
and a cancellation token; "stop that" reaches the running coroutine through the
same token the tools poll.
"""

from __future__ import annotations

import asyncio
import functools
import json
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from ..core.events import EventBus, EventType
from ..core.logging import get_logger
from ..tools.registry import redact_arguments

log = get_logger("jarvis.tasks")

#: Longest step summary written to disk — the audit log's own limit.
STEP_SUMMARY_CHARS = 240
#: Checklist items a step's record names as proven, and how long each is kept.
STEP_PROVEN_ITEMS = 20
STEP_PROVEN_CHARS = 120
#: What a step entry holds that has a column of its own (or is handled
#: separately); the rest is the "arguments" column.
_STEP_COLUMNS = frozenset({"message", "ts", "tool", "ok", "checklist"})
_STEP_ARGS_CHARS = 2000


class TaskStatus:
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"

    TERMINAL = {SUCCEEDED, FAILED, CANCELLED}


def _running() -> asyncio.Event:
    event = asyncio.Event()
    event.set()
    return event


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
    #: Set while the task may run; cleared while it's paused (the user
    #: paused it, or took over to do something themselves).
    resume_event: asyncio.Event = field(default_factory=_running)
    #: "" when running, else why it's paused: "paused" or "taken over".
    paused: str = ""
    #: Steps reported so far — the sequence number of the next durable
    #: checkpoint, which ``steps`` can't give (the UI view is trimmed).
    step_count: int = 0
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
            "paused": self.paused,
            "result": _summarise(self.result),
        }


class TaskManager:
    def __init__(self, bus: EventBus, memory=None, max_concurrent: int = 4):
        self._bus = bus
        self._memory = memory
        self._tasks: dict[str, Task] = {}
        self._semaphore = asyncio.Semaphore(max_concurrent)
        #: Memory writes in flight. Held so none is garbage-collected
        #: half-done, and so a clean shutdown can wait for them.
        self._writes: set[asyncio.Task] = set()

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
            if self._memory is not None:
                # Written now, not just at the end: this is the row a crash,
                # kill or power loss leaves behind at "running" — the record
                # that lets the next startup notice a task that never got
                # the chance to log its own finish (see
                # MemoryStore.orphaned_tasks).
                self._write(self._memory.log_task, task.id, task.kind, task.title, TaskStatus.RUNNING,
                            task.started, None, "")
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
            self._write(self._memory.log_task, task.id, task.kind, task.title, status, task.started,
                        task.finished, str(_summarise(task.result) or task.error or ""))
            # Nothing left to reconcile for a task that finished one way or
            # another: its step checkpoints only matter if a crash strands it.
            self._write(self._memory.discard_task_steps, [task.id])

    # -- progress ----------------------------------------------------------
    def step(self, task: Task, message: str, progress: float | None = None,
             **meta: Any) -> None:
        entry = {"message": message, "ts": time.time(), **meta}
        task.steps.append(entry)
        task.step_count += 1
        if progress is not None:
            task.progress = max(0.0, min(1.0, progress))
        self._publish_update(task, step=entry)
        self._checkpoint(task, entry)

    def _checkpoint(self, task: Task, entry: dict[str, Any]) -> None:
        """Write a step through to memory as it happens, so a crash leaves a
        record of how far the task got — to be reconciled against the real
        machine at the next start (tasks/reconcile.py), never replayed.

        Only while the task is running: that is when the "running" row that
        lets startup find a stranded task exists (see _run). The arguments
        go through the same redaction as the audit log's."""
        if self._memory is None or task.status != TaskStatus.RUNNING:
            return
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return                      # not on the event loop: nowhere to write from
        meta = {k: v for k, v in entry.items() if k not in _STEP_COLUMNS}
        checklist = entry.get("checklist")
        proven = [str(item.get("text", ""))[:STEP_PROVEN_CHARS] for item in checklist or []
                  if isinstance(item, dict) and item.get("done")]
        if proven:
            meta["proven"] = proven[:STEP_PROVEN_ITEMS]
        args = json.dumps(redact_arguments(meta), default=str)
        if len(args) > _STEP_ARGS_CHARS:
            args = json.dumps({"truncated": True})
        ok = entry.get("ok")
        self._write(self._memory.log_task_step, task.id, task.step_count, entry["ts"],
                    str(entry.get("tool") or ""), args, None if ok is None else bool(ok),
                    str(entry["message"])[:STEP_SUMMARY_CHARS])

    def _write(self, write_fn: Callable[..., Awaitable[Any]], *args: Any) -> None:
        """Run a memory write in the background without letting a failed
        write take a task down — and without losing track of it.

        Given the function and its arguments (taken now), not a coroutine made
        from them: the coroutine is created only when the write starts. A write
        cancelled before it ever ran — the loop closing right after a task
        finished — then leaves nothing behind, instead of a coroutine that
        was made and never awaited."""
        write = asyncio.create_task(self._guarded(functools.partial(write_fn, *args)))
        self._writes.add(write)
        write.add_done_callback(self._writes.discard)

    async def flush(self) -> None:
        """Wait for the memory writes in flight — a clean shutdown's last
        chance to leave the record complete."""
        while self._writes:
            await asyncio.gather(*list(self._writes), return_exceptions=True)

    @staticmethod
    async def _guarded(start: Callable[[], Awaitable[Any]]) -> None:
        try:
            await start()
        except Exception as exc:
            log.warning("task memory write failed: %s", exc)

    def _publish_update(self, task: Task, step: dict | None = None) -> None:
        payload = task.as_dict()
        if step:
            payload["step"] = step
        self._bus.publish(EventType.TASK_UPDATED, **payload)

    # -- control -----------------------------------------------------------
    def pause(self, task_id: str, *, reason: str = "paused") -> bool:
        """Hold the task before its next step — the step in flight finishes."""
        task = self._tasks.get(task_id)
        if task is None or task.status not in {TaskStatus.PENDING, TaskStatus.RUNNING}:
            return False
        task.paused = reason
        task.resume_event.clear()
        self.step(task, "Over to you — say “carry on” when you're done." if reason == "taken over"
                  else "Paused.")
        return True

    def resume(self, task_id: str) -> bool:
        task = self._tasks.get(task_id)
        if task is None or not task.paused:
            return False
        task.paused = ""
        task.resume_event.set()
        self.step(task, "Carrying on.")
        return True

    def latest(self, *, paused: bool | None = None) -> Task | None:
        """The newest unfinished task (only paused ones, or only running ones)."""
        active = [t for t in self._tasks.values() if t.cancellable
                  and (paused is None or bool(t.paused) == paused)]
        return max(active, key=lambda t: t.started) if active else None

    def cancel(self, task_id: str) -> bool:
        task = self._tasks.get(task_id)
        if task is None or not task.cancellable:
            return False
        task.cancel_event.set()
        task.paused = ""
        task.resume_event.set()              # a paused task wakes to see it's stopped
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
        await self.flush()


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
