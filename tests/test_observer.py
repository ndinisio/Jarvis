"""Waking a native wait early on the app's own notification (surfaces/native/observer.py).

What is tested here is everything JARVIS decides and does around an
``AXObserver``: that one dedicated thread owns every subscription, that a
notification does nothing but wake the waiter, that a wake only ever ends a
pause early and never changes what a wait decides or when it gives up, and
that a driver that refuses, raises, hangs or fails leaves the wait exactly as
it was. The platform is a fake driver that, like a run loop, delivers
notifications only inside ``spin()`` on the thread that calls it.

What cannot be tested here is whether macOS posts ``AXApplicationActivated``
and ``AXMenuOpened`` for those actions, delivers them to a Python callback on
a non-main thread, or keeps the asyncio loop responsive while that thread
spins. That is what ``scripts/check_native.py --observe`` measures on a Mac —
and why ``automation.native_observer`` is off by default.
"""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import pytest
from jarvis.core.config import Config
from jarvis.surfaces.native import NativeError, NativeSurface
from jarvis.surfaces.native.backend import MacObserverDriver
from jarvis.surfaces.native.observer import ObserverThread, Wake
from jarvis.surfaces.native.surface import ACTIVATED, MENU_OPENED
from test_native import El, FakeBackend, RecordingInput, _notes_app

THREAD = "jarvis-ax-observer"


def eventually(condition, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.01)
    return condition()


