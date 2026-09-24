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
import platform
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ...core.logging import get_logger
from . import ax
from .ax import Control, Frame, WindowSnapshot
from .input import PASTE_THRESHOLD, Clipboard, Keystroke, NativeInput, resolve_key
from .marks import Mark, build_marks, draw_overlay, image_size, render_marks

log = get_logger("jarvis.surfaces.native")

PERMISSION_HINT = (
    "Accessibility permission is required. Allow it in System Settings → Privacy & Security → "
    "Accessibility for whatever launched JARVIS (Terminal, or the JARVIS app)."
)
#: Handles kept before the registry starts again from ax1.
MAX_HANDLES = 4000


class NativeError(Exception):
    def __init__(self, message: str, *, wrong_tool: bool = False, detail: str = ""):
        super().__init__(message)
        self.message = message
        self.wrong_tool = wrong_tool
        self.detail = detail or message


class NativeSurface:
    def __init__(self, deps=None, *, backend: Any = None, input: Any = None,
                 clipboard: Any = None, sleep: Callable[[float], None] = time.sleep):
        self._deps = deps
        self._backend = backend
        self._input = input
        self._clipboard = clipboard
        self._sleep = sleep
        self._handles: dict[str, Any] = {}
        self._by_key: dict[int, list[tuple[Any, str]]] = {}
        self._handle_app: dict[str, int] = {}
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
        window = backend.front_window(application)
        if window is None:
            raise NativeError(f"{name} has no window open.", detail="no window")
        dialogs = [w for w in backend.windows(application)
                   if not backend.same(w, window) and _is_dialog(backend, w)]
        snap = ax.snapshot(backend, window, app=name, pid=pid, offset=offset,
                           max_listed=max_listed, blockers=dialogs)
        snap.menus = self._menu_titles(application)
        for control in snap.controls:
            control.handle = self._handle_for(control.ref, pid)
        self.last_app = name
        return snap

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
        element, pid = self._resolve(handle)
        attrs = self.backend.attributes(element, ax.ATTRIBUTES)
        role = str(attrs.get("AXRole") or "")
        return {"role": ax.SUBROLE_NAMES.get(str(attrs.get("AXSubrole") or ""))
                or ax.ROLE_NAMES.get(role, role), "text": ax.label_for(attrs),
                "value": ax._text(attrs.get("AXValue")), "application": self._app_name(pid),
                "secure": attrs.get("AXSubrole") == "AXSecureTextField"}

    # -- acting ---------------------------------------------------------------------------
    async def press(self, handle: str, *, clicks: int = 1, button: str = "left") -> str:
        return await asyncio.to_thread(self._press, handle, clicks, button)

    def _press(self, handle: str, clicks: int, button: str) -> str:
        element, pid = self._resolve(handle)
        backend = self.backend
        attrs = backend.attributes(element, ax.ATTRIBUTES)
        name = ax.label_for(attrs) or ax.ROLE_NAMES.get(str(attrs.get("AXRole")), "control")
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
        element, pid = self._resolve(handle)
        backend = self.backend
        attrs = backend.attributes(element, ax.ATTRIBUTES)
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
        self._require()
        await asyncio.to_thread(self.input.press, stroke, repeat)

    async def choose_option(self, handle: str, option: str) -> str:
        return await asyncio.to_thread(self._choose_option, handle, option)

    def _choose_option(self, handle: str, option: str) -> str:
        element, pid = self._resolve(handle)
        backend = self.backend
        name = ax.label_for(backend.attributes(element, ax.ATTRIBUTES)) or "the menu"
        items = self._menu_items(element)
        opened = False
        if not items:
            backend.perform(element, "AXPress")
            opened = True
            self._sleep(0.3)
            items = self._menu_items(element)
        chosen = _match(items, option, backend)
        if chosen is None:
            self._dismiss(element, opened)
            titles = [t for t in (_title(backend, i) for i in items) if t]
            raise NativeError(f"“{name}” has no option “{option}”"
                              + (f" — it has: {', '.join(titles[:15])}." if titles else "."))
        if not backend.perform(chosen, "AXPress"):
            self._dismiss(element, opened)
            raise NativeError(f"I couldn't choose “{option}”.")
        return f"Chose “{_title(backend, chosen)}” in “{name}”."

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
                backend.perform(current, "AXPress")        # some menus fill in when opened
                opened_root = current
                self._sleep(0.3)
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

    async def drag(self, source: str, target: str) -> str:
        return await asyncio.to_thread(self._drag, source, target)

    def _drag(self, source: str, target: str) -> str:
        start, pid = self._resolve(source)
        end, _ = self._resolve(target)
        backend = self.backend
        a = backend.attributes(start, ax.ATTRIBUTES)
        b = backend.attributes(end, ax.ATTRIBUTES)
        fa, fb = ax.frame_of(a), ax.frame_of(b)
        if fa is None or fb is None:
            raise NativeError("One of those has no position on screen to drag from or to.")
        self._front(pid)
        self.input.drag(fa.center, fb.center)
        return f"Dragged “{ax.label_for(a)}” onto “{ax.label_for(b)}”."

    async def scroll_to(self, handle: str) -> None:
        """Put the pointer over *handle*, so the next scroll goes to it."""
        await asyncio.to_thread(self._scroll_to, handle)

    def _scroll_to(self, handle: str) -> None:
        element, pid = self._resolve(handle)
        frame = ax.frame_of(self.backend.attributes(element, ("AXPosition", "AXSize")))
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
        if snap.frame is None or window_number is None:
            raise NativeError(f"I couldn't find {snap.app}'s window on screen to look at.")
        path = await capture(snap.pid, window_number)
        if recognize is None:
            from .marks import recognize_text as recognize
        texts = await asyncio.to_thread(recognize, str(path))
        size = await asyncio.to_thread(image_size, path)
        marks = build_marks(texts, snap.controls, size, snap.frame)
        self._marks, self._marks_pid = marks, snap.pid
        overlay = None
        if overlay_dir is not None:
            overlay = await asyncio.to_thread(draw_overlay, path, marks, snap.frame,
                                              Path(overlay_dir) / (Path(path).stem + "-marks.png"))
        self._overlay = overlay
        return marks, render_marks(marks, app=snap.app, title=snap.title), overlay

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

    # -- plumbing --------------------------------------------------------------------------------
    def _target(self, app: str) -> tuple[int, str]:
        backend = self.backend
        found = backend.find_app(app) if app.strip() else backend.frontmost()
        if found is None:
            raise NativeError(f"{app} doesn't appear to be running." if app.strip()
                              else "I couldn't tell which app is in front.", wrong_tool=bool(app.strip()))
        self.last_app = found[1]
        return found

    def _front(self, pid: int) -> None:
        front = self.backend.frontmost()
        if front is None or front[0] != pid:
            self.backend.activate(pid)
            self._sleep(0.25)

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

    def _dismiss(self, element: Any, opened: bool) -> None:
        if not opened or element is None:
            return
        for child in list(self.backend.attribute(element, "AXChildren") or []):
            if self.backend.attribute(child, "AXRole") == "AXMenu":
                self.backend.perform(child, "AXCancel")
                return
        self.input.press(resolve_key("escape"))

    def _handle_for(self, element: Any, pid: int) -> str:
        backend = self.backend
        key = backend.key(element)
        for known, handle in self._by_key.get(key, []):
            if backend.same(known, element):
                return handle
        if self._counter >= MAX_HANDLES:
            self._handles.clear()
            self._by_key.clear()
            self._handle_app.clear()
            self._counter = 0
        self._counter += 1
        handle = f"ax{self._counter}"
        self._handles[handle] = element
        self._by_key.setdefault(key, []).append((element, handle))
        self._handle_app[handle] = pid
        return handle

    def _resolve(self, handle: str) -> tuple[Any, int]:
        self._require()
        cleaned = str(handle).strip().strip("[]").lower()
        element = self._handles.get(cleaned)
        if element is None:
            raise NativeError(f"There's no control [{cleaned}] — read_window to see the current ones.")
        attrs = self.backend.attributes(element, ("AXRole",))
        if not attrs.get("AXRole"):
            raise NativeError(f"[{cleaned}] isn't on screen any more — read_window again.")
        pid = self._handle_app.get(cleaned, 0)
        self.last_app = self._app_name(pid) or self.last_app
        return element, pid


