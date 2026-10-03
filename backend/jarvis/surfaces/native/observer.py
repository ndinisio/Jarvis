"""Cutting a wait short when the app says what the wait is for.

The native surface waits on apps in two places that are really "wait for a
known event": an app coming to the front, and a menu filling in once opened
(``NativeSurface._poll``). Polling every 50 ms finds out eventually; an
``AXObserver`` notification (``AXApplicationActivated``, ``AXMenuOpened``) is
told the instant it happens.

This is deliberately *only an early wake-up*. The poll's own ``ready()`` check
stays the one judge of whether the thing happened, and it keeps running at its
usual pace: a notification that never comes, a subscription that couldn't be
made, or an observer that is switched off all leave the wait exactly as it was.
Nothing here can make a wait fail or give up later than it would have; the one
cost is the subscription itself, which is bounded (``REQUEST_TIMEOUT_S``) and
skipped outright while the thread is held up by an app that isn't answering.

How it is threaded
------------------
Notifications are delivered by a ``CFRunLoop`` that some thread has to spin,
and JARVIS has none (every Accessibility call elsewhere is a plain synchronous
call on whatever worker thread asked). So there is one dedicated daemon thread,
started on first use, that owns every observer: subscribe and unsubscribe
requests from other threads are queued to it, and it spins the loop in short
slices between them (blocking on the queue, doing nothing, while nothing is
subscribed). A notification's callback does the least possible — set a
``threading.Event`` — and the waiter, which is itself a worker thread inside
``asyncio.to_thread``, wakes. Nothing runs on the observer thread but that.

Every call into the platform goes through a *driver* (``MacObserverDriver`` in
``backend.py``, the one file that imports PyObjC), so all of the above — the
marshalling, the hand-offs, a hung app, a driver that fails — is tested here
against a fake. What a fake cannot say is whether macOS really posts those
notifications for those actions, or delivers them to a Python callback on a
non-main thread; ``scripts/check_native.py --observe`` measures that on a Mac,
and the surface only uses any of this when ``automation.native_observer`` is on.
"""

from __future__ import annotations

import contextlib
import queue
import threading
import time
from collections.abc import Callable
from typing import Any, Protocol

from ...core.logging import get_logger

log = get_logger("jarvis.surfaces.native.observer")

#: How long one slice of the run loop lasts before the thread looks at its
#: queue again. A request interrupts a slice, so this only bounds the rare
#: case where the interrupt lands between slices.
SPIN_SLICE_S = 0.05
#: The longest a caller waits for a subscription to be made before carrying on
#: without one. A subscription is a round trip to the app being watched.
REQUEST_TIMEOUT_S = 0.25
#: How often an idle thread (nothing subscribed) looks up to see it was stopped.
_IDLE_CHECK_S = 1.0
#: How long stop() waits for the thread to wind down.
_STOP_JOIN_S = 1.0


class Driver(Protocol):
    """What the thread needs of the platform. All of it is called on the
    observer thread except ``interrupt``, which may be called from any."""

    def prepare(self) -> None: ...

    def observe(self, pid: int, notification: str, fire: Callable[[], None]) -> Any | None:
        """Call *fire* (on the observer thread) whenever *pid*'s application
        posts *notification*. Returns a token for ``unobserve``, or None if it
        couldn't be subscribed."""

    def unobserve(self, token: Any) -> None: ...

    def spin(self, timeout_s: float) -> bool:
        """Run the loop that delivers notifications, for up to *timeout_s*.
        False if there was nothing for it to run (every source gone) and it
        returned at once — so the thread doesn't spin hot on an empty loop."""

    def interrupt(self) -> None:
        """Make a ``spin`` in progress return now."""


class Wake:
    """What a waiter sleeps on between two polls: set when a watched
    notification arrives. ``fired_at`` is when the first one did (monotonic),
    for the harness that measures how much sooner than polling it was."""

    def __init__(self) -> None:
        self._event = threading.Event()
        self.fired = 0
        self.fired_at: float | None = None

    def fire(self) -> None:
        if self.fired_at is None:
            self.fired_at = time.monotonic()
        self.fired += 1
        self._event.set()

    def clear(self) -> None:
        self._event.clear()

    def wait(self, timeout_s: float) -> bool:
        return self._event.wait(timeout_s)


class Watch:
    """A live subscription. Use it as a context manager: leaving it
    unsubscribes. ``wake`` is what to sleep on."""

    def __init__(self, wake: Wake, close: Callable[[], None]):
        self.wake = wake
        self._close = close
        self._closed = False
        self._lock = threading.Lock()

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self._close()

    def __enter__(self) -> Wake:
        return self.wake

    def __exit__(self, *exc: object) -> None:
        self.close()


class _Add:
    def __init__(self, pid: int, notifications: tuple[str, ...], wake: Wake):
        self.id = id(self)
        self.pid, self.notifications, self.wake = pid, notifications, wake
        self.done = threading.Event()
        self.tokens: list[Any] = []
        self._lock = threading.Lock()
        self._finished = False
        self._abandoned = False

    def finish(self, tokens: list[Any]) -> list[Any]:
        """Called by the thread. Returns the tokens to unsubscribe at once: all
        of them if the caller already gave up, none if it's still waiting."""
        with self._lock:
            self._finished = True
            if self._abandoned:
                return tokens
            self.tokens = tokens
        self.done.set()
        return []

    def abandon(self) -> bool:
        """Called by the waiting side on timeout. True if it really gave up,
        False if the thread finished first (and the result stands)."""
        with self._lock:
            if self._finished:
                return False
            self._abandoned = True
            return True