class FakeDriver:
    """A platform whose notifications arrive only inside ``spin()``, on the
    thread that is spinning — as a CFRunLoop delivers them."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.calls: list[tuple[str, str]] = []          # (method, thread it ran on)
        self.fired_on: list[str] = []
        self.observed: dict[int, tuple[int, str, object]] = {}
        self.pending: list[tuple[int, str]] = []
        self.refuse: set[str] = set()
        self.raise_on_observe = False
        self.block_observe: threading.Event | None = None
        self.spin_error: Exception | None = None
        self.nothing_to_run = False
        self.spins = 0
        self.subscribed = 0                              # observe() calls that returned a token
        self._next = 0
        self._wake = threading.Event()

    def _note(self, method: str) -> None:
        with self.lock:
            self.calls.append((method, threading.current_thread().name))

    def prepare(self) -> None:
        self._note("prepare")

    def observe(self, pid, notification, fire):
        self._note("observe")
        if self.block_observe is not None:
            self.block_observe.wait(5)
        if self.raise_on_observe:
            raise RuntimeError("the app isn't answering")
        if notification in self.refuse:
            return None
        with self.lock:
            self._next += 1
            self.observed[self._next] = (pid, notification, fire)
            self.subscribed += 1
            return self._next

    def unobserve(self, token) -> None:
        self._note("unobserve")
        with self.lock:
            self.observed.pop(token, None)

    def spin(self, timeout_s: float) -> bool:
        self._note("spin")
        self.spins += 1
        if self.spin_error is not None:
            raise self.spin_error
        if self.nothing_to_run:
            return False
        self._wake.wait(timeout_s)
        self._wake.clear()
        with self.lock:
            pending, self.pending = self.pending, []
            listeners = list(self.observed.values())
        for pid, notification in pending:
            for p, n, fire in listeners:
                if (p, n) == (pid, notification):
                    self.fired_on.append(threading.current_thread().name)
                    fire()
        return True

    def interrupt(self) -> None:
        self._wake.set()

    def post(self, pid: int, notification: str) -> None:
        """The app posting a notification (from any thread)."""
        with self.lock:
            self.pending.append((pid, notification))
        self._wake.set()


@pytest.fixture
def driver():
    return FakeDriver()


@pytest.fixture
def observer(driver):
    thread = ObserverThread(driver)
    yield thread
    thread.stop()


# ---------------------------------------------------------------------------
# the thread and its hand-offs
# ---------------------------------------------------------------------------
def test_a_notification_wakes_the_waiter_and_only_for_what_was_watched(driver, observer):
    watch = observer.watch(7, MENU_OPENED)
    assert watch is not None
    driver.post(7, "AXSomethingElse")
    driver.post(8, MENU_OPENED)
    assert not watch.wake.wait(0.2), "another notification, or another app's, isn't ours"
    driver.post(7, MENU_OPENED)
    assert watch.wake.wait(2.0)
    assert watch.wake.fired == 1 and watch.wake.fired_at is not None


def test_each_watch_wakes_only_its_own_waiter(driver, observer):
    first, second = observer.watch(1, ACTIVATED), observer.watch(2, ACTIVATED)
    driver.post(2, ACTIVATED)
    assert second.wake.wait(2.0)
    assert not first.wake.wait(0.1)


def test_every_platform_call_and_every_callback_happens_on_the_one_observer_thread(driver, observer):
    with observer.watch(7, MENU_OPENED, ACTIVATED) as wake:
        driver.post(7, MENU_OPENED)
        assert wake.wait(2.0)
    assert eventually(lambda: not driver.observed)
    observer.stop()
    wanted = {"prepare", "observe", "unobserve", "spin"}
    seen = {method for method, _ in driver.calls}
    assert wanted <= seen
    assert {thread for method, thread in driver.calls if method in wanted} == {THREAD}
    assert set(driver.fired_on) == {THREAD}, "the callback ran on the observer thread, nowhere else"


def test_leaving_the_watch_unsubscribes_and_a_second_close_is_harmless(driver, observer):
    watch = observer.watch(7, MENU_OPENED, ACTIVATED)
    assert len(driver.observed) == 2
    watch.close()
    watch.close()
    assert eventually(lambda: not driver.observed)


def test_a_notification_after_the_watch_closed_wakes_nothing(driver, observer):
    watch = observer.watch(7, MENU_OPENED)
    watch.close()
    assert eventually(lambda: not driver.observed)
    driver.post(7, MENU_OPENED)
    time.sleep(0.15)
    assert watch.wake.fired == 0


def test_the_loop_is_not_spun_while_nothing_is_subscribed(driver, observer):
    assert driver.calls == [], "no thread until someone watches"
    observer.watch(7, MENU_OPENED).close()
    assert eventually(lambda: not driver.observed)
    time.sleep(0.15)
    settled = driver.spins
    time.sleep(0.3)
    assert driver.spins == settled


def test_a_request_while_the_loop_is_spinning_interrupts_it(driver):
    """Slices here are two seconds long: a second watch is only served in time
    because the request interrupts the one in progress."""
    thread = ObserverThread(driver, spin_s=2.0, request_timeout_s=1.0)
    try:
        assert thread.watch(1, ACTIVATED) is not None
        started = time.monotonic()
        assert thread.watch(2, ACTIVATED) is not None
        assert time.monotonic() - started < 0.8
    finally:
        thread.stop()


def test_an_empty_loop_is_not_spun_hot(driver, observer):
    driver.nothing_to_run = True                       # e.g. the app quit and its source went with it
    assert observer.watch(7, MENU_OPENED) is not None
    time.sleep(0.4)
    assert driver.spins < 30


# ---------------------------------------------------------------------------
# when the platform won't or can't
# ---------------------------------------------------------------------------
def test_a_subscription_the_app_refuses_is_no_watch_and_the_thread_carries_on(driver, observer):
    driver.refuse = {MENU_OPENED}
    assert observer.watch(7, MENU_OPENED) is None
    watch = observer.watch(7, MENU_OPENED, ACTIVATED)          # one of two taken: a watch on that one
    assert watch is not None
    driver.post(7, ACTIVATED)
    assert watch.wake.wait(2.0)


def test_a_driver_that_raises_while_subscribing_is_survived(driver, observer):
    driver.raise_on_observe = True
    assert observer.watch(7, MENU_OPENED) is None
    driver.raise_on_observe = False
    assert observer.watch(7, MENU_OPENED) is not None


def test_a_hung_app_costs_one_timeout_and_the_late_subscription_is_undone(driver):
    driver.block_observe = threading.Event()
    thread = ObserverThread(driver, request_timeout_s=0.3)
    try:
        started = time.monotonic()
        assert thread.watch(7, MENU_OPENED) is None
        assert time.monotonic() - started >= 0.25
        started = time.monotonic()
        assert thread.watch(8, MENU_OPENED) is None, "the thread is still stuck in the first app"
        assert time.monotonic() - started < 0.1, "…and says so at once rather than waiting out another timeout"
        driver.block_observe.set()                     # the app answers at last
        assert eventually(lambda: driver.subscribed == 1), "the late subscription did get made…"
        assert eventually(lambda: not driver.observed), "…and, with nobody waiting for it, is undone"
        assert eventually(lambda: thread.watch(9, MENU_OPENED) is not None)
    finally:
        thread.stop()


def test_a_failing_run_loop_ends_observing_and_waits_go_back_to_polling(driver, observer):
    watch = observer.watch(7, MENU_OPENED)
    assert watch is not None
    driver.spin_error = RuntimeError("the run loop is gone")
    driver.post(7, MENU_OPENED)
    assert eventually(lambda: not driver.observed), "what was subscribed is torn down"
    assert observer.watch(7, MENU_OPENED) is None
    watch.close()                                      # closing a watch the thread no longer serves is fine


def test_stop_unsubscribes_everything_and_refuses_new_watches(driver, observer):
    observer.watch(7, MENU_OPENED)
    observer.watch(8, ACTIVATED)
    observer.stop()
    observer.stop()
    assert not driver.observed
    assert observer.watch(7, MENU_OPENED) is None


def test_every_watch_is_unsubscribed_however_many_are_dropped_unclosed(driver, observer):
    """On Python 3.14 this test's twin above failed, intermittently: a watch dropped
    without being closed let its request be freed, the next request reused its ``id()``,
    and the earlier subscription was overwritten in the thread's table — never unsubscribed."""
    for number in range(150):
        observer.watch(number, MENU_OPENED)             # the Watch is dropped, unclosed
    observer.stop()
    assert not driver.observed
    assert driver.subscribed == 150


