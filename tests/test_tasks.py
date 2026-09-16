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