class _Remove:
    def __init__(self, add_id: int):
        self.add_id = add_id


class _Stop:
    pass


class ObserverThread:
    def __init__(self, driver: Driver, *, spin_s: float = SPIN_SLICE_S,
                 request_timeout_s: float = REQUEST_TIMEOUT_S):
        self._driver = driver
        self._spin_s = spin_s
        self._request_timeout_s = request_timeout_s
        self._requests: queue.Queue[Any] = queue.Queue()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        #: True while the thread is inside a driver call that talks to an app
        #: — which a hung app can hold for a while.
        self._busy = False
        self._broken = False
        self._stopped = False

    # -- the calling side -----------------------------------------------------------------
    def watch(self, pid: int, *notifications: str) -> Watch | None:
        """Subscribe to *notifications* from *pid*'s application. None — and
        the caller carries on polling — if that isn't possible right now: the
        thread is stopped or has failed, it is held up by an app that isn't
        answering, or the app refused every subscription."""
        if not notifications:
            return None
        with self._lock:
            if self._broken or self._stopped or self._busy:
                return None
            self._start()
        request = _Add(pid, tuple(notifications), Wake())
        self._submit(request)
        if not request.done.wait(self._request_timeout_s) and request.abandon():
            return None                        # the thread cleans up whatever it makes late
        if not request.tokens:
            return None
        return Watch(request.wake, lambda: self._submit(_Remove(request.id)))

    def stop(self) -> None:
        """Wind the thread down, unsubscribing everything. Idempotent."""
        with self._lock:
            if self._stopped:
                return
            self._stopped = True
            thread = self._thread
        if thread is not None:
            self._submit(_Stop())
            thread.join(_STOP_JOIN_S)

    def _start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="jarvis-ax-observer", daemon=True)
            self._thread.start()

    def _submit(self, request: Any) -> None:
        self._requests.put(request)
        with contextlib.suppress(Exception):   # nothing was spinning, or the loop is gone
            self._driver.interrupt()

    # -- the observer thread ----------------------------------------------------------------
    def _run(self) -> None:
        live: dict[int, list[Any]] = {}
        healthy = True
        try:
            self._driver.prepare()
            while True:
                # Idle (nothing subscribed): sleep until asked. Subscribed: take
                # whatever is queued, else spin the loop — and if the loop has
                # nothing left to run (an app quit under us), wait on the queue
                # for a slice instead of spinning hot.
                block_s = _IDLE_CHECK_S if not live else (0.0 if healthy else self._spin_s)
                try:
                    request = self._requests.get(timeout=block_s) if block_s else self._requests.get_nowait()
                except queue.Empty:
                    request = None
                if isinstance(request, _Stop) or (request is None and self._stopped and not live):
                    break
                if request is not None:
                    self._handle(request, live)
                    healthy = True
                elif live:
                    healthy = bool(self._driver.spin(self._spin_s))
        except Exception as exc:               # a failing driver ends observing; waits go back to polling
            log.warning("AX observer stopped: %s", exc)
            with self._lock:
                self._broken = True
        finally:
            for tokens in live.values():
                self._unobserve_all(tokens)
            self._refuse_pending()

    def _handle(self, request: Any, live: dict[int, list[Any]]) -> None:
        if isinstance(request, _Add):
            tokens = self._subscribe(request)
            leftover = request.finish(tokens)
            if leftover:                       # the caller timed out while we were subscribing
                self._unobserve_all(leftover)
            elif tokens:
                live[request.id] = tokens
        elif isinstance(request, _Remove):
            self._unobserve_all(live.pop(request.add_id, []))

    def _subscribe(self, request: _Add) -> list[Any]:
        tokens: list[Any] = []
        self._set_busy(True)
        try:
            for notification in request.notifications:
                try:
                    token = self._driver.observe(request.pid, notification, request.wake.fire)
                except Exception as exc:
                    log.debug("couldn't observe %s: %s", notification, exc)
                    continue
                if token is not None:
                    tokens.append(token)
        finally:
            self._set_busy(False)
        return tokens

    def _unobserve_all(self, tokens: list[Any]) -> None:
        self._set_busy(True)
        try:
            for token in tokens:
                try:
                    self._driver.unobserve(token)
                except Exception as exc:
                    log.debug("couldn't stop observing: %s", exc)
        finally:
            self._set_busy(False)

    def _set_busy(self, busy: bool) -> None:
        with self._lock:
            self._busy = busy

    def _refuse_pending(self) -> None:
        """Nobody is going to serve what's still queued: answer the waiting
        callers (with nothing) rather than leave them to time out."""
        while True:
            try:
                request = self._requests.get_nowait()
            except queue.Empty:
                return
            if isinstance(request, _Add):
                request.finish([])


__all__ = ["Driver", "ObserverThread", "Wake", "Watch"]