def test_request_numbers_are_unique_even_when_the_requests_are_dropped_at_once():
    from jarvis.surfaces.native.observer import _Add

    numbers = [_Add(1, (MENU_OPENED,), Wake()).id for _ in range(2000)]    # each freed as the next is made
    assert len(set(numbers)) == 2000


def test_request_numbers_are_unique_across_threads():
    from jarvis.surfaces.native.observer import _Add

    seen: list[int] = []

    def make():
        seen.extend(_Add(1, (MENU_OPENED,), Wake()).id for _ in range(500))

    threads = [threading.Thread(target=make) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(set(seen)) == 4000


def test_watching_nothing_is_no_watch(observer):
    assert observer.watch(7) is None


def test_stats_say_whether_the_thread_is_alive_what_it_holds_and_how_it_ended(driver, observer):
    assert observer.stats() == {"alive": False, "broken": False, "stopped": False, "subscriptions": 0,
                                "spins": 0, "callback_errors": 0}
    watch = observer.watch(7, MENU_OPENED, ACTIVATED)
    assert eventually(lambda: observer.stats()["spins"] > 0)
    stats = observer.stats()
    assert stats["alive"] and stats["subscriptions"] == 1, "one watch, however many notifications it holds"
    watch.close()
    assert eventually(lambda: observer.stats()["subscriptions"] == 0)
    driver.errors = 3                                   # a driver that counts its callbacks' failures
    assert observer.stats()["callback_errors"] == 3
    observer.stop()
    assert observer.stats()["stopped"] and not observer.stats()["alive"]


def test_a_failed_loop_shows_in_the_stats(driver, observer):
    observer.watch(7, MENU_OPENED)
    driver.spin_error = RuntimeError("gone")
    driver.post(7, MENU_OPENED)
    assert eventually(lambda: observer.stats()["broken"])


def test_the_surface_reports_the_observer_it_has_and_none_when_it_has_none(driver):
    surface, _, _ = _surface(observe=True, driver=driver)
    try:
        assert surface.observer_stats() == {}
        with surface.watch(101, ACTIVATED):
            assert surface.observer_stats()["subscriptions"] == 1
    finally:
        surface.close()


def test_the_wake_records_when_the_first_notification_came():
    wake = Wake()
    assert wake.fired_at is None
    wake.fire()
    first = wake.fired_at
    wake.fire()
    assert wake.fired == 2 and wake.fired_at == first
    wake.clear()
    assert not wake.wait(0.01)


# ---------------------------------------------------------------------------
# the surface
# ---------------------------------------------------------------------------
class ObservingBackend(FakeBackend):
    """The fake Mac, with a fake observer driver."""

    def __init__(self, *args, driver: FakeDriver, **kwargs):
        super().__init__(*args, **kwargs)
        self.driver = driver
        self.drivers_made = 0
        self.delay = 0.0                                 # how long making a driver takes

    def observer_driver(self):
        time.sleep(self.delay)
        self.drivers_made += 1
        return self.driver


def _surface(*, observe, driver=None, with_driver=True):
    app, parts = _notes_app()
    finder = El("AXApplication", "Finder", actions=(), AXWindows=[], AXMenuBar=El("AXMenuBar", actions=()))
    apps = {"Notes": (101, app), "Finder": (202, finder)}
    backend = (ObservingBackend(apps, front="Notes", driver=driver) if with_driver
               else FakeBackend(apps, front="Notes"))
    surface = NativeSurface(backend=backend, input=RecordingInput(), sleep=lambda _s: None, observe=observe)
    return surface, backend, parts


def test_observing_is_off_unless_asked_for_and_nothing_is_started(driver):
    surface, backend, _ = _surface(observe=None, driver=driver)
    with surface.watch(101, ACTIVATED) as wake:
        assert wake is None
    assert backend.drivers_made == 0 and driver.calls == []


def test_observing_follows_the_configuration_when_not_told_otherwise(driver):
    surface, backend, _ = _surface(observe=None, driver=driver)
    assert Config().automation.native_observer is False
    on = SimpleNamespace(config=SimpleNamespace(automation=SimpleNamespace(native_observer=True)))
    surface._deps = on
    try:
        with surface.watch(101, ACTIVATED) as wake:
            assert wake is not None
    finally:
        surface.close()


def test_waits_on_many_threads_share_one_observer_thread(driver):
    surface, backend, _ = _surface(observe=True, driver=driver)
    backend.delay = 0.05                                 # a wide window for the waiters to race in
    results = []

    def wait():
        with surface.watch(101, ACTIVATED) as wake:
            results.append(wake is not None)

    try:
        threads = [threading.Thread(target=wait) for _ in range(12)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert backend.drivers_made == 1, "one driver, one thread, however many waiters raced to make it"
        assert results.count(True) >= 1
    finally:
        surface.close()


def test_switching_observing_off_stops_new_watches_at_once(driver):
    surface, _, _ = _surface(observe=None, driver=driver)
    deps = SimpleNamespace(config=SimpleNamespace(automation=SimpleNamespace(native_observer=True)))
    surface._deps = deps
    try:
        with surface.watch(101, ACTIVATED) as wake:
            assert wake is not None
        deps.config.automation.native_observer = False
        with surface.watch(101, ACTIVATED) as wake:
            assert wake is None
    finally:
        surface.close()


def test_a_backend_that_cannot_observe_just_polls(driver):
    surface, _, _ = _surface(observe=True, driver=driver, with_driver=False)
    with surface.watch(101, ACTIVATED) as wake:
        assert wake is None


def test_a_watch_yields_a_wake_and_unsubscribes_on_leaving_even_after_an_error(driver):
    surface, _, _ = _surface(observe=True, driver=driver)
    try:
        with pytest.raises(RuntimeError), surface.watch(101, MENU_OPENED) as wake:
            assert wake is not None and len(driver.observed) == 1
            raise RuntimeError("the block failed")
        assert eventually(lambda: not driver.observed)
    finally:
        surface.close()


def test_a_driver_that_cannot_even_be_made_just_polls(driver):
    class Broken(ObservingBackend):
        def observer_driver(self):
            raise RuntimeError("no run loop for you")

    app, _ = _notes_app()
    surface = NativeSurface(backend=Broken({"Notes": (101, app)}, front="Notes", driver=driver),
                            input=RecordingInput(), sleep=lambda _s: None, observe=True)
    with surface.watch(101, ACTIVATED) as wake:
        assert wake is None


def test_a_wake_ends_the_pause_between_polls_and_nothing_else():
    surface, _, _ = _surface(observe=False)
    wake = Wake()
    flag = {"ready": False}

    def arrives():
        time.sleep(0.1)
        flag["ready"] = True
        wake.fire()

    threading.Thread(target=arrives, daemon=True).start()
    started = time.monotonic()
    assert surface._poll(lambda: flag["ready"], timeout_s=10, interval_s=5.0, wake=wake)
    assert time.monotonic() - started < 2.0, "woken by the event, not after the 5 s pause"


def test_a_wake_does_not_stand_in_for_the_check():
    """A notification that arrives while the condition is still false keeps the wait going."""
    surface, _, _ = _surface(observe=False)
    wake = Wake()
    checks = []

    def ready():
        checks.append(1)
        if len(checks) <= 2:
            wake.fire()                                # events that don't mean we're ready
        return len(checks) >= 3

    started = time.monotonic()
    assert surface._poll(ready, timeout_s=20, interval_s=5.0, wake=wake)
    assert len(checks) == 3, "each early event led to another check, not to a verdict"
    assert time.monotonic() - started < 2.0, "an event during a check isn't lost to a pause that follows"


def test_a_wait_with_no_event_still_gives_up_at_the_same_deadline():
    surface, _, _ = _surface(observe=False)
    started = time.monotonic()
    assert not surface._poll(lambda: False, timeout_s=0.3, interval_s=5.0, wake=Wake())
    assert 0.25 <= time.monotonic() - started < 1.0, "the pause is cut to the time left, not 5 s"


def test_a_wait_without_a_wake_polls_exactly_as_before():
    slept = []
    surface, _, _ = _surface(observe=False)
    surface._sleep = slept.append
    results = iter([False, False, True])
    assert surface._poll(lambda: next(results), timeout_s=5)
    assert slept == [0.05, 0.05]


def _recording_watch(surface, log):
    import contextlib

    @contextlib.contextmanager
    def watch(pid, *notifications):
        log.append(("watch", pid, notifications))
        try:
            yield None
        finally:
            log.append(("unwatch", pid))

    surface.watch = watch


def test_bringing_an_app_to_the_front_listens_for_it_before_asking(driver):
    surface, backend, _ = _surface(observe=False)
    log = []
    _recording_watch(surface, log)
    original = backend.activate

    def activate(pid):
        log.append(("activate", pid))
        return original(pid)

    backend.activate = activate
    surface._front(202)
    assert log == [("watch", 202, (ACTIVATED,)), ("activate", 202), ("unwatch", 202)]


def test_an_app_already_in_front_is_not_watched_for(driver):
    surface, _, _ = _surface(observe=False)
    log = []
    _recording_watch(surface, log)
    surface._front(101)
    assert log == []


async def test_choosing_from_a_pop_up_listens_for_the_menu_while_it_opens(driver):
    surface, _, _ = _surface(observe=False)
    popup = El("AXPopUpButton", "Format", value="PDF", frame=(10, 10, 80, 20),
               actions=("AXPress",))
    item = El("AXMenuItem", "A4")
    popup.on_perform = lambda _a: popup.children.append(El("AXMenu", actions=(), children=[item]))
    app = El("AXApplication", "Notes", actions=(), AXWindows=[El("AXWindow", "w", actions=(), children=[popup])],
             AXMenuBar=El("AXMenuBar", actions=()))
    app.attrs["AXFocusedWindow"] = app.attrs["AXWindows"][0]
    surface.backend.apps["Notes"] = (101, app)
    log = []
    _recording_watch(surface, log)
    snap, _ = await surface.read("Notes")
    handle = next(c.handle for c in snap.controls if c.label == "Format")
    result = await surface.choose_option(handle, "A4")
    assert result.startswith("Chose “A4”")
    assert log == [("watch", 101, (MENU_OPENED,)), ("unwatch", 101)]


async def test_choosing_from_a_menu_bar_menu_listens_for_it_to_open(driver):
    surface, _, parts = _surface(observe=False)
    log = []
    _recording_watch(surface, log)
    parts["file"].children[0].children.clear()                 # a menu that fills in only when opened
    parts["file"].on_perform = lambda _a: parts["file"].children[0].children.append(parts["export"])
    await surface.choose_menu(["File", "Export as PDF"], "Notes")
    assert log == [("watch", 101, (MENU_OPENED,)), ("unwatch", 101)]
    assert parts["export"].performed == ["AXPress"]


async def test_a_menu_that_fills_in_late_is_waited_for_through_the_real_thread(driver):
    """The whole path with fakes in place of macOS: the surface subscribes
    through the observer thread, the 'app' fills the menu in 150 ms later and
    posts its notification, and the choice goes through — then nothing is left
    subscribed."""
    surface, _, parts = _surface(observe=True, driver=driver)
    try:
        parts["file"].children[0].children.clear()

        def opens(_action):
            def fill():
                parts["file"].children[0].children.append(parts["export"])
                driver.post(101, MENU_OPENED)
            threading.Timer(0.15, fill).start()

        parts["file"].on_perform = opens
        summary = await surface.choose_menu(["File", "Export as PDF"], "Notes")
        assert "Export as PDF" in summary and parts["export"].performed == ["AXPress"]
        assert eventually(lambda: not driver.observed)
        assert {t for m, t in driver.calls if m in {"observe", "unobserve", "spin"}} == {THREAD}
    finally:
        surface.close()


async def test_a_menu_wait_does_not_depend_on_the_notification_ever_coming(driver):
    """Subscribed, but the app never says anything: the poll finds the menu anyway."""
    surface, _, parts = _surface(observe=True, driver=driver)
    try:
        parts["file"].children[0].children.clear()

        def opens(_action):
            threading.Timer(0.15, lambda: parts["file"].children[0].children.append(parts["export"])).start()

        parts["file"].on_perform = opens
        summary = await surface.choose_menu(["File", "Export as PDF"], "Notes")
        assert "Export as PDF" in summary
    finally:
        surface.close()


async def test_a_missing_option_still_says_what_there_is_and_unsubscribes(driver):
    surface, _, parts = _surface(observe=True, driver=driver)
    try:
        parts["file"].children[0].children.clear()
        with pytest.raises(NativeError, match="no “Nope”|There's no “Nope”"):
            await surface.choose_menu(["File", "Nope"], "Notes")
        assert eventually(lambda: not driver.observed)
    finally:
        surface.close()


def test_closing_the_surface_stops_the_thread_and_is_idempotent(driver):
    surface, _, _ = _surface(observe=True, driver=driver)
    with surface.watch(101, ACTIVATED) as wake:
        assert wake is not None
    surface.close()
    surface.close()
    with surface.watch(101, ACTIVATED) as wake:
        assert wake is None, "stopped for good"
    NativeSurface(observe=False).close()                      # never started: nothing to stop


# ---------------------------------------------------------------------------
# the PyObjC driver, against a stand-in for ApplicationServices / CoreFoundation
# ---------------------------------------------------------------------------
class StandIn:
    """Records the calls the driver makes, in order, and answers as configured.
    This pins the call sequence as written — it cannot say macOS accepts it."""

    def __init__(self, *, create_error=0, add_error=0, raises=False, run_result=3):
        self.log: list[tuple] = []
        self.create_error, self.add_error, self.raises, self.run_result = create_error, add_error, raises, run_result
        self.callback = None

    # ApplicationServices
    def AXObserverCreate(self, pid, callback, out):
        self.log.append(("create", pid, out))
        if self.raises:
            raise RuntimeError("boom")
        self.callback = callback
        return self.create_error, (None if self.create_error else "observer")

    def AXUIElementCreateApplication(self, pid):
        self.log.append(("application", pid))
        return f"app-{pid}"

    def AXObserverAddNotification(self, observer, element, notification, refcon):
        self.log.append(("add", observer, element, notification, refcon))
        return self.add_error

    def AXObserverGetRunLoopSource(self, observer):
        self.log.append(("source", observer))
        return "source"

    def AXObserverRemoveNotification(self, observer, element, notification):
        self.log.append(("remove", observer, element, notification))
        return 0

    # CoreFoundation
    kCFRunLoopDefaultMode = "default-mode"

    def CFRunLoopGetCurrent(self):
        return "loop"

    def CFRunLoopAddSource(self, loop, source, mode):
        self.log.append(("add-source", loop, source, mode))

    def CFRunLoopRemoveSource(self, loop, source, mode):
        self.log.append(("remove-source", loop, source, mode))

    def CFRunLoopRunInMode(self, mode, seconds, return_after_source):
        self.log.append(("run", mode, seconds, return_after_source))
        return self.run_result

    def CFRunLoopStop(self, loop):
        self.log.append(("stop", loop))


def test_the_driver_subscribes_in_the_documented_order_and_keeps_the_callback_alive():
    stand_in = StandIn()
    driver = MacObserverDriver(stand_in, stand_in)
    driver.prepare()
    fired = []
    token = driver.observe(7, MENU_OPENED, lambda: fired.append(1))
    assert [entry[0] for entry in stand_in.log] == ["create", "application", "add", "source", "add-source"]
    assert stand_in.log[0] == ("create", 7, None)
    assert stand_in.log[2] == ("add", "observer", "app-7", MENU_OPENED, None)
    assert stand_in.log[4] == ("add-source", "loop", "source", "default-mode")
    assert token.callback is stand_in.callback, "held, because PyObjC does not retain it"
    stand_in.callback("observer", "element", MENU_OPENED, None)
    assert fired == [1]


def test_a_callback_that_raises_does_not_escape_into_the_run_loop_and_is_counted():
    stand_in = StandIn()
    driver = MacObserverDriver(stand_in, stand_in)

    def boom():
        raise RuntimeError("waiter gone")

    driver.observe(7, MENU_OPENED, boom)
    assert driver.errors == 0
    stand_in.callback("observer", "element", MENU_OPENED, None)       # must not raise
    stand_in.callback("observer", "element", MENU_OPENED, None)
    assert driver.errors == 2


@pytest.mark.parametrize("stand_in", [StandIn(create_error=-25204), StandIn(add_error=-25205),
                                      StandIn(raises=True)])
def test_a_subscription_the_platform_refuses_is_none_and_adds_no_source(stand_in):
    driver = MacObserverDriver(stand_in, stand_in)
    assert driver.observe(7, MENU_OPENED, lambda: None) is None
    assert not any(entry[0] == "add-source" for entry in stand_in.log)


def test_unobserving_removes_the_notification_and_the_source_and_never_raises():
    stand_in = StandIn()
    driver = MacObserverDriver(stand_in, stand_in)
    token = driver.observe(7, MENU_OPENED, lambda: None)
    stand_in.log.clear()
    driver.unobserve(token)
    assert [entry[0] for entry in stand_in.log] == ["remove", "remove-source"]
    stand_in.AXObserverRemoveNotification = lambda *a: (_ for _ in ()).throw(RuntimeError("gone"))
    driver.unobserve(token)


def test_spinning_runs_the_default_mode_and_reports_an_empty_loop():
    stand_in = StandIn(run_result=3)
    driver = MacObserverDriver(stand_in, stand_in)
    assert driver.spin(0.05) is True
    assert stand_in.log[-1] == ("run", "default-mode", 0.05, True)
    stand_in.run_result = 1                                           # kCFRunLoopRunFinished
    assert driver.spin(0.05) is False


def test_interrupting_stops_the_observer_threads_loop_and_is_a_no_op_before_it_exists():
    stand_in = StandIn()
    driver = MacObserverDriver(stand_in, stand_in)
    driver.interrupt()
    assert stand_in.log == []
    driver.prepare()
    driver.interrupt()
    assert stand_in.log == [("stop", "loop")]
