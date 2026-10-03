"""Background tasks: concurrency, progress and cancellation."""

from __future__ import annotations

import asyncio
import json

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


# ---------------------------------------------------------------------------
# durable step checkpoints (reconcile, never replay)
# ---------------------------------------------------------------------------
# Simulated: these exercise JARVIS's own SQLite and task bookkeeping. A real
# crash and relaunch of the app on a Mac is not simulated here.
async def _running_task(manager, body):
    """Spawn a task, run *body(task)* inside it, and hold it open at the end."""
    release = asyncio.Event()
    reached = asyncio.Event()

    async def work(task):
        body(task)
        reached.set()
        await release.wait()
        return "done"

    task = manager.spawn("test", "Reorganise the desktop", work)
    await reached.wait()
    await manager.flush()
    return task, release


async def test_each_step_of_a_running_task_is_checkpointed_as_it_happens(app):
    manager = app.tasks

    def body(task):
        manager.step(task, "Opened Finder.", tool="open_application", ok=True)
        manager.step(task, "Pressed “Save”.", tool="click_control", ok=True,
                     checklist=[{"text": "file saved", "done": True},
                                {"text": "folder tidy", "done": False}])
        manager.step(task, "Carrying on.")

    task, release = await _running_task(manager, body)
    rows = app.memory.task_steps(task.id)
    assert [r["seq"] for r in rows] == [1, 2, 3]
    assert (rows[0]["tool"], rows[0]["ok"], rows[0]["summary"]) == ("open_application", 1, "Opened Finder.")
    assert json.loads(rows[1]["args_redacted"]) == {"proven": ["file saved"]}
    assert (rows[2]["tool"], rows[2]["ok"]) == ("", None), "a step with no tool result has no verdict"
    release.set()


async def test_step_arguments_go_through_the_same_redaction_as_the_audit_log(app):
    manager = app.tasks

    def body(task):
        manager.step(task, "Signed in.", tool="fill_page_field", ok=True,
                     password="hunter2", token="abc123", phase="login")

    task, release = await _running_task(manager, body)
    stored = app.memory.task_steps(task.id)[0]["args_redacted"]
    assert "hunter2" not in stored and "abc123" not in stored
    assert json.loads(stored) == {"password": "••••", "token": "••••", "phase": "login"}
    release.set()


async def test_the_step_record_is_a_ring_not_an_ever_growing_log(app, monkeypatch):
    from jarvis.memory import store

    monkeypatch.setattr(store, "TASK_STEPS_KEPT", 5)
    manager = app.tasks

    def body(task):
        for i in range(12):
            manager.step(task, f"step {i}", tool="press_key", ok=True)

    task, release = await _running_task(manager, body)
    assert [r["seq"] for r in app.memory.task_steps(task.id)] == [8, 9, 10, 11, 12]
    release.set()


async def test_a_task_that_finishes_leaves_no_checkpoints_behind(app):
    manager = app.tasks

    def body(task):
        manager.step(task, "Opened Finder.", tool="open_application", ok=True)

    task, release = await _running_task(manager, body)
    assert app.memory.task_steps(task.id)
    release.set()
    await task._runner
    await manager.flush()
    assert app.memory.task_steps(task.id) == []
    assert not any(row["id"] == task.id for row in app.memory.orphaned_tasks())


async def test_a_task_that_fails_leaves_no_checkpoints_behind_either(app):
    manager = app.tasks

    async def work(task):
        manager.step(task, "Opened Finder.", tool="open_application", ok=True)
        raise RuntimeError("boom")

    task = manager.spawn("test", "Doomed", work)
    await task._runner
    await manager.flush()
    assert task.status == TaskStatus.FAILED
    assert app.memory.task_steps(task.id) == []


async def test_steps_outside_a_running_task_are_not_checkpointed(app):
    """The step rows belong to the "running" row startup looks for: a task
    not yet running (or already finished) has neither."""
    manager = app.tasks
    task = manager.create("test", "Not started")
    manager.step(task, "Queued.", tool="press_key", ok=True)
    await manager.flush()
    assert app.memory.task_steps(task.id) == []
    assert task.step_count == 1, "it is still counted for the UI"


