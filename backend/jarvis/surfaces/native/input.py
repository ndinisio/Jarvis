"""Genuine keyboard and mouse input (Quartz ``CGEvent``).

The v2 native tools typed through System Events' ``keystroke``, which knows
a dozen named keys, mangles non-ASCII text, and can't click anything but a
control it can name. This posts real HID events instead:

* **Keys** — the full macOS virtual-key table (letters, digits, punctuation,
  F1–F20, forward delete, arrows, keypad Enter) with any chord of
  command/shift/option/control/fn.
* **Text** — Unicode typed as keyboard events carrying the characters
  themselves, so it's independent of the keyboard layout and handles "café",
  "£" and emoji; newlines become Return. Long text is pasted instead
  (the clipboard is saved first and put back after).
* **Mouse** — move, click, double-click, right-click and drag at a point in
  global screen coordinates, which is also what the Accessibility API
  reports element positions in. A move far enough to matter glides there
  in a few steps instead of teleporting — the destination is already known
  from the accessibility tree, so this costs a few extra events, not a
  recalculation.

Everything that decides *what* to post is plain Python and tested; the
posting itself goes through :class:`QuartzPoster`, a thin layer over
``Quartz`` imported lazily (``pyobjc-framework-Quartz``, the ``native``
extra), so this module imports anywhere.

**Verification note**: this container has no macOS, so the Quartz calls
are exercised against a recording fake, not a real event tap. The key codes
are Apple's documented ``kVK_*`` values (HIToolbox/Events.h).
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Any

#: macOS virtual key codes (HIToolbox kVK_*), by the names people use.
KEYCODES: dict[str, int] = {
    "a": 0x00, "s": 0x01, "d": 0x02, "f": 0x03, "h": 0x04, "g": 0x05, "z": 0x06, "x": 0x07,
    "c": 0x08, "v": 0x09, "b": 0x0B, "q": 0x0C, "w": 0x0D, "e": 0x0E, "r": 0x0F, "y": 0x10,
    "t": 0x11, "1": 0x12, "2": 0x13, "3": 0x14, "4": 0x15, "6": 0x16, "5": 0x17, "=": 0x18,
    "9": 0x19, "7": 0x1A, "-": 0x1B, "8": 0x1C, "0": 0x1D, "]": 0x1E, "o": 0x1F, "u": 0x20,
    "[": 0x21, "i": 0x22, "p": 0x23, "l": 0x25, "j": 0x26, "'": 0x27, "k": 0x28, ";": 0x29,
    "\\": 0x2A, ",": 0x2B, "/": 0x2C, "n": 0x2D, "m": 0x2E, ".": 0x2F, "`": 0x32,
    "return": 0x24, "enter": 0x24, "tab": 0x30, "space": 0x31, "delete": 0x33,
    "backspace": 0x33, "escape": 0x35, "esc": 0x35, "forwarddelete": 0x75, "del": 0x75,
    "home": 0x73, "end": 0x77, "pageup": 0x74, "pagedown": 0x79, "help": 0x72,
    "left": 0x7B, "right": 0x7C, "down": 0x7D, "up": 0x7E, "keypadenter": 0x4C,
    "f1": 0x7A, "f2": 0x78, "f3": 0x63, "f4": 0x76, "f5": 0x60, "f6": 0x61, "f7": 0x62,
    "f8": 0x64, "f9": 0x65, "f10": 0x6D, "f11": 0x67, "f12": 0x6F, "f13": 0x69, "f14": 0x6B,
    "f15": 0x71, "f16": 0x6A, "f17": 0x40, "f18": 0x4F, "f19": 0x50, "f20": 0x5A,
}
#: Other names for the same keys.
ALIASES = {
    "arrowleft": "left", "arrowright": "right", "arrowup": "up", "arrowdown": "down",
    "leftarrow": "left", "rightarrow": "right", "uparrow": "up", "downarrow": "down",
    "pgup": "pageup", "pgdn": "pagedown", "page up": "pageup", "page down": "pagedown",
    "forward delete": "forwarddelete", "fwddelete": "forwarddelete", "spacebar": "space",
    "minus": "-", "equals": "=", "plus": "+", "comma": ",", "period": ".", "dot": ".",
    "slash": "/", "backslash": "\\", "semicolon": ";", "quote": "'", "grave": "`",
    "backtick": "`", "leftbracket": "[", "rightbracket": "]", "ret": "return",
}
#: CGEventFlags masks.
MODIFIER_FLAGS = {
    "command": 1 << 20, "cmd": 1 << 20, "⌘": 1 << 20,
    "shift": 1 << 17, "⇧": 1 << 17,
    "option": 1 << 19, "alt": 1 << 19, "opt": 1 << 19, "⌥": 1 << 19,
    "control": 1 << 18, "ctrl": 1 << 18, "⌃": 1 << 18,
    "fn": 1 << 23, "function": 1 << 23,
}
#: A Unicode keyboard event carries at most 20 UTF-16 units.
MAX_UNITS_PER_EVENT = 20
#: Past this, pasting beats typing (and never drops characters).
PASTE_THRESHOLD = 200


@dataclass(frozen=True)
class Keystroke:
    keycode: int
    flags: int
    name: str


class UnknownKey(ValueError):
    pass


#: Characters typed with shift on a US layout, and the key underneath.
SHIFTED = {
    "!": "1", "@": "2", "#": "3", "$": "4", "%": "5", "^": "6", "&": "7", "*": "8", "(": "9",
    ")": "0", "_": "-", "+": "=", "{": "[", "}": "]", "|": "\\", ":": ";", '"': "'", "<": ",",
    ">": ".", "?": "/", "~": "`",
}


def resolve_key(key: str, modifiers: list[str] | tuple[str, ...] = ()) -> Keystroke:
    """"cmd+shift+s", or ("s", ["command", "shift"]), → the key code and flags.

    A capital letter or a shifted symbol ("?", "+") adds shift itself.
    """
    raw = str(key).strip()
    names = [str(m) for m in modifiers]
    if len(raw) > 1 and "+" in raw:
        head, _, tail = raw.rpartition("+")
        if not tail:                      # "cmd++": the key is "+" itself
            head, tail = head[:-1] if head.endswith("+") else head, "+"
        names += [part for part in head.split("+") if part.strip()]
        raw = tail.strip()
    base = raw if len(raw) == 1 else raw.lower()
    if len(base) > 1:
        base = ALIASES.get(base, ALIASES.get(base.replace(" ", ""), base.replace(" ", "")))
    flags = 0
    if len(base) == 1 and base.isalpha() and base.isupper():
        flags |= MODIFIER_FLAGS["shift"]
        base = base.lower()
    elif base in SHIFTED:
        flags |= MODIFIER_FLAGS["shift"]
        base = SHIFTED[base]
    if base not in KEYCODES:
        raise UnknownKey(raw)
    for name in names:
        mask = MODIFIER_FLAGS.get(name.strip().lower())
        if mask is None:
            raise UnknownKey(name)
        flags |= mask
    shown = "+".join([*(n.strip().lower() for n in names), raw.lower() if len(raw) > 1 else raw])
    return Keystroke(KEYCODES[base], flags, shown)


def text_chunks(text: str) -> list[str]:
    """Split *text* into pieces a Unicode key event can carry, with each
    newline its own piece (typed as Return) and no surrogate pair split."""
    pieces: list[str] = []
    for index, line in enumerate(text.replace("\r\n", "\n").split("\n")):
        if index:
            pieces.append("\n")
        current, units = "", 0
        for char in line:
            width = 2 if ord(char) > 0xFFFF else 1
            if units + width > MAX_UNITS_PER_EVENT:
                pieces.append(current)
                current, units = "", 0
            current += char
            units += width
        if current:
            pieces.append(current)
    return pieces


class QuartzPoster:
    """The only place that touches Quartz. Synchronous: call it from a thread."""

    def __init__(self, quartz: Any = None):
        self._quartz = quartz

    @property
    def q(self) -> Any:
        if self._quartz is None:
            import Quartz

            self._quartz = Quartz
        return self._quartz

    @staticmethod
    def available() -> bool:
        try:
            import Quartz  # noqa: F401
        except ImportError:
            return False
        return True

    def key(self, keycode: int, flags: int, down: bool) -> None:
        q = self.q
        event = q.CGEventCreateKeyboardEvent(None, keycode, down)
        q.CGEventSetFlags(event, flags)
        q.CGEventPost(q.kCGHIDEventTap, event)

    def unicode(self, text: str, down: bool) -> None:
        q = self.q
        event = q.CGEventCreateKeyboardEvent(None, 0, down)
        q.CGEventKeyboardSetUnicodeString(event, len(text.encode("utf-16-le")) // 2, text)
        q.CGEventPost(q.kCGHIDEventTap, event)

    def mouse(self, kind: str, x: float, y: float, button: str = "left", click_state: int = 1) -> None:
        q = self.q
        types = {
            ("down", "left"): getattr(q, "kCGEventLeftMouseDown", 1),
            ("up", "left"): getattr(q, "kCGEventLeftMouseUp", 2),
            ("down", "right"): getattr(q, "kCGEventRightMouseDown", 3),
            ("up", "right"): getattr(q, "kCGEventRightMouseUp", 4),
            ("move", "left"): getattr(q, "kCGEventMouseMoved", 5),
            ("move", "right"): getattr(q, "kCGEventMouseMoved", 5),
            ("drag", "left"): getattr(q, "kCGEventLeftMouseDragged", 6),
        }
        buttons = {"left": getattr(q, "kCGMouseButtonLeft", 0), "right": getattr(q, "kCGMouseButtonRight", 1)}
        event = q.CGEventCreateMouseEvent(None, types[(kind, button)], q.CGPointMake(x, y), buttons[button])
        if kind in {"down", "up"}:
            q.CGEventSetIntegerValueField(event, getattr(q, "kCGMouseEventClickState", 1), click_state)
        q.CGEventPost(q.kCGHIDEventTap, event)


#: Below this distance a glide is pointless — a few pixels reads as jitter,
#: not motion, so those moves stay an instant jump.
_GLIDE_MIN_DISTANCE = 8.0
_GLIDE_STEPS = 6
_GLIDE_STEP_DELAY = 0.006


def _glide_points(x0: float, y0: float, x1: float, y1: float,
                   steps: int = _GLIDE_STEPS) -> list[tuple[float, float]]:
    """*steps* points from just past ``(x0, y0)`` up to and including
    ``(x1, y1)`` — a straight-line sweep is enough to read as a real cursor
    move rather than a teleport; it doesn't need to be curved."""
    return [(x0 + (x1 - x0) * i / steps, y0 + (y1 - y0) * i / steps) for i in range(1, steps + 1)]


