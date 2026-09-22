"""ScreenWatcher: the cheap-signal-gated background screen watcher.

Fine-grained change-detection/cooldown behaviour is tested by driving
_poll_once() directly rather than the real start()/sleep loop — deterministic
and independent of real wall-clock timing. start()/stop() are covered
separately as the lifecycle they are.

Most tests mutate `app.config` directly rather than going through
`app.config_store.update()` — the same shortcut test_models.py's
`app.config.models.fast.model = "..."` already uses. That distinction matters
here specifically: `config_store.update()` also fires `core/app.py`'s
`_on_config_change`, which calls `app.screen_watcher.reconfigure(...)` and
would auto-start JARVIS's *own* watcher instance in the background —
confounding a test that constructs and drives its own standalone
`ScreenWatcher`. `test_reconfigure_starts_and_stops_the_watcher_live` is the
one test that deliberately keeps `config_store.update()`, because that
app-level wiring is exactly what it's testing.
"""

from __future__ import annotations

import asyncio

import pytest
from jarvis.core.errors import ConfirmationDeclined
from jarvis.tools.base import ToolResult
from jarvis.tools.registry import build_registry
from jarvis.vision.watcher import ScreenWatcher

pytestmark = pytest.mark.asyncio


class _FakeSignal:
    """Controllable stand-in for the frontmost app/window AppleScript calls."""

    def __init__(self):
        self.app = ""
        self.window = "0"

    async def frontmost_app(self) -> str:
        return self.app

    async def frontmost_window_id(self) -> str:
        return self.window


def _signal(app, monkeypatch) -> _FakeSignal:
    signal = _FakeSignal()
    monkeypatch.setattr(app.deps.controller, "frontmost_app", signal.frontmost_app)
    monkeypatch.setattr(app.deps.controller, "frontmost_window_id", signal.frontmost_window_id)
    return signal


def _enable_and_stub_watch_screen(app, monkeypatch, *, min_vision_interval_s: float = 0.0):
    """Turn the capability on directly (see module docstring) and rebuild
    the registry so watch_screen exists, then stub its run()."""
    app.config.capabilities.screen_awareness = True
    app.config.screen_awareness.min_vision_interval_s = min_vision_interval_s
    app.deps.registry = build_registry(app.deps)

    calls: list[dict] = []

    async def run(args, ctx):
        calls.append(dict(args))
        return ToolResult(data={"answer": "ok"}, summary="ok")

    tool = app.deps.registry.get("watch_screen")
    assert tool is not None
    monkeypatch.setattr(tool, "run", run)
    return calls


# -- change detection ----------------------------------------------------------

async def test_poll_fires_only_on_a_real_change(app, monkeypatch):
    signal = _signal(app, monkeypatch)
    calls = _enable_and_stub_watch_screen(app, monkeypatch)
    watcher = ScreenWatcher(app.deps)

    signal.app, signal.window = "Safari", "1"
    await watcher._poll_once()
    assert len(calls) == 1

    await watcher._poll_once()  # unchanged
    assert len(calls) == 1

    signal.app, signal.window = "Safari", "2"  # same app, new window
    await watcher._poll_once()
    assert len(calls) == 2

    signal.app, signal.window = "Mail", "2"  # app switch
    await watcher._poll_once()
    assert len(calls) == 3


async def test_poll_ignores_an_empty_frontmost_signal(app, monkeypatch):
    """A constantly-empty signal (no AppleScript available, e.g. non-macOS)
    must never spuriously fire a vision call."""
    signal = _signal(app, monkeypatch)
    calls = _enable_and_stub_watch_screen(app, monkeypatch)
    watcher = ScreenWatcher(app.deps)

    signal.app, signal.window = "", "0"
    await watcher._poll_once()
    await watcher._poll_once()
    assert calls == []


async def test_poll_survives_a_controller_error(app, monkeypatch):
    calls = _enable_and_stub_watch_screen(app, monkeypatch)

    async def broken():
        raise RuntimeError("osascript blew up")

    monkeypatch.setattr(app.deps.controller, "frontmost_app", broken)
    watcher = ScreenWatcher(app.deps)

    await watcher._poll_once()  # must not raise
    assert calls == []


