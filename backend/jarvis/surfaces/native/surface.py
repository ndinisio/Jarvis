"""The native surface: what the app tools call to see and act on Mac apps.

One per JARVIS (``deps.native``). It owns the ``[axN]`` handles — stable
across looks for as long as the element exists, so "click [ax12]" still
means the same button after a re-read — the last look at each app (to say
what changed), and the latest numbered marks.

Acting prefers the accessibility action (``AXPress``: no mouse, works on a
window behind others) and falls back to a genuine click at the element's
centre after bringing its app to the front. Typing focuses the field
through Accessibility, then types real key events; a password field is
refused outright — JARVIS never types credentials.
"""

from __future__ import annotations

import asyncio
import contextlib
import platform
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ...core.logging import get_logger
from ...security import denylist
from . import ax, options
from .ax import Control, Frame, WindowSnapshot
from .input import PASTE_THRESHOLD, Clipboard, Keystroke, NativeInput, resolve_key
from .marks import Capture, Mark, build_marks, capture_frame, draw_overlay, image_size, render_marks
from .observer import ObserverThread, Wake

log = get_logger("jarvis.surfaces.native")

PERMISSION_HINT = (
    "Accessibility permission is required. Allow it in System Settings → Privacy & Security → "
    "Accessibility for whatever launched JARVIS (Terminal, or the JARVIS app)."
)
#: Handles kept before the registry starts again from ax1.
MAX_HANDLES = 4000
#: What an app posts when it comes to the front, and when a menu opens — the
#: two events the surface waits on (see ``NativeSurface.watch``).
ACTIVATED = "AXApplicationActivated"
MENU_OPENED = "AXMenuOpened"
#: Controls looked at when re-finding a stale handle — the same ceiling
#: ``find()`` uses for a whole-window search.
RELOCATE_MAX_LISTED = 1000


@dataclass(frozen=True)
class _Locator:
    """How a handle was first found, in terms that outlive the element
    itself: which app and window, and what the control was (role and
    name). When the element goes stale, this is what a plain re-search of
    the same window looks for."""

    app: str
    pid: int
    window: str
    ax_role: str
    subrole: str
    label: str
    identifier: str


class NativeError(Exception):
    def __init__(self, message: str, *, wrong_tool: bool = False, detail: str = ""):
        super().__init__(message)
        self.message = message
        self.wrong_tool = wrong_tool
        self.detail = detail or message


class AmbiguousOption(NativeError):
    """More than one item fits the request, so none was chosen. ``candidates`` are the matches,
    in menu order; ``occurrence`` on the next call says which."""

    def __init__(self, message: str, candidates: list[options.Option]):
        super().__init__(message, detail=f"ambiguous: {len(candidates)} options match")
        self.candidates = candidates


