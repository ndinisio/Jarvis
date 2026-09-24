"""What a native app's window shows, as a list a model can act on.

The same idea as the web page listing (``tools/browser/observe.py``), for
apps: every control in the front window — however deeply it is nested,
which is where the v2 AppleScript search never looked — with a short
``[axN]`` handle, its role, its label and its state::

    Window: “Shopping list” — Notes
    [ax3] button "New Note"
    [ax4] search field "Search" (focused)
    [ax5] row "Shopping list · milk, eggs, bread" (selected)
    Text on screen: 12 notes · Today
    Menus: Notes, File, Edit, Format, View, Window, Help — use choose_menu_item.

Everything here is pure logic over an :class:`AXBackend` — the macOS
Accessibility API in ``backend.py``, a fake in the tests. The traversal is
bounded (depth, nodes visited, time), big tables contribute only their
visible rows, rows are labelled by the text inside them, scroll bars and
layout containers are walked through but never listed, and a sheet or
alert — which blocks everything else — is listed first.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Protocol

#: Attributes read for every node, in one round trip where the backend can.
ATTRIBUTES = (
    "AXRole", "AXSubrole", "AXTitle", "AXDescription", "AXValue", "AXPlaceholderValue",
    "AXHelp", "AXEnabled", "AXFocused", "AXSelected", "AXPosition", "AXSize", "AXIdentifier",
)

#: AX role (and subrole) → the plain name the model sees. Shared vocabulary
#: with the web listing where the concept is the same.
ROLE_NAMES = {
    "AXButton": "button", "AXMenuButton": "menu button", "AXPopUpButton": "select",
    "AXCheckBox": "checkbox", "AXRadioButton": "radio", "AXTextField": "field",
    "AXTextArea": "text area", "AXComboBox": "combobox", "AXLink": "link",
    "AXSlider": "slider", "AXIncrementor": "stepper", "AXStepper": "stepper",
    "AXDisclosureTriangle": "disclosure", "AXRow": "row", "AXCell": "cell",
    "AXStaticText": "text", "AXImage": "image", "AXMenuItem": "menu item",
    "AXMenuBarItem": "menu", "AXColorWell": "color well", "AXDateField": "date field",
    "AXTimeField": "time field", "AXHeading": "heading", "AXSwitch": "switch",
    "AXToggle": "switch", "AXSegmentedControl": "segmented control",
}
SUBROLE_NAMES = {
    "AXSearchField": "search field", "AXSecureTextField": "password field",
    "AXTabButton": "tab", "AXOutlineRow": "row", "AXSwitch": "switch", "AXToggle": "switch",
    "AXSortButton": "button", "AXCloseButton": "close button",
    "AXMinimizeButton": "minimise button", "AXFullScreenButton": "full-screen button",
    "AXZoomButton": "zoom button", "AXToolbarButton": "button",
}

#: Listed with a handle: things you can act on.
ACTIONABLE = frozenset({
    "AXButton", "AXMenuButton", "AXPopUpButton", "AXCheckBox", "AXRadioButton",
    "AXTextField", "AXTextArea", "AXComboBox", "AXLink", "AXSlider", "AXIncrementor",
    "AXStepper", "AXDisclosureTriangle", "AXRow", "AXMenuItem", "AXColorWell",
    "AXDateField", "AXTimeField", "AXSwitch", "AXToggle",
})
#: Where typing goes.
EDITABLE = frozenset({"AXTextField", "AXTextArea", "AXComboBox", "AXDateField", "AXTimeField"})
#: Walked through, never listed: layout and plumbing.
SKIP = frozenset({"AXScrollBar", "AXGrowArea", "AXValueIndicator", "AXMenuBar", "AXRuler",
                  "AXSplitter", "AXHandle", "AXRelevanceIndicator", "AXLevelIndicator",
                  "AXBusyIndicator", "AXProgressIndicator"})
#: Collections whose visible rows are enough (a mailbox has thousands).
COLLECTIONS = frozenset({"AXTable", "AXOutline", "AXList", "AXBrowser", "AXGrid"})
#: Windows-within-the-window that block everything else until dealt with.
BLOCKING = frozenset({"AXSheet", "AXDialog", "AXSystemDialog", "AXAlert", "AXPopover"})

#: Bounds on one look.
MAX_DEPTH = 30
MAX_VISITED = 1500
MAX_LISTED = 80
MAX_TEXTS = 30
DEADLINE_S = 3.0


class AXBackend(Protocol):
    """What the snapshot needs from the platform. Elements are opaque."""

    def attributes(self, element: Any, names: tuple[str, ...]) -> dict[str, Any]: ...
    def attribute(self, element: Any, name: str) -> Any: ...
    def actions(self, element: Any) -> list[str]: ...


@dataclass
class Frame:
    x: float
    y: float
    w: float
    h: float

    @property
    def center(self) -> tuple[float, float]:
        return (self.x + self.w / 2, self.y + self.h / 2)

    @property
    def empty(self) -> bool:
        return self.w <= 0.5 or self.h <= 0.5

    def intersects(self, other: Frame) -> bool:
        return not (self.x + self.w <= other.x or other.x + other.w <= self.x
                    or self.y + self.h <= other.y or other.y + other.h <= self.y)

    def clip(self, other: Frame) -> Frame:
        x, y = max(self.x, other.x), max(self.y, other.y)
        right = min(self.x + self.w, other.x + other.w)
        bottom = min(self.y + self.h, other.y + other.h)
        return Frame(x, y, max(0.0, right - x), max(0.0, bottom - y))


@dataclass
class Control:
    """One listed element."""

    ref: Any
    ax_role: str
    subrole: str
    role: str
    label: str
    value: str = ""
    placeholder: str = ""
    enabled: bool = True
    focused: bool = False
    selected: bool = False
    checked: bool | None = None
    frame: Frame | None = None
    visible: bool = True
    in_blocker: bool = False
    identifier: str = ""
    handle: str = ""

    @property
    def secure(self) -> bool:
        return self.subrole == "AXSecureTextField"

    @property
    def editable(self) -> bool:
        return self.ax_role in EDITABLE and not self.secure

    def line(self) -> str:
        text = self.label.replace('"', "'")
        line = f'[{self.handle}] {self.role} "{text}"'
        if self.value and self.value != self.label and self.role not in {"checkbox", "radio",
                                                                         "switch", "tab"}:
            line += f' value="{self.value[:80]}"'
        if self.placeholder and not self.value and self.placeholder != self.label:
            line += f' placeholder="{self.placeholder[:60]}"'
        if self.checked is not None:
            line += " (checked)" if self.checked else " (unchecked)"
        if self.selected and self.checked is None:
            line += " (selected)"
        if self.focused:
            line += " (focused)"
        if not self.enabled:
            line += " (disabled)"
        if not self.visible:
            line += " (off screen)"
        return line


@dataclass
class WindowSnapshot:
    app: str
    pid: int
    title: str
    frame: Frame | None
    controls: list[Control] = field(default_factory=list)
    texts: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    menus: list[str] = field(default_factory=list)
    visited: int = 0
    truncated: bool = False
    total: int = 0

    def lines(self) -> list[str]:
        return [control.line() for control in self.controls]


def label_for(attrs: dict[str, Any], texts_inside: list[str] | None = None,
              title_element_text: str = "") -> str:
    """The name a person would use for this control."""
    for key in ("AXTitle", "AXDescription"):
        value = _text(attrs.get(key))
        if value:
            return value
    if title_element_text:
        return title_element_text
    role = attrs.get("AXRole")
    if role in {"AXStaticText", "AXHeading"} or (role not in EDITABLE and role not in {
            "AXCheckBox", "AXRadioButton", "AXSlider", "AXIncrementor", "AXSwitch", "AXToggle"}):
        value = _text(attrs.get("AXValue"))
        if value:
            return value
    if texts_inside:
        return " · ".join(texts_inside)[:120]
    return _text(attrs.get("AXHelp")) or _text(attrs.get("AXPlaceholderValue"))


def snapshot(backend: AXBackend, window: Any, *, app: str = "", pid: int = 0,
             max_listed: int = MAX_LISTED, offset: int = 0, deadline_s: float = DEADLINE_S,
             blockers: list[Any] | None = None) -> WindowSnapshot:
    """Walk *window* (and any sheet or dialog over it) into a listing.

    Controls inside a blocking sheet/dialog come first; then everything
    visible; then anything scrolled out of view, marked as such. *offset*
    skips that many controls, for reading further down a long window.
    """
    started = time.monotonic()
    window_attrs = backend.attributes(window, ATTRIBUTES)
    window_frame = frame_of(window_attrs)
    result = WindowSnapshot(app=app, pid=pid, title=_text(window_attrs.get("AXTitle")),
                            frame=window_frame)
    found: list[Control] = []
    texts: list[str] = []
    roots: list[tuple[Any, bool]] = [(window, False)] + [(b, True) for b in (blockers or [])]

    for root, root_blocks in roots:
        if root_blocks:
            result.blockers.append(_blocker_name(backend, root, backend.attributes(root, ATTRIBUTES)))
        # (element, depth, clip, inside a blocker, inside a listed row)
        stack: list[tuple[Any, int, Frame | None, bool, bool]] = [
            (child, 1, window_frame, root_blocks, False)
            for child in reversed(_children(backend, root, window_attrs if root is window else None))
        ]
        while stack:
            if result.visited >= MAX_VISITED or time.monotonic() - started > deadline_s:
                result.truncated = True
                break
            element, depth, clip, blocked, in_row = stack.pop()
            attrs = backend.attributes(element, ATTRIBUTES)
            result.visited += 1
            role = str(attrs.get("AXRole") or "")
            subrole = str(attrs.get("AXSubrole") or "")
            if role in SKIP:
                continue
            frame = frame_of(attrs)
            if frame is not None and frame.empty and role not in {"AXGroup", "AXLayoutArea"}:
                continue            # hidden
            if role in BLOCKING or subrole in BLOCKING:
                blocked = True
                result.blockers.append(_blocker_name(backend, element, attrs))
            visible = clip is None or frame is None or frame.intersects(clip)

            listed_row = False
            if role == "AXStaticText" or role == "AXHeading":
                text = label_for(attrs)
                if text and not in_row and len(texts) < MAX_TEXTS and visible:
                    texts.append(text[:100])
            elif _interesting(role, attrs, backend, element):
                inside = _texts_inside(backend, element) if role in {"AXRow", "AXCell"} else None
                title_text = ""
                if role in EDITABLE and not _text(attrs.get("AXTitle")) \
                        and not _text(attrs.get("AXDescription")):
                    title_text = _title_element_text(backend, element)
                control = Control(
                    ref=element, ax_role=role, subrole=subrole,
                    role=SUBROLE_NAMES.get(subrole) or ROLE_NAMES.get(role, role.removeprefix("AX").lower()),
                    label=label_for(attrs, inside, title_text),
                    value=_value_text(role, attrs),
                    placeholder=_text(attrs.get("AXPlaceholderValue")),
                    enabled=attrs.get("AXEnabled") is not False,
                    focused=bool(attrs.get("AXFocused")),
                    selected=bool(attrs.get("AXSelected")),
                    checked=_checked(role, subrole, attrs),
                    frame=frame, visible=visible, in_blocker=blocked,
                    identifier=_text(attrs.get("AXIdentifier")),
                )
                found.append(control)
                listed_row = role in {"AXRow", "AXCell"}

            if depth >= MAX_DEPTH:
                continue
            child_clip = clip
            if role in {"AXScrollArea", "AXWebArea"} and frame is not None and clip is not None:
                child_clip = clip.clip(frame)
            children = _children(backend, element, attrs)
            for child in reversed(children):
                stack.append((child, depth + 1, child_clip, blocked, in_row or listed_row))
        if result.truncated:
            break

    ordered = ([c for c in found if c.in_blocker] + [c for c in found if not c.in_blocker and c.visible]
               + [c for c in found if not c.in_blocker and not c.visible])
    result.total = len(ordered)
    result.controls = ordered[offset:offset + max_listed]
    if len(ordered) > offset + max_listed:
        result.truncated = True
    result.texts = texts
    return result


def render(snap: WindowSnapshot, *, changes: str = "", text_chars: int = 700) -> str:
    """The listing, as the model sees it."""
    head = f"Window: “{snap.title}” — {snap.app}" if snap.title else f"Window of {snap.app}"
    lines = [head]
    for blocker in snap.blockers:
        lines.append(f"Open {blocker} — deal with it first; it blocks the rest of the window.")
    if changes:
        lines.append(changes)
    shown = len(snap.controls)
    if snap.total > shown:
        lines.append(f"Showing {shown} of {snap.total} controls (read_window with an offset shows more).")
    lines.extend(snap.lines())
    if not snap.controls:
        lines.append("No controls were readable here. mark_screen reads the window's text from "
                     "a screenshot instead.")
    if snap.texts:
        text = " · ".join(snap.texts)
        lines.append(f"Text on screen: {text[:text_chars]}" + ("…" if len(text) > text_chars else ""))
    if snap.menus:
        lines.append("Menus: " + ", ".join(snap.menus) + " — use choose_menu_item.")
    return "\n".join(lines)


def describe_changes(before: WindowSnapshot | None, after: WindowSnapshot) -> str:
    """What's new since the last look at the same window, in one line."""
    if before is None or before.pid != after.pid or before.title != after.title:
        return ""
    new_blockers = [b for b in after.blockers if b not in before.blockers]
    old, new = set(_stable_lines(before)), set(_stable_lines(after))
    appeared, gone = len(new - old), len(old - new)
    parts = [f"a {b} opened" for b in new_blockers]
    if appeared or gone:
        parts.append(f"{appeared} new, {gone} gone")
    return ("New since the last look: " + "; ".join(parts) + ".") if parts else \
        "Nothing changed since the last look."


