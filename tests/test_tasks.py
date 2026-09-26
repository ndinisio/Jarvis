"""Background tasks: concurrency, progress and cancellation."""

from __future__ import annotations

import asyncio

from jarvis.tasks.manager import TaskManager, TaskStatus


async def test_task_runs_and_reports(app):
    manager = app.tasks

    async def work(task):
        manager.step(task, "half way", 0.5)
        return "finished"

    task = manager.spawn("test", "A test task", work)
    await asyncio.sleep(0.1)
    assert task.status == TaskStatus.SUCCEEDED
    assert task.result == "finished"
    assert task.progress == 1.0
    assert any(step["message"] == "half way" for step in task.steps)


async def test_task_events_are_published(app):
    async def work(task):
        return None

    app.tasks.spawn("test", "Publishing", work)
    await asyncio.sleep(0.1)
    types = [e.type for e in app.bus.history]
    assert "task.created" in types and "task.finished" in types


async def test_cooperative_cancellation(app):
    manager = app.tasks
    observed = {"cancelled": False}

    async def work(task):
        for _ in range(200):
            if task.cancel_event.is_set():
                observed["cancelled"] = True
                return "stopped early"
            await asyncio.sleep(0.01)
        return "ran to completion"

    task = manager.spawn("test", "Long task", work)
    await asyncio.sleep(0.05)
    assert manager.cancel(task.id) is True
    await asyncio.sleep(0.1)
    assert observed["cancelled"] is True
    assert task.status == TaskStatus.CANCELLED


async def test_hard_cancellation_for_unresponsive_work(app):
    async def stubborn(task):
        await asyncio.sleep(30)
        return "never"

    task = app.tasks.spawn("test", "Stubborn", stubborn)
    await asyncio.sleep(0.05)
    app.tasks.cancel(task.id)
    await asyncio.sleep(2.0)
    assert task.status == TaskStatus.CANCELLED


async def test_failure_is_captured_not_raised(app):
    async def broken(task):
        raise ValueError("bad thing")

    task = app.tasks.spawn("test", "Broken", broken)
    await asyncio.sleep(0.1)
    assert task.status == TaskStatus.FAILED
    assert "bad thing" in task.error


async def test_tasks_run_concurrently(app):
    manager = app.tasks
    order: list[str] = []

    async def slow(task):
        await asyncio.sleep(0.12)
        order.append(task.title)
        return task.title

    async def quick(task):
        await asyncio.sleep(0.01)
        order.append(task.title)
        return task.title

    manager.spawn("test", "slow", slow)
    manager.spawn("test", "quick", quick)
    await asyncio.sleep(0.3)
    assert order == ["quick", "slow"]  # the slow task never blocked the quick one


async def test_cancel_latest_targets_the_newest(app):
    manager = app.tasks

    async def work(task):
        await asyncio.sleep(5)

    first = manager.spawn("test", "first", work)
    await asyncio.sleep(0.02)
    second = manager.spawn("test", "second", work)
    await asyncio.sleep(0.02)
    cancelled = manager.cancel_latest()
    assert cancelled is not None and cancelled.id == second.id
    assert first.cancellable
    await manager.shutdown()


async def test_snapshot_is_serialisable(app):
    async def work(task):
        return {"value": 1}

    app.tasks.spawn("test", "Snapshot", work)
    await asyncio.sleep(0.1)
    snapshot = app.tasks.snapshot()
    assert snapshot and snapshot[0]["status"] in {"succeeded", "running"}
    assert "elapsed_s" in snapshot[0]


def test_prune_keeps_recent(app):
    manager: TaskManager = app.tasks
    for index in range(70):
        task = manager.create("test", f"task {index}")
        task.status = TaskStatus.SUCCEEDED
        task.finished = index
    manager.prune(keep=10)
    assert len(manager.all(limit=200)) <= 11


async def test_a_task_is_recorded_as_orphaned_while_it_is_genuinely_still_running(app):
    """A crash, kill or power loss mid-task leaves memory's task_log stuck
    at "running" — the record the next startup uses to notice a task that
    never got the chance to log its own finish. This proves the "running"
    row lands as soon as the task starts, not only once it completes."""
    manager = app.tasks
    started = asyncio.Event()
    finish_now = asyncio.Event()

    async def work(task):
        started.set()
        await finish_now.wait()
        return "done"

    task = manager.spawn("test", "Long reorganisation", work)
    await started.wait()
    await asyncio.sleep(0.05)  # let the background "running" write land
    assert any(row["id"] == task.id for row in app.memory.orphaned_tasks())

    finish_now.set()
    await asyncio.sleep(0.05)
    assert not any(row["id"] == task.id for row in app.memory.orphaned_tasks()), \
        "a normal finish must close out the row, not leave it looking crashed"


async def test_startup_reports_and_clears_a_task_orphaned_by_a_previous_crash(app):
    """The other half, from the next process's point of view: a "running"
    row nobody is running any more (this is a fresh app, nothing has
    spawned a task yet) must be surfaced once at startup, then not nag
    again — there's no safe way to resume it (the conversation, browser
    and app state that run depended on are gone with the old process), so
    reporting it once and closing the record is the honest outcome."""
    import time

    from jarvis.core.events import EventType

    await app.memory.log_task("crashed-1", "research", "Investigate warranty options",
                              "running", time.time(), None, "")
    assert app.memory.orphaned_tasks()

    await app.startup()
    await asyncio.sleep(0.1)

    notices = [e for e in app.bus.history if e.type == EventType.NOTICE]
    assert any("Investigate warranty options" in e.payload.get("message", "") for e in notices)
    assert not app.memory.orphaned_tasks()
    await app.shutdown()