class NativeSurface:
    def __init__(self, deps=None, *, backend: Any = None, input: Any = None,
                 clipboard: Any = None, sleep: Callable[[float], None] = time.sleep,
                 observe: bool | None = None):
        self._deps = deps
        #: Wake waits early on the app's own notifications — None follows
        #: ``automation.native_observer`` in the configuration.
        self._observe = observe
        self._observer: ObserverThread | None = None
        self._observer_lock = threading.Lock()
        #: How many stale handles were re-found, and how many were refused
        #: (nothing matched, or more than one did) — what a validation run
        #: reads to tell "the reference survived" from "it was re-found".
        self.relocations = 0
        self.relocations_refused = 0
        #: Seconds spent so far finding the element behind a handle (reading
        #: it, checking it is still the one asked for, re-finding it) — so a
        #: timing harness can tell locating from acting.
        self.resolve_seconds = 0.0
        #: Where the last drag took hold and let go (``ax.GrabPoint``s) and the two elements'
        #: frames — what a validation run prints when the app didn't take the drop.
        self.last_drag: dict[str, Any] | None = None
        #: What the last mark_screen photograph covered (``marks.Capture``): the window server's
        #: rectangle and Accessibility's, which one the pixels were converted against, and why.
        self.last_capture: Capture | None = None
        self._backend = backend
        self._input = input
        self._clipboard = clipboard
        self._sleep = sleep
        self._handles: dict[str, Any] = {}
        self._by_key: dict[int, list[tuple[Any, str]]] = {}
        self._handle_app: dict[str, int] = {}
        #: The label last read for each handle — how a stale AX node reused
        #: for different content (a virtualised table row, common in Mail,
        #: Messages and Finder list view) gets caught: the handle itself
        #: stays valid, since it's the same element, but what it now shows
        #: has silently changed since the model read it. See _resolve().
        self._fingerprints: dict[str, str] = {}
        #: How each handle was first found — what _relocate() re-searches
        #: for when the element behind a handle has gone stale.
        self._locators: dict[str, _Locator] = {}
        self._counter = 0
        self._last: dict[int, WindowSnapshot] = {}
        self._marks: list[Mark] = []
        self._marks_pid = 0
        self._overlay: Path | None = None
        #: The app the latest look or action was in — where to look again.
        self.last_app = ""
        self._prompted = False

    # -- availability -------------------------------------------------------------
    @property
    def backend(self) -> Any:
        if self._backend is None:
            from .backend import MacAXBackend

            self._backend = MacAXBackend()
        return self._backend

    @property
    def input(self) -> NativeInput:
        if self._input is None:
            self._input = NativeInput()
        return self._input

    def available(self) -> bool:
        """Can this surface work at all here (macOS, PyObjC installed)?"""
        if self._backend is not None:
            return True
        if platform.system() != "Darwin":
            return False
        from .backend import MacAXBackend

        return MacAXBackend.available()

    def _require(self) -> Any:
        if not self.available():
            raise NativeError("Reading app windows needs macOS with the native extras installed "
                              "(pip install -e '.[native]').", detail="native surface unavailable")
        backend = self.backend
        if not backend.trusted():
            if not self._prompted:
                self._prompted = True
                backend.trusted(prompt=True)
            raise NativeError(PERMISSION_HINT, detail="accessibility not trusted")
        return backend

    def require_input(self) -> None:
        """Posting key and mouse events also needs Accessibility; without it
        macOS drops them silently — so say so rather than report success."""
        self._require()

    # -- looking --------------------------------------------------------------------
    async def read(self, app: str = "", *, offset: int = 0) -> tuple[WindowSnapshot, str]:
        """The front window of *app* (default: the frontmost app), listed."""
        return await asyncio.to_thread(self._read, app, offset)

    def _read(self, app: str, offset: int) -> tuple[WindowSnapshot, str]:
        snap = self._snapshot(app, offset=offset)
        changes = ax.describe_changes(self._last.get(snap.pid), snap) if offset == 0 else ""
        if offset == 0:
            self._last[snap.pid] = snap
        return snap, ax.render(snap, changes=changes)

    def _snapshot(self, app: str, *, offset: int = 0,
                  max_listed: int = ax.MAX_LISTED) -> WindowSnapshot:
        backend = self._require()
        pid, name = self._target(app)
        application = backend.application(pid)
        window, sheets = self._window_under_sheets(application)
        if window is None:
            raise NativeError(f"{name} has no window open.", detail="no window")
        self._guard(name, _title(backend, window))
        snap = ax.snapshot(backend, window, app=name, pid=pid, offset=offset, max_listed=max_listed,
                           blockers=[*sheets, *self._blockers(application, window)])
        snap.menus = self._menu_titles(application)
        for control in snap.controls:
            locator = _Locator(name, pid, snap.title, control.ax_role, control.subrole,
                               control.label, control.identifier)
            control.handle = self._handle_for(control.ref, pid, control.label, locator)
        self.last_app = name
        return snap

    def _window_under_sheets(self, application: Any) -> tuple[Any, list[Any]]:
        """The window to read, and any sheet over it the walk wouldn't find by itself.

        While a sheet is up, macOS reports *the sheet* as the app's focused window
        (seen with TextEdit's save sheet on macOS 27). Read as the window, its controls
        are listed as an ordinary window's: nothing says a sheet is blocking, the title
        is empty, and the window under it — the one the sheet hangs from — isn't read at
        all. So a focused sheet is followed up (through its AXParent, as many levels as
        there are sheets over sheets) to the window it belongs to, which is read with
        the sheet as what blocks it. Only a sheet is: a dialog that is a window of its
        own is just the window.

        The sheet is normally among that window's children, where the walk meets it. If
        it isn't, it is handed over as a blocker to read separately; either way it is
        listed once."""
        backend = self.backend
        window = backend.front_window(application)
        outer = None
        while window is not None and backend.attribute(window, "AXRole") == "AXSheet":
            owner = backend.attribute(window, "AXParent")
            if owner is None or backend.attribute(owner, "AXRole") not in {"AXWindow", "AXSheet"}:
                break
            outer, window = window, owner
        if outer is None:
            return window, []
        listed = list(backend.attribute(window, "AXChildren") or [])
        return window, ([] if any(backend.same(child, outer) for child in listed) else [outer])

    def _blockers(self, application: Any, window: Any) -> list[Any]:
        """The sheets and dialogs over *window* — listed first, since they
        block everything else until dealt with."""
        backend = self.backend
        return [w for w in backend.windows(application)
                if not backend.same(w, window) and _is_dialog(backend, w)]

    async def find(self, label: str, app: str = "") -> list[Control]:
        """Every control whose name matches *label*, at any depth of the
        window — exact matches if there are any, else those containing it."""
        snap = await asyncio.to_thread(self._snapshot, app, max_listed=1000)
        wanted = label.strip().lower()
        exact = [c for c in snap.controls if c.label.lower() == wanted]
        return exact or [c for c in snap.controls if wanted and wanted in c.label.lower()]

    async def describe(self, handle: str) -> dict[str, Any]:
        """What *handle* is right now — for the consequence check, which
        judges the real control, never the model's description of it."""
        return await asyncio.to_thread(self._describe, handle)

    def _describe(self, handle: str) -> dict[str, Any]:
        element, pid, attrs = self._resolve(handle)
        role = str(attrs.get("AXRole") or "")
        return {"role": ax.SUBROLE_NAMES.get(str(attrs.get("AXSubrole") or ""))
                or ax.ROLE_NAMES.get(role, role), "text": ax.label_for(attrs),
                "value": ax._text(attrs.get("AXValue")), "application": self._app_name(pid),
                "secure": attrs.get("AXSubrole") == "AXSecureTextField"}

    # -- acting ---------------------------------------------------------------------------
    async def press(self, handle: str, *, clicks: int = 1, button: str = "left") -> str:
        return await asyncio.to_thread(self._press, handle, clicks, button)

    def _press(self, handle: str, clicks: int, button: str) -> str:
        element, pid, attrs = self._resolve(handle)
        backend = self.backend
        name = self._name(element, attrs, "control")
        if attrs.get("AXEnabled") is False:
            raise NativeError(f"“{name}” is greyed out right now.")
        actions = backend.actions(element)
        if clicks == 1 and button == "left" and "AXPress" in actions and backend.perform(element, "AXPress"):
            return f"Pressed “{name}”."
        if clicks == 1 and button == "right" and "AXShowMenu" in actions \
                and backend.perform(element, "AXShowMenu"):
            return f"Opened the menu for “{name}”."
        if clicks == 1 and button == "left" and attrs.get("AXRole") in {"AXRow", "AXCell"} \
                and backend.set_attribute(element, "AXSelected", True):
            return f"Selected “{name}”."
        frame = ax.frame_of(attrs)
        if frame is None or frame.empty:
            raise NativeError(f"“{name}” can't be clicked — it has no position on screen.")
        self._front(pid)
        x, y = frame.center
        self.input.click(x, y, button=button, clicks=clicks)
        verb = {1: "Clicked", 2: "Double-clicked", 3: "Triple-clicked"}.get(clicks, "Clicked")
        return f"{'Right-clicked' if button == 'right' else verb} “{name}”."

    async def type_into(self, handle: str, text: str, *, replace: bool = True,
                        submit: bool = False) -> tuple[str, str]:
        """Type into a field; returns ``(summary, value_after)``."""
        return await asyncio.to_thread(self._type_into, handle, text, replace, submit)

    def _type_into(self, handle: str, text: str, replace: bool, submit: bool) -> tuple[str, str]:
        element, pid, attrs = self._resolve(handle)
        backend = self.backend
        name = ax.label_for(attrs) or "the field"
        if attrs.get("AXSubrole") == "AXSecureTextField":
            raise NativeError(f"“{name}” is a password field — I never type passwords. "
                              "Please enter it yourself.", detail="secure text field")
        if attrs.get("AXEnabled") is False:
            raise NativeError(f"“{name}” is greyed out right now.")
        self._front(pid)
        if not backend.set_attribute(element, "AXFocused", True):
            frame = ax.frame_of(attrs)
            if frame is None:
                raise NativeError(f"I couldn't put the cursor in “{name}”.")
            self.input.click(*frame.center)
        self._sleep(0.08)
        # Focusing a field selects what's in it: replace types over that;
        # otherwise the cursor goes to the very end first.
        self.input.press(resolve_key("cmd+a" if replace else "cmd+down"))
        self._type(text)
        value = ax._text(backend.attribute(element, "AXValue"))
        if text.strip() and text.strip() not in value and attrs.get("AXRole") != "AXComboBox":
            # Some fields ignore synthetic keys; setting the value directly
            # is the Accessibility API's own way in.
            current = value if not replace else ""
            if backend.set_attribute(element, "AXValue", current + text):
                value = ax._text(backend.attribute(element, "AXValue"))
        if submit:
            self.input.press(resolve_key("return"))
        typed = f"Typed “{text[:60]}” into “{name}”"
        return typed + (" and pressed Return." if submit else "."), value

    def _type(self, text: str) -> None:
        if len(text) <= PASTE_THRESHOLD:
            self.input.type_text(text)
            return
        clipboard = self._clipboard or Clipboard()
        saved = clipboard.save()
        try:
            clipboard.set_text(text)
            self.input.press(resolve_key("cmd+v"))
            self._sleep(0.25)
        finally:
            clipboard.restore(saved)

    async def type_text(self, text: str, app: str = "") -> str:
        """Type into whatever has focus — refusing a password field."""
        return await asyncio.to_thread(self._type_text, text, app)

    def _type_text(self, text: str, app: str) -> str:
        backend = self._require()
        pid, name = self._target(app)
        focused = backend.focused_element(backend.application(pid))
        if focused is not None and backend.attribute(focused, "AXSubrole") == "AXSecureTextField":
            raise NativeError("The cursor is in a password field — I never type passwords. "
                              "Please enter it yourself.", detail="secure text field")
        if app:
            self._front(pid)
        self._type(text)
        return name

    async def press_key(self, stroke: Keystroke, repeat: int = 1) -> None:
        backend = self._require()
        front = backend.frontmost()               # a key goes to whatever is in front
        if front is not None:
            self._guard(front[1])
        await asyncio.to_thread(self.input.press, stroke, repeat)

    async def choose_option(self, handle: str, option: str, *, occurrence: int | None = None) -> str:
        return await asyncio.to_thread(self._choose_option, handle, option, occurrence)

    def _choose_option(self, handle: str, option: str, occurrence: int | None = None) -> str:
        element, pid, attrs = self._resolve(handle)
        backend = self.backend
        name = ax.label_for(attrs) or "the menu"
        items = self._menu_options(element)
        opened = False
        if not items:
            with self.watch(pid, MENU_OPENED) as wake:
                backend.perform(element, "AXPress")
                opened = True
                self._poll(lambda: bool(self._menu_items(element)), timeout_s=1.5, wake=wake)
            items = self._menu_options(element)
        found = options.resolve(items, option, occurrence=occurrence)
        if found.option is None:
            self._dismiss(element, opened)
            if found.ambiguous:
                listing = "; ".join(options.describe(n, o) for n, o in enumerate(found.matches[:8], 1))
                raise AmbiguousOption(
                    f"“{name}” has {len(found.matches)} options that fit “{option}” — {listing}. "
                    "Say which: choose_option again with occurrence set to its number, or give "
                    "more of its name if they differ.", found.matches)
            if found.out_of_range:
                raise NativeError(f"“{name}” has only {len(found.matches)} option(s) that fit “{option}”, "
                                  f"so there is no number {occurrence}.")
            titles = [f"{o.title}{'' if o.enabled else ' (greyed out)'}" for o in items]
            raise NativeError(f"“{name}” has no option “{option}”"
                              + (f" — it has: {', '.join(titles[:25])}." if titles else "."))
        chosen = found.option
        if not chosen.enabled:
            self._dismiss(element, opened)
            raise NativeError(f"“{chosen.title}” is greyed out right now.")
        if not backend.perform(chosen.element, "AXPress"):
            self._dismiss(element, opened)
            raise NativeError(f"I couldn't choose “{option}”.")
        which = (f" (the {options.ordinal(found.matches.index(chosen) + 1)} of {len(found.matches)} that fit)"
                 if len(found.matches) > 1 else "")
        return f"Chose “{chosen.title}”{which} in “{name}”."

    async def choose_menu(self, path: list[str], app: str = "") -> str:
        return await asyncio.to_thread(self._choose_menu, path, app)

    def _choose_menu(self, path: list[str], app: str) -> str:
        backend = self._require()
        steps = [p for p in (str(s).strip() for s in path) if p]
        if len(steps) < 2:
            raise NativeError("A menu path needs the menu and the item, like [\"File\", \"Export…\"].")
        pid, name = self._target(app)
        application = backend.application(pid)
        bar = backend.attribute(application, "AXMenuBar")
        current = _match(list(backend.attribute(bar, "AXChildren") or []), steps[0], backend)
        if current is None:
            raise NativeError(f"{name} has no “{steps[0]}” menu — its menus are: "
                              f"{', '.join(self._menu_titles(application))}.")
        opened_root = None
        chosen_titles = [_title(backend, current)]
        for step in steps[1:]:
            items = self._menu_items(current)
            chosen = _match(items, step, backend)
            if chosen is None and opened_root is None:
                with self.watch(pid, MENU_OPENED) as wake:
                    backend.perform(current, "AXPress")    # some menus fill in when opened
                    opened_root = current
                    self._poll(lambda: bool(self._menu_items(current)), timeout_s=1.5, wake=wake)
                items = self._menu_items(current)
                chosen = _match(items, step, backend)
            if chosen is None:
                self._dismiss(opened_root, bool(opened_root))
                titles = [t for t in (_title(backend, i) for i in items) if t]
                raise NativeError(f"There's no “{step}” in {_title(backend, current)} — it has: "
                                  f"{', '.join(titles[:20])}.")
            current = chosen
            chosen_titles.append(_title(backend, chosen))
        if backend.attribute(current, "AXEnabled") is False:
            self._dismiss(opened_root, bool(opened_root))
            raise NativeError(f"“{' › '.join(steps)}” is greyed out right now.")
        if not backend.perform(current, "AXPress"):
            self._dismiss(opened_root, bool(opened_root))
            raise NativeError(f"I couldn't choose “{' › '.join(chosen_titles)}”.")
        return f"Chose {' › '.join(chosen_titles)} in {name}."

    def _name(self, element: Any, attrs: dict[str, Any], fallback: str) -> str:
        """What to call *element* in a sentence: its listed label — a row is named by the text
        inside it, which ``label_for`` on the row alone never sees — else its kind."""
        role = str(attrs.get("AXRole") or "")
        return ax.control_label(self.backend, element, role, attrs) or ax.ROLE_NAMES.get(role, fallback)

    async def drag(self, source: str, target: str) -> str:
        return await asyncio.to_thread(self._drag, source, target)

    def _drag(self, source: str, target: str) -> str:
        start, pid, a = self._resolve(source)
        end, _, b = self._resolve(target)
        grab, drop = ax.grab_point(self.backend, start, a), ax.grab_point(self.backend, end, b)
        if grab is None or drop is None:
            raise NativeError("One of those has no position on screen to drag from or to.")
        self._front(pid)
        self.last_drag = {"from": grab, "to": drop, "source": ax.frame_of(a), "target": ax.frame_of(b)}
        self.input.drag(grab.point, drop.point)
        return (f"Dragged “{self._name(start, a, 'item')}” onto “{self._name(end, b, 'item')}” "
                "— the drop is the app's to accept; check that it moved.")

    async def scroll_to(self, handle: str) -> None:
        """Put the pointer over *handle*, so the next scroll goes to it."""
        await asyncio.to_thread(self._scroll_to, handle)

    def _scroll_to(self, handle: str) -> None:
        _, pid, attrs = self._resolve(handle)
        frame = ax.frame_of(attrs)
        if frame is not None:
            self._front(pid)
            self.input.move(*frame.center)

    # -- marks ------------------------------------------------------------------------------
    async def mark(self, capture: Callable[[int, int], Any], app: str = "", *,
                   recognize: Callable[[str], list] | None = None,
                   overlay_dir: Path | None = None) -> tuple[list[Mark], str, Path | None]:
        """Photograph the front window, read its text, and number everything
        worth pointing at. *capture(pid, window_number)* returns a PNG path."""
        snap, _ = await self.read(app)
        backend = self.backend
        window_number = await asyncio.to_thread(backend.window_number, snap.pid, snap.title)
        bounds = await asyncio.to_thread(self._window_bounds, window_number) if window_number is not None else None
        if window_number is None or (snap.frame is None and bounds is None):
            raise NativeError(f"I couldn't find {snap.app}'s window on screen to look at.")
        path = await capture(snap.pid, window_number)
        if recognize is None:
            from .marks import recognize_text as recognize
        texts = await asyncio.to_thread(recognize, str(path))
        size = await asyncio.to_thread(image_size, path)
        covered = capture_frame(snap.frame, bounds, size)
        self.last_capture = covered
        if covered is None:  # pragma: no cover - guarded above
            raise NativeError(f"I couldn't find {snap.app}'s window on screen to look at.")
        marks = build_marks(texts, snap.controls, size, covered.frame)
        self._marks, self._marks_pid = marks, snap.pid
        overlay = None
        if overlay_dir is not None:
            overlay = await asyncio.to_thread(draw_overlay, path, marks, covered.frame,
                                              Path(overlay_dir) / (Path(path).stem + "-marks.png"))
        self._overlay = overlay
        return marks, render_marks(marks, app=snap.app, title=snap.title), overlay

    def _window_bounds(self, number: int) -> Frame | None:
        found = getattr(self.backend, "window_bounds", None)
        bounds = found(number) if found is not None else None
        return Frame(*bounds) if bounds else None

    async def click_mark(self, number: int, *, clicks: int = 1, button: str = "left") -> Mark:
        mark = next((m for m in self._marks if m.number == number), None)
        if mark is None:
            raise NativeError(f"There's no mark {number} — mark_screen again to see the current ones.")
        await asyncio.to_thread(self._front, self._marks_pid)
        x, y = mark.frame.center
        await asyncio.to_thread(self.input.click, x, y, button=button, clicks=clicks)
        return mark

    def marks(self) -> list[Mark]:
        return list(self._marks)

    def last_overlay(self) -> Path | None:
        """The latest marked screenshot, for a vision model to pick from."""
        return self._overlay if self._overlay is not None and self._overlay.exists() else None

    # -- notifications ------------------------------------------------------------------------------
    def _observing(self) -> bool:
        if self._observe is not None:
            return self._observe
        automation = getattr(getattr(self._deps, "config", None), "automation", None)
        return bool(getattr(automation, "native_observer", False))

    @contextlib.contextmanager
    def watch(self, pid: int, *notifications: str) -> Iterator[Wake | None]:
        """Listen for *notifications* from *pid*'s application while the block
        runs, yielding what to sleep on between polls — or None when there is
        nothing to listen with (observing is off, the backend can't, the
        subscription didn't take). Never raises, and None is not an error: the
        wait just polls, as it always did."""
        watch = None
        with contextlib.suppress(Exception):
            observer = self._observer_thread()
            if observer is not None:
                watch = observer.watch(pid, *notifications)
        try:
            yield watch.wake if watch is not None else None
        finally:
            if watch is not None:
                watch.close()

    def _observer_thread(self) -> ObserverThread | None:
        if not self._observing():
            return None
        with self._observer_lock:            # waits run on worker threads: make only one
            if self._observer is None:
                make = getattr(self.backend, "observer_driver", None)
                if make is not None:
                    self._observer = ObserverThread(make())
            return self._observer

    def observer_stats(self) -> dict[str, Any]:
        """What the observer thread is doing, for a validation run; empty if
        there isn't one."""
        return self._observer.stats() if self._observer is not None else {}

    def close(self) -> None:
        """Stop the observer thread, if one was started. Idempotent."""
        if self._observer is not None:
            self._observer.stop()

    # -- plumbing --------------------------------------------------------------------------------
    def _target(self, app: str) -> tuple[int, str]:
        backend = self.backend
        found = backend.find_app(app) if app.strip() else backend.frontmost()
        if found is None:
            raise NativeError(f"{app} doesn't appear to be running." if app.strip()
                              else "I couldn't tell which app is in front.", wrong_tool=bool(app.strip()))
        self._guard(found[1])
        self.last_app = found[1]
        return found

    def _guard(self, app: str, window: str = "") -> None:
        """Never read or operate a password manager, the Keychain or a
        permissions pane (security/denylist.py)."""
        reason = denylist.app_refusal(getattr(self._deps, "config", None), app, window)
        if reason:
            raise NativeError(reason, detail="denylist")

    def _front(self, pid: int) -> None:
        front = self.backend.frontmost()
        if front is None or front[0] != pid:
            with self.watch(pid, ACTIVATED) as wake:         # subscribed first: the event can't be missed
                self.backend.activate(pid)
                self._poll(lambda: (self.backend.frontmost() or (None,))[0] == pid, timeout_s=1.5,
                           wake=wake)

    def _poll(self, ready: Callable[[], bool], *, timeout_s: float, interval_s: float = 0.05,
              wake: Wake | None = None) -> bool:
        """Check *ready* repeatedly rather than a flat sleep — a slow app
        activation or menu populate under load (more likely exactly when a
        long background errand is sharing the machine with other work) gets
        however long it actually needs, up to *timeout_s*, instead of a
        fixed guess that's indistinguishable from "it will never be ready".
        Mirrors the web surface's own poll-until-settled pattern
        (tools/browser/observe.py).

        *wake*, if given, ends the pause between two checks the moment the
        app posts the event being waited for — and only that: *ready* still
        decides, the pause is no longer than it would have been, and the wait
        ends at the same deadline either way."""
        deadline = time.monotonic() + timeout_s
        while True:
            if wake is not None:
                wake.clear()                # before the check, so an event during it isn't lost
            if ready():
                return True
            if time.monotonic() >= deadline:
                return False
            if wake is not None:
                wake.wait(min(interval_s, max(0.0, deadline - time.monotonic())))
            else:
                self._sleep(interval_s)

    def _app_name(self, pid: int) -> str:
        snap = self._last.get(pid)
        if snap:
            return snap.app
        return next((s.app for s in self._last.values() if s.pid == pid), "")

    def _menu_titles(self, application: Any) -> list[str]:
        backend = self.backend
        bar = backend.attribute(application, "AXMenuBar")
        titles = [_title(backend, item) for item in list(backend.attribute(bar, "AXChildren") or [])]
        return [t for t in titles[1:] if t]         # the first is the Apple menu

    def _menu_items(self, element: Any) -> list[Any]:
        backend = self.backend
        items: list[Any] = []
        for child in list(backend.attribute(element, "AXChildren") or []):
            if backend.attribute(child, "AXRole") == "AXMenu":
                items.extend(list(backend.attribute(child, "AXChildren") or []))
            elif backend.attribute(child, "AXRole") == "AXMenuItem":
                items.append(child)
        return [i for i in items if _title(self.backend, i)]

    def _menu_options(self, element: Any) -> list[options.Option]:
        """The menu's titled items as options: in order, with whether each can be chosen and the
        heading (the nearest greyed-out item) it sits under."""
        backend = self.backend
        found: list[options.Option] = []
        heading = ""
        for item in self._menu_items(element):
            enabled = backend.attribute(item, "AXEnabled") is not False
            title = _title(backend, item)
            found.append(options.Option(item, title, enabled, len(found) + 1, heading))
            if not enabled:
                heading = title
        return found

    def _dismiss(self, element: Any, opened: bool) -> None:
        if not opened or element is None:
            return
        for child in list(self.backend.attribute(element, "AXChildren") or []):
            if self.backend.attribute(child, "AXRole") == "AXMenu":
                self.backend.perform(child, "AXCancel")
                return
        self.input.press(resolve_key("escape"))

    def _handle_for(self, element: Any, pid: int, label: str = "",
                    locator: _Locator | None = None) -> str:
        backend = self.backend
        key = backend.key(element)
        for known, handle in self._by_key.get(key, []):
            if backend.same(known, element):
                self._fingerprints[handle] = label
                if locator is not None:
                    self._locators[handle] = locator
                return handle
        if self._counter >= MAX_HANDLES:
            self._handles.clear()
            self._by_key.clear()
            self._handle_app.clear()
            self._fingerprints.clear()
            self._locators.clear()
            self._counter = 0
        self._counter += 1
        handle = f"ax{self._counter}"
        self._handles[handle] = element
        self._by_key.setdefault(key, []).append((element, handle))
        self._handle_app[handle] = pid
        self._fingerprints[handle] = label
        if locator is not None:
            self._locators[handle] = locator
        return handle

    def _resolve(self, handle: str) -> tuple[Any, int, dict[str, Any]]:
        """The live element behind *handle*, its owning app, and the
        attributes just fetched to verify it — handed back rather than
        discarded, so a caller that needs them too (every one does) reads
        the app's Accessibility server once per action, not twice.

        A handle whose element has gone stale (gone, or recycled for other
        content) is re-found by what it was — see _relocate() — and only
        reported to the model when that finds nothing, or can't tell
        which of several it was."""
        started = time.monotonic()
        try:
            return self._resolve_handle(handle)
        finally:
            self.resolve_seconds += time.monotonic() - started

    def _resolve_handle(self, handle: str) -> tuple[Any, int, dict[str, Any]]:
        self._require()
        cleaned = str(handle).strip().strip("[]").lower()
        element = self._handles.get(cleaned)
        if element is None:
            raise NativeError(f"There's no control [{cleaned}] — read_window to see the current ones.")
        attrs = self.backend.attributes(element, ax.ATTRIBUTES)
        problem = self._staleness(cleaned, element, attrs)
        if problem:
            found = self._relocate(cleaned)
            if found is None:
                self.relocations_refused += 1
                raise NativeError(problem)
            self.relocations += 1
            element, attrs = found
        pid = self._handle_app.get(cleaned, 0)
        window = self.backend.attribute(element, "AXWindow")
        self._guard(self._app_name(pid), _title(self.backend, window) if window is not None else "")
        self.last_app = self._app_name(pid) or self.last_app
        return element, pid, attrs

    def _staleness(self, handle: str, element: Any, attrs: dict[str, Any]) -> str:
        """Why the element behind *handle* can't be trusted as read, or ""."""
        role = str(attrs.get("AXRole") or "")
        if not role:
            return f"[{handle}] isn't on screen any more — read_window again."
        expected = self._fingerprints.get(handle, "")
        if expected:
            current = ax.control_label(self.backend, element, role, attrs)
            if current and current != expected:
                # Same AX element, different content: a recycled row/cell in
                # a virtualised list (Mail, Messages, Finder list view, most
                # Electron apps), not a genuinely stale handle — but acting
                # on it now would hit whatever it shows today, not what the
                # model actually chose.
                return (f"[{handle}] now shows “{current}”, not “{expected}” as last read — its "
                        "content has changed since then. read_window again before acting on it.")
        return ""

    def _relocate(self, handle: str) -> tuple[Any, dict[str, Any]] | None:
        """Re-find a stale handle's control by what it was, rather than
        sending the model back to read the window again for what is often a
        trivially re-findable button.

        Deliberately no looser than the check that rejected the element: it
        looks only in the window the handle was read in, of the same app
        process; it needs the same role, name and identifier; it gives up
        unless exactly one control matches (two, or a window too big to
        search whole, means it can't say which was meant); the match must
        be on screen; and the match must then pass the very same staleness
        check as any other element. It does not refresh other handles'
        fingerprints, so everything else the model read stays protected.
        """
        locator = self._locators.get(handle)
        if locator is None or not (locator.label or locator.identifier):
            return None
        backend = self.backend
        application = backend.application(locator.pid)
        windows = [w for w in backend.windows(application)
                   if ax._text(backend.attribute(w, "AXTitle")) == locator.window]
        matches: list[Control] = []
        for window in windows:
            snap = ax.snapshot(backend, window, app=locator.app, pid=locator.pid,
                               max_listed=RELOCATE_MAX_LISTED,
                               blockers=self._blockers(application, window))
            if snap.truncated:
                return None             # can't show nothing else matches
            matches.extend(c for c in snap.controls
                           if (c.ax_role, c.subrole, c.label, c.identifier)
                           == (locator.ax_role, locator.subrole, locator.label, locator.identifier))
        if len(matches) != 1 or not matches[0].visible:
            return None
        candidate = matches[0].ref
        attrs = backend.attributes(candidate, ax.ATTRIBUTES)
        if self._staleness(handle, candidate, attrs):
            return None
        self._repoint(handle, candidate)
        log.debug("re-found [%s] (%s “%s”) after it went stale", handle, matches[0].role, locator.label)
        return candidate, attrs

    def _repoint(self, handle: str, element: Any) -> None:
        """Make *handle* mean *element*. The old element stops being known by
        this handle, so seeing it again later gets it a handle of its own
        rather than a listing that disagrees with what the handle acts on."""
        backend = self.backend
        old = self._handles.get(handle)
        self._handles[handle] = element
        if old is not None:
            old_key = backend.key(old)
            kept = [(e, h) for e, h in self._by_key.get(old_key, []) if h != handle]
            if kept:
                self._by_key[old_key] = kept
            else:
                self._by_key.pop(old_key, None)
        key = backend.key(element)
        if not any(backend.same(known, element) for known, _ in self._by_key.get(key, [])):
            self._by_key.setdefault(key, []).append((element, handle))


def _title(backend: Any, element: Any) -> str:
    attrs = backend.attributes(element, ("AXTitle", "AXDescription", "AXValue"))
    for key in ("AXTitle", "AXDescription", "AXValue"):
        text = ax._text(attrs.get(key))
        if text:
            return text
    return ""


def _match(elements: list[Any], wanted: str, backend: Any) -> Any:
    """The menu-path item titled *wanted* (see :mod:`.options` for the rule). Several items with
    the very same title resolve to the first — two open windows called "Untitled" in the Window
    menu — but a looser fit that is not unique is refused."""
    items = [options.Option(e, _title(backend, e), True, n) for n, e in enumerate(elements, 1)]
    found = options.resolve(items, wanted, strict=False)
    return found.option.element if found.option is not None else None


def _is_dialog(backend: Any, window: Any) -> bool:
    attrs = backend.attributes(window, ("AXRole", "AXSubrole", "AXModal"))
    return (attrs.get("AXSubrole") in {"AXDialog", "AXSystemDialog", "AXAlert"}
            or bool(attrs.get("AXModal")))


__all__ = ["PERMISSION_HINT", "AmbiguousOption", "Frame", "NativeError", "NativeSurface"]