def frame_of(attrs: dict[str, Any]) -> Frame | None:
    position, size = attrs.get("AXPosition"), attrs.get("AXSize")
    if not position or not size:
        return None
    try:
        x, y = float(position[0]), float(position[1])
        w, h = float(size[0]), float(size[1])
    except (TypeError, ValueError, IndexError):
        return None
    return Frame(x, y, w, h)


# -- helpers -----------------------------------------------------------------------
def _interesting(role: str, attrs: dict[str, Any], backend: AXBackend, element: Any) -> bool:
    if role in ACTIONABLE:
        return True
    if role == "AXImage" and (_text(attrs.get("AXDescription")) or _text(attrs.get("AXTitle"))):
        return "AXPress" in _actions(backend, element)
    if role == "AXCell":
        return "AXPress" in _actions(backend, element)
    return False


def _children(backend: AXBackend, element: Any, attrs: dict[str, Any] | None) -> list[Any]:
    role = str((attrs or {}).get("AXRole") or "")
    if role in COLLECTIONS:
        rows = backend.attribute(element, "AXVisibleRows")
        if rows:
            return list(rows)
        visible = backend.attribute(element, "AXVisibleChildren")
        if visible:
            return list(visible)
    return list(backend.attribute(element, "AXChildren") or [])


def _texts_inside(backend: AXBackend, element: Any, limit: int = 3, budget: int = 14) -> list[str]:
    """The words shown inside a row or cell — its name, for the model."""
    found: list[str] = []
    queue = list(backend.attribute(element, "AXChildren") or [])
    while queue and budget > 0 and len(found) < limit:
        budget -= 1
        child = queue.pop(0)
        attrs = backend.attributes(child, ("AXRole", "AXValue", "AXTitle", "AXDescription"))
        if attrs.get("AXRole") in {"AXStaticText", "AXTextField", "AXHeading"}:
            text = _text(attrs.get("AXValue")) or _text(attrs.get("AXTitle"))
            if text and text not in found:
                found.append(text[:60])
        elif attrs.get("AXRole") == "AXImage":
            text = _text(attrs.get("AXDescription"))
            if text and text not in found:
                found.append(text[:40])
        else:
            queue.extend(list(backend.attribute(child, "AXChildren") or []))
    return found