async def test_a_failed_checkpoint_write_never_takes_the_task_down(app, monkeypatch, caplog):
    """The task carries on — and the lost checkpoint is logged, not silently
    dropped, since a gap in the record is worth knowing about."""
    import logging

    async def broken(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(app.memory, "log_task_step", broken)
    manager = app.tasks

    async def work(task):
        manager.step(task, "Opened Finder.", tool="open_application", ok=True)
        return "still fine"

    with caplog.at_level(logging.WARNING, logger="jarvis.tasks"):
        task = manager.spawn("test", "Resilient", work)
        await task._runner
        await manager.flush()
    assert task.status == TaskStatus.SUCCEEDED and task.result == "still fine"
    assert any("task memory write failed" in r.getMessage() and "disk full" in r.getMessage()
               for r in caplog.records)


async def test_checkpoints_survive_a_restart_of_the_store(tmp_path):
    """The SQLite layer: reopening the database file (what a relaunch does)
    still shows the task as running and its steps."""
    from jarvis.memory.store import MemoryStore

    path = tmp_path / "jarvis.db"
    first = MemoryStore(path)
    await first.log_task("t1", "automation", "Tidy the desktop", "running", 1.0, None, "")
    await first.log_task_step("t1", 1, 2.0, "open_application", "{}", True, "Opened Finder.")
    await first.log_task_step("t1", 2, 3.0, "click_control", "{}", False, "Couldn't press Save.")
    first._conn.close()

    second = MemoryStore(path)
    assert [r["id"] for r in second.orphaned_tasks()] == ["t1"]
    assert [(r["seq"], r["ok"]) for r in second.task_steps("t1")] == [(1, 1), (2, 0)]


def test_the_account_of_a_stranded_task_says_what_the_record_shows_and_no_more():
    from jarvis.tasks.reconcile import describe_interrupted

    steps = [
        {"seq": 1, "tool": "open_application", "ok": 1, "summary": "Opened Finder.", "args_redacted": "{}"},
        {"seq": 2, "tool": "click_control", "ok": 1, "summary": "Pressed “Save”.",
         "args_redacted": json.dumps({"proven": ["file saved"]})},
        {"seq": 3, "tool": "click_control", "ok": 0, "summary": "Couldn't press Done.",
         "args_redacted": "{}"},
    ]
    text = describe_interrupted(steps)
    assert "3 steps recorded" in text
    assert "last action to report success was “Pressed “Save”.”" in text
    assert "last thing recorded was “Couldn't press Done.” (it failed)" in text
    assert "its checklist had confirmed: file saved" in text
    assert "may or may not have taken effect" in text
    assert "verified" not in text, "a tool's own report is not called verification"

    only = describe_interrupted(steps[:1])
    assert "1 step recorded" in only and "last thing recorded" not in only
    assert "confirmed" not in only
    assert "no telling" in describe_interrupted([])


async def test_startup_reconciles_a_stranded_task_without_replaying_anything(app):
    """Reconcile, don't replay: the report says how far the record shows it
    got, keeps that on the task's own record, drops the checkpoints — and
    does not call a tool or start a task."""
    import time

    from jarvis.core.events import EventType

    await app.memory.log_task("crashed-2", "automation", "Tidy the desktop", "running",
                              time.time(), None, "")
    await app.memory.log_task_step("crashed-2", 1, time.time(), "open_application", "{}", True,
                                   "Opened Finder.")
    await app.memory.log_task_step("crashed-2", 2, time.time(), "click_control",
                                   json.dumps({"proven": ["downloads folder open"]}), True,
                                   "Pressed “Downloads”.")

    await app.startup()
    await asyncio.sleep(0.2)

    notice = next(e.payload["message"] for e in app.bus.history if e.type == EventType.NOTICE
                  and "Tidy the desktop" in e.payload.get("message", ""))
    assert "2 steps recorded" in notice and "Pressed “Downloads”." in notice
    assert "downloads folder open" in notice and "check before running it again" in notice

    row = next(r for r in app.memory.task_history() if r["id"] == "crashed-2")
    assert row["status"] == "interrupted" and "2 steps recorded" in row["result"]
    assert app.memory.task_steps("crashed-2") == []
    assert not app.memory.orphaned_tasks()

    assert not [e for e in app.bus.history if e.type == EventType.TOOL_CALL], "nothing was replayed"
    assert app.tasks.all() == [], "no task was started to 'resume' it"
    await app.shutdown()


async def test_a_write_cancelled_before_it_starts_makes_no_coroutine_to_leave_unawaited(app, recwarn):
    """The loop closing right after a task finishes cancels the memory writes that
    haven't run yet. They used to be coroutines made up front, so each one cancelled
    that way was reported as "coroutine … was never awaited"."""
    import gc

    manager, made = app.tasks, []

    def write(*args):                      # a plain function that returns the coroutine, as a store method does
        made.append(args)
        return asyncio.sleep(0)

    manager._write(write, 1, 2)
    (pending,) = list(manager._writes)
    pending.cancel()
    await asyncio.gather(pending, return_exceptions=True)
    gc.collect()
    assert made == [], "no coroutine was made for a write that never ran"
    assert not [w for w in recwarn if "never awaited" in str(w.message)]


async def test_a_write_takes_its_arguments_when_it_is_scheduled_not_when_it_runs(app):
    manager, seen = app.tasks, []

    async def write(*args):
        seen.append(args)

    row = ["before"]
    manager._write(write, tuple(row), "fixed")
    row[0] = "after"
    await manager.flush()
    assert seen == [(("before",), "fixed")]