class NativeInput:
    """Keys, text and mouse gestures, built from single events."""

    def __init__(self, poster: Any = None, *, sleep=time.sleep):
        self.poster = poster or QuartzPoster()
        self._sleep = sleep
        #: Where the cursor last landed, so the next click can glide from
        #: there instead of teleporting. Unknown until the first move —
        #: querying the real OS position isn't worth it for one call.
        self._last_position: tuple[float, float] | None = None

    def press(self, stroke: Keystroke, repeat: int = 1) -> None:
        for _ in range(max(1, repeat)):
            self.poster.key(stroke.keycode, stroke.flags, True)
            self.poster.key(stroke.keycode, stroke.flags, False)
            self._sleep(0.02)

    def type_text(self, text: str) -> int:
        """Type *text*; returns how many key events it took."""
        events = 0
        for piece in text_chunks(text):
            if piece == "\n":
                self.press(Keystroke(KEYCODES["return"], 0, "return"))
            else:
                self.poster.unicode(piece, True)
                self.poster.unicode(piece, False)
                self._sleep(0.012)
            events += 1
        return events

    def click(self, x: float, y: float, *, button: str = "left", clicks: int = 1) -> None:
        self._glide_to(x, y, button)
        self._sleep(0.03)
        for state in range(1, max(1, clicks) + 1):
            self.poster.mouse("down", x, y, button, state)
            self.poster.mouse("up", x, y, button, state)
            self._sleep(0.04)

    def move(self, x: float, y: float) -> None:
        self._glide_to(x, y)

    def _glide_to(self, x: float, y: float, button: str = "left") -> None:
        last = self._last_position
        if last is not None and math.hypot(x - last[0], y - last[1]) >= _GLIDE_MIN_DISTANCE:
            for px, py in _glide_points(last[0], last[1], x, y):
                self.poster.mouse("move", px, py, button)
                self._sleep(_GLIDE_STEP_DELAY)
        else:
            self.poster.mouse("move", x, y, button)
        self._last_position = (x, y)

    def drag(self, start: tuple[float, float], end: tuple[float, float], steps: int = 12) -> None:
        (x0, y0), (x1, y1) = start, end
        self._glide_to(x0, y0)
        self._sleep(0.05)
        self.poster.mouse("down", x0, y0, "left", 1)
        self._sleep(0.08)
        for step in range(1, steps + 1):
            t = step / steps
            self.poster.mouse("drag", x0 + (x1 - x0) * t, y0 + (y1 - y0) * t)
            self._sleep(0.02)
        self._sleep(0.08)
        self.poster.mouse("up", x1, y1, "left", 1)