def _title(backend: Any, element: Any) -> str:
    attrs = backend.attributes(element, ("AXTitle", "AXDescription", "AXValue"))
    for key in ("AXTitle", "AXDescription", "AXValue"):
        text = ax._text(attrs.get(key))
        if text:
            return text
    return ""


def _normal(text: str) -> str:
    text = " ".join(text.lower().replace("…", "...").split())
    return text.rstrip(".: ").strip()


def _match(elements: list[Any], wanted: str, backend: Any) -> Any:
    """The element titled *wanted*: exactly (ignoring case and a trailing
    "…"), else the only one that starts with it, else the only one that
    contains it."""
    target = _normal(wanted)
    if not target:
        return None
    titled = [(e, _normal(_title(backend, e))) for e in elements]
    exact = [e for e, t in titled if t == target]
    if exact:
        return exact[0]
    for test in (lambda t: t.startswith(target), lambda t: target in t):
        hits = [e for e, t in titled if t and test(t)]
        if len(hits) == 1:
            return hits[0]
    return None


def _is_dialog(backend: Any, window: Any) -> bool:
    attrs = backend.attributes(window, ("AXRole", "AXSubrole", "AXModal"))
    return (attrs.get("AXSubrole") in {"AXDialog", "AXSystemDialog", "AXAlert"}
            or bool(attrs.get("AXModal")))


__all__ = ["PERMISSION_HINT", "Frame", "NativeError", "NativeSurface"]