def _title_element_text(backend: AXBackend, element: Any) -> str:
    """A field's label is often a separate text element, linked by AXTitleUIElement."""
    label = backend.attribute(element, "AXTitleUIElement")
    if label is None:
        return ""
    attrs = backend.attributes(label, ("AXValue", "AXTitle"))
    return _text(attrs.get("AXValue")) or _text(attrs.get("AXTitle"))


def _blocker_name(backend: AXBackend, element: Any, attrs: dict[str, Any]) -> str:
    """"sheet “Do you want to save…”", for the listing's warning line."""
    role, subrole = str(attrs.get("AXRole") or ""), str(attrs.get("AXSubrole") or "")
    kind = {"AXSheet": "sheet", "AXPopover": "popover"}.get(role) or \
        {"AXDialog": "dialog", "AXSystemDialog": "dialog", "AXAlert": "alert"}.get(subrole, "dialog")
    title = _text(attrs.get("AXTitle")) or _text(attrs.get("AXDescription")) or \
        " — ".join(_texts_inside(backend, element, limit=2, budget=20))
    return f"{kind} “{title[:100]}”" if title else kind


def _value_text(role: str, attrs: dict[str, Any]) -> str:
    if role in {"AXCheckBox", "AXRadioButton", "AXSwitch", "AXToggle"}:
        return ""
    value = attrs.get("AXValue")
    if isinstance(value, bool):
        return ""
    if isinstance(value, (int, float)) and role in {"AXSlider", "AXIncrementor", "AXStepper"}:
        return f"{value:g}"
    return _text(value)


def _checked(role: str, subrole: str, attrs: dict[str, Any]) -> bool | None:
    if role not in {"AXCheckBox", "AXSwitch", "AXToggle"} and not (
            role == "AXRadioButton" and subrole != "AXTabButton"):
        return None
    value = attrs.get("AXValue")
    try:
        return bool(int(value))
    except (TypeError, ValueError):
        return None


def _actions(backend: AXBackend, element: Any) -> list[str]:
    try:
        return list(backend.actions(element) or [])
    except Exception:
        return []


def _stable_lines(snap: WindowSnapshot) -> list[str]:
    """Listing lines without handles or focus, for comparing two looks."""
    out = []
    for control in snap.controls:
        out.append(f"{control.role}|{control.label}|{control.value}|{control.checked}|{control.selected}")
    return out


def _text(value: Any) -> str:
    if value is None or isinstance(value, bool):
        return ""
    if isinstance(value, (int, float)):
        return ""
    text = " ".join(str(value).split())
    return text