class Clipboard:
    """Save, replace and restore the general pasteboard — every item and
    type on it, not just text, so pasting never costs the user an image
    they had copied. Uses AppKit (``pyobjc-framework-Cocoa``)."""

    def __init__(self, appkit: Any = None):
        self._appkit = appkit

    @property
    def appkit(self) -> Any:
        if self._appkit is None:
            import AppKit

            self._appkit = AppKit
        return self._appkit

    def save(self) -> list[dict[str, Any]]:
        board = self.appkit.NSPasteboard.generalPasteboard()
        saved: list[dict[str, Any]] = []
        for item in board.pasteboardItems() or []:
            entry = {}
            for kind in item.types() or []:
                data = item.dataForType_(kind)
                if data is not None:
                    entry[kind] = data
            if entry:
                saved.append(entry)
        return saved

    def set_text(self, text: str) -> None:
        board = self.appkit.NSPasteboard.generalPasteboard()
        board.clearContents()
        board.setString_forType_(text, self.appkit.NSPasteboardTypeString)

    def restore(self, saved: list[dict[str, Any]]) -> None:
        board = self.appkit.NSPasteboard.generalPasteboard()
        board.clearContents()
        items = []
        for entry in saved:
            item = self.appkit.NSPasteboardItem.alloc().init()
            for kind, data in entry.items():
                item.setData_forType_(data, kind)
            items.append(item)
        if items:
            board.writeObjects_(items)