async def test_vision_call_is_cooled_down_independent_of_the_poll(app, monkeypatch):
    """Two rapid signal changes within one cooldown window must only spend
    one real vision-model call."""
    signal = _signal(app, monkeypatch)
    calls = _enable_and_stub_watch_screen(app, monkeypatch, min_vision_interval_s=100.0)
    watcher = ScreenWatcher(app.deps)

    signal.app, signal.window = "Safari", "1"
    await watcher._poll_once()
    assert len(calls) == 1

    signal.app, signal.window = "Mail", "2"  # a real change, but still inside the cooldown
    await watcher._poll_once()
    assert len(calls) == 1


# -- lifecycle -------------------------------------------------------------

async def test_start_does_nothing_when_the_capability_flag_is_off(app):
    assert app.config.capabilities.screen_awareness is False
    watcher = ScreenWatcher(app.deps)
    assert await watcher.start() is False
    assert watcher.running is False


async def test_start_does_nothing_when_screen_capture_is_disabled(app):
    app.config.capabilities.screen_awareness = True
    app.config.security.allow_screen_capture = False
    app.config.security.auto_approve = ["low", "medium"]
    watcher = ScreenWatcher(app.deps)
    assert await watcher.start() is False
    assert watcher.running is False


async def test_start_asks_once_then_probes_screen_recording_permission(app, monkeypatch):
    app.config.capabilities.screen_awareness = True
    app.config.security.auto_approve = ["low", "medium"]
    app.config.screen_awareness.poll_interval_s = 100.0

    async def check_permission(kind):
        assert kind == "screen_recording"
        return True, "ok"

    monkeypatch.setattr(app.deps.controller, "check_permission", check_permission)
    watcher = ScreenWatcher(app.deps)

    assert await watcher.start() is True
    assert watcher.running is True
    assert watcher._consented is True
    await watcher.stop()
    assert watcher.running is False


async def test_start_refuses_without_screen_recording_permission(app, monkeypatch):
    app.config.capabilities.screen_awareness = True
    app.config.security.auto_approve = ["low", "medium"]

    async def check_permission(kind):
        return False, "Screen Recording permission is required"

    monkeypatch.setattr(app.deps.controller, "check_permission", check_permission)
    watcher = ScreenWatcher(app.deps)

    assert await watcher.start() is False
    assert watcher.running is False


async def test_start_refuses_when_consent_is_declined(app, monkeypatch):
    app.config.capabilities.screen_awareness = True

    async def declined(*args, **kwargs):
        raise ConfirmationDeclined("no")

    monkeypatch.setattr(app.deps.permissions, "require", declined)
    watcher = ScreenWatcher(app.deps)

    assert await watcher.start() is False
    assert watcher.running is False


async def test_concurrent_start_never_creates_a_second_loop(app, monkeypatch):
    app.config.capabilities.screen_awareness = True
    app.config.security.auto_approve = ["low", "medium"]
    app.config.screen_awareness.poll_interval_s = 100.0

    async def check_permission(kind):
        # A genuine yield here is what makes this test actually exercise the
        # lock: without it, the two gathered start() calls would never truly
        # interleave, and the test would pass even with no lock at all.
        await asyncio.sleep(0)
        return True, "ok"

    monkeypatch.setattr(app.deps.controller, "check_permission", check_permission)
    watcher = ScreenWatcher(app.deps)

    results = await asyncio.gather(watcher.start(), watcher.start())
    assert results == [True, True]
    assert watcher.running is True
    watch_tasks = [t for t in asyncio.all_tasks() if t.get_name() == "jarvis-screen-watch"]
    assert len(watch_tasks) == 1, "the lock must prevent a second loop from ever being created"

    await watcher.stop()
    assert watcher.running is False


async def test_reconfigure_starts_and_stops_the_watcher_live(app, monkeypatch):
    """The app-level wiring in core/app.py: flipping the capability flag via
    config_store.update() must start/stop app.screen_watcher itself, live,
    with no restart of the process."""
    app.config_store.update({"security": {"auto_approve": ["low", "medium"]},
                             "screen_awareness": {"poll_interval_s": 100.0}})

    async def check_permission(kind):
        return True, "ok"

    monkeypatch.setattr(app.deps.controller, "check_permission", check_permission)
    assert app.screen_watcher.running is False

    app.config_store.update({"capabilities": {"screen_awareness": True}})
    await asyncio.sleep(0.05)
    assert app.screen_watcher.running is True

    app.config_store.update({"capabilities": {"screen_awareness": False}})
    await asyncio.sleep(0.05)
    assert app.screen_watcher.running is False
