"""Mac apps as a surface: the Accessibility snapshot, acting on handles,
menus, genuine input, numbered marks — and the safety rules around them.

The platform is faked at the one seam the surface has (``AXBackend``): an
accessibility tree built from plain objects, and an input recorder standing
in for Quartz. What's tested is everything JARVIS decides — what's listed
and how, which element a handle means, what gets pressed, typed or refused.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from types import SimpleNamespace
from typing import Any

import pytest
from jarvis.intelligence.operator import Budget, Operator
from jarvis.security import consequence
from jarvis.surfaces.native import PERMISSION_HINT, NativeError, NativeSurface
from jarvis.surfaces.native import ax as axmod
from jarvis.surfaces.native.input import Keystroke, NativeInput, resolve_key, text_chunks
from jarvis.surfaces.native.marks import (
    Mark,
    TextBox,
    build_marks,
    draw_overlay,
    from_normalised,
    parse_captions,
    parse_pick,
    render_marks,
    to_points,
)
from jarvis.tools.base import ToolResult
from jarvis.tools.native.tools import MarkScreenTool


# ---------------------------------------------------------------------------
# a fake accessibility tree
# ---------------------------------------------------------------------------
class El:
    def __init__(self, role: str, title: str = "", *, subrole: str = "", description: str = "",
                 value: Any = None, placeholder: str = "", enabled: bool = True,
                 focused: bool = False, selected: bool = False, frame=(10, 10, 80, 20),
                 actions=("AXPress",), children=(), settable: bool = True, **extra):
        self.attrs: dict[str, Any] = {
            "AXRole": role, "AXSubrole": subrole, "AXTitle": title, "AXDescription": description,
            "AXValue": value, "AXPlaceholderValue": placeholder, "AXEnabled": enabled,
            "AXFocused": focused, "AXSelected": selected,
            "AXPosition": (frame[0], frame[1]) if frame else None,
            "AXSize": (frame[2], frame[3]) if frame else None,
        }
        self.attrs.update(extra)
        self.children = list(children)
        self.actions = list(actions)
        self.performed: list[str] = []
        self.settable = settable
        self.alive = True
        self.on_perform = None

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"El({self.attrs['AXRole']}, {self.attrs['AXTitle']!r})"


class FakeBackend:
    def __init__(self, apps: dict[str, tuple[int, El]], front: str):
        self.apps = apps
        self.front = front
        self.is_trusted = True
        self.prompted = 0
        self.activations: list[int] = []
        self.sets: list[tuple[El, str, Any]] = []

    def trusted(self, prompt: bool = False) -> bool:
        if prompt:
            self.prompted += 1
        return self.is_trusted

    def frontmost(self):
        pid, _ = self.apps[self.front]
        return pid, self.front

    def find_app(self, name: str):
        for app_name, (pid, _) in self.apps.items():
            if app_name.lower() == name.strip().lower():
                return pid, app_name
        return None

    def activate(self, pid: int) -> bool:
        self.activations.append(pid)
        self.front = next(n for n, (p, _) in self.apps.items() if p == pid)
        return True

    def application(self, pid: int) -> El:
        return next(app for p, app in self.apps.values() if p == pid)

    def windows(self, app: El) -> list[El]:
        return list(app.attrs.get("AXWindows") or [])

    def front_window(self, app: El):
        return app.attrs.get("AXFocusedWindow") or next(iter(self.windows(app)), None)

    def focused_element(self, app: El):
        return app.attrs.get("AXFocusedUIElement")

    def window_number(self, pid: int, title: str = ""):
        return 4242

    def attribute(self, element: El | None, name: str):
        if element is None or not element.alive:
            return None
        if name == "AXChildren":
            return list(element.children)
        return element.attrs.get(name)

    def attributes(self, element: El | None, names) -> dict[str, Any]:
        return {name: self.attribute(element, name) for name in names}

    def actions(self, element: El) -> list[str]:
        return list(element.actions)

    def perform(self, element: El, action: str) -> bool:
        if action not in element.actions:
            return False
        element.performed.append(action)
        if element.on_perform:
            element.on_perform(action)
        return True

    def set_attribute(self, element: El, name: str, value: Any) -> bool:
        self.sets.append((element, name, value))
        if not element.settable:
            return False
        element.attrs[name] = value
        return True

    def key(self, element: El) -> int:
        return id(element)

    def same(self, a: El, b: El) -> bool:
        return a is b


class RecordingInput:
    def __init__(self):
        self.events: list[tuple] = []

    def press(self, stroke: Keystroke, repeat: int = 1) -> None:
        self.events.append(("key", stroke.name, repeat))

    def type_text(self, text: str) -> int:
        self.events.append(("type", text))
        return 1

    def click(self, x: float, y: float, *, button: str = "left", clicks: int = 1) -> None:
        self.events.append(("click", round(x), round(y), button, clicks))

    def move(self, x: float, y: float) -> None:
        self.events.append(("move", round(x), round(y)))

    def drag(self, start, end, steps: int = 12) -> None:
        self.events.append(("drag", tuple(round(v) for v in start), tuple(round(v) for v in end)))


def _notes_app():
    """A Notes-like window: controls nested four deep, a note list whose rows
    are named by the text inside them, a scroll bar, and a hidden control."""
    search = El("AXTextField", subrole="AXSearchField", placeholder="Search", frame=(700, 60, 180, 22))
    new_note = El("AXButton", "New Note", frame=(640, 60, 30, 22))
    rows = [El("AXRow", actions=(), frame=(20, 100 + 30 * i, 200, 28), selected=(i == 0), children=[
        El("AXCell", actions=(), frame=(20, 100 + 30 * i, 200, 28), children=[
            El("AXStaticText", value=title, actions=(), frame=(24, 102 + 30 * i, 150, 12)),
            El("AXStaticText", value=preview, actions=(), frame=(24, 114 + 30 * i, 150, 12)),
        ])]) for i, (title, preview) in enumerate([("Shopping list", "milk, eggs"),
                                                    ("Holiday ideas", "Lisbon"),
                                                    ("Recipes", "risotto")])]
    table = El("AXTable", actions=(), frame=(20, 100, 200, 400), children=rows,
               AXVisibleRows=rows)
    body = El("AXTextArea", description="Note body", value="milk, eggs, bread", frame=(240, 100, 500, 400))
    bold = El("AXCheckBox", "Bold", value=1, frame=(300, 60, 24, 22))
    hidden = El("AXButton", "Secret", frame=(0, 0, 0, 0))
    scrollbar = El("AXScrollBar", frame=(220, 100, 10, 400), children=[El("AXButton", "increment")])
    toolbar = El("AXToolbar", actions=(), frame=(0, 50, 900, 40), children=[
        El("AXGroup", actions=(), children=[El("AXGroup", actions=(), children=[new_note, bold])]),
        search])
    split = El("AXSplitGroup", actions=(), frame=(0, 90, 900, 450), children=[
        El("AXScrollArea", actions=(), frame=(20, 100, 210, 400), children=[table, scrollbar]),
        El("AXScrollArea", actions=(), frame=(240, 100, 500, 400), children=[body]),
        hidden, El("AXStaticText", value="3 notes", actions=(), frame=(20, 520, 60, 14))])
    window = El("AXWindow", "Shopping list", actions=(), frame=(0, 0, 900, 560),
                children=[toolbar, split])
    export = El("AXMenuItem", "Export as PDF…", actions=("AXPress",))
    greyed = El("AXMenuItem", "Print…", enabled=False)
    file_menu = El("AXMenuBarItem", "File", children=[El("AXMenu", actions=(), children=[
        El("AXMenuItem", "New Note"), El("AXMenuItem", ""), export, greyed])])
    menubar = El("AXMenuBar", actions=(), children=[
        El("AXMenuBarItem", "Apple"), El("AXMenuBarItem", "Notes"), file_menu,
        El("AXMenuBarItem", "Edit")])
    app = El("AXApplication", "Notes", actions=(), AXWindows=[window], AXFocusedWindow=window,
             AXMenuBar=menubar)
    return app, {"search": search, "new_note": new_note, "rows": rows, "body": body, "bold": bold,
                 "window": window, "export": export, "greyed": greyed, "file": file_menu,
                 "split": split, "table": table}


@pytest.fixture
def notes():
    app, parts = _notes_app()
    finder = El("AXApplication", "Finder", actions=(), AXWindows=[], AXMenuBar=El("AXMenuBar", actions=()))
    backend = FakeBackend({"Notes": (101, app), "Finder": (202, finder)}, front="Notes")
    recorder = RecordingInput()
    surface = NativeSurface(backend=backend, input=recorder, sleep=lambda _s: None)
    return surface, backend, recorder, parts


# ---------------------------------------------------------------------------
# seeing
# ---------------------------------------------------------------------------
async def test_controls_nested_deep_in_the_window_are_listed(notes):
    """The v2 AppleScript search looked at a window's direct children only;
    "New Note" here is four containers down."""
    surface, _, _, _ = notes
    snap, listing = await surface.read()
    assert '] button "New Note"' in listing
    assert '] search field "Search"' in listing or 'search field "' in listing
    assert '] checkbox "Bold" (checked)' in listing
    assert '] text area "Note body" value="milk, eggs, bread"' in listing
    assert "Window: “Shopping list” — Notes" in listing
    assert "Menus: Notes, File, Edit — use choose_menu_item." in listing


async def test_rows_are_named_by_what_they_show_and_plumbing_is_left_out(notes):
    surface, _, _, _ = notes
    _, listing = await surface.read()
    assert '] row "Shopping list · milk, eggs" (selected)' in listing
    assert '] row "Holiday ideas · Lisbon"' in listing
    assert "Text on screen: 3 notes" in listing, "free-standing text is summarised, not handled"
    assert "increment" not in listing, "scroll bars are never listed"
    assert "Secret" not in listing, "zero-size (hidden) controls are skipped"
    assert listing.count("Shopping list · milk") == 1


async def test_a_long_table_contributes_only_its_visible_rows(notes):
    surface, _, _, parts = notes
    extra = [axmod_row(f"Old note {n}") for n in range(500)]
    parts["table"].children.extend(extra)          # all rows exist…
    _, listing = await surface.read()               # …but only the visible ones are read
    assert "Old note" not in listing


def axmod_row(title: str):
    return El("AXRow", actions=(), children=[El("AXStaticText", value=title, actions=())])


async def test_a_sheet_is_listed_first_and_called_out(notes):
    surface, _, _, parts = notes
    sheet = El("AXSheet", actions=(), frame=(200, 40, 400, 150), children=[
        El("AXStaticText", value="Do you want to keep this note?", actions=()),
        El("AXButton", "Delete", frame=(220, 150, 80, 22)), El("AXButton", "Keep", frame=(320, 150, 80, 22))])
    parts["window"].children.append(sheet)
    snap, listing = await surface.read()
    lines = listing.splitlines()
    assert lines[1].startswith("Open sheet “Do you want to keep this note?”")
    assert snap.controls[0].label == "Delete" and snap.controls[1].label == "Keep"


# What a Mac actually reports while TextEdit's save sheet is up (seen on macOS 27 with
# scripts/check_native.py --act): the sheet is *the app's focused window*, not a child of
# a focused window; the window it hangs from is the one entry in AXWindows. The fake tree
# above had the window focused with the sheet inside it, which is not what macOS says.
def _save_sheet_app(*, in_window_children: bool = True, nested: bool = False, parent: bool = True,
                    focus: str = "sheet", sheet_frame="normal"):
    delete = El("AXButton", "Delete", AXIdentifier="DontSaveButton", frame=(260, 540, 80, 24))
    cancel = El("AXButton", "Cancel", AXIdentifier="CancelButton", frame=(350, 540, 80, 24))
    save = El("AXButton", "Save", AXIdentifier="OKButton", frame=(440, 540, 80, 24))
    name = El("AXTextField", description="Save As", value="Untitled", frame=(260, 300, 260, 22))
    sheet = El("AXSheet", description="save", AXIdentifier="save-panel", actions=(), frame=(200, 280, 400, 300),
               children=[El("AXSplitGroup", actions=(), frame=(200, 320, 400, 200),
                            children=[El("AXStaticText", value="Where", actions=(), frame=(210, 330, 60, 14))]),
                         name, delete, cancel, save])
    body = El("AXTextArea", description="document", value="Hello from JARVIS", frame=(20, 60, 560, 400))
    close = El("AXButton", subrole="AXCloseButton", description="close button", frame=(8, 4, 14, 14))
    window = El("AXWindow", "Untitled", subrole="AXStandardWindow", AXIdentifier="_NS:31", actions=(),
                frame=(100, 100, 600, 500), children=[close, body, *([sheet] if in_window_children else [])])
    focused = sheet
    if sheet_frame == "missing":
        sheet.attrs["AXPosition"] = sheet.attrs["AXSize"] = None
    elif sheet_frame == "zero":
        sheet.attrs["AXSize"] = (0.0, 0.0)
    elif sheet_frame == "flat":
        sheet.attrs["AXSize"] = (400.0, 0.0)
    if nested:
        confirm = El("AXButton", "Replace", frame=(300, 400, 80, 24))
        inner = El("AXSheet", description="alert", actions=(), frame=(250, 350, 300, 120), children=[confirm])
        sheet.children.append(inner)
        inner.attrs["AXParent"] = sheet
        focused = inner
    if parent:
        sheet.attrs["AXParent"] = window
    if focus == "window":
        focused = window
    app = El("AXApplication", "TextEdit", actions=(), AXWindows=[window], AXFocusedWindow=focused,
             AXMenuBar=El("AXMenuBar", actions=()))
    return app, {"window": window, "sheet": sheet, "delete": delete, "cancel": cancel, "save": save,
                 "body": body, "close": close}


def _on_save_sheet(**kwargs):
    app, parts = _save_sheet_app(**kwargs)
    backend = FakeBackend({"TextEdit": (303, app)}, front="TextEdit")
    return NativeSurface(backend=backend, input=RecordingInput(), sleep=lambda _s: None), backend, parts


async def test_a_sheet_the_app_reports_as_its_focused_window_is_read_as_a_sheet_over_its_window():
    surface, _, parts = _on_save_sheet()
    snap, listing = await surface.read("TextEdit")
    assert snap.title == "Untitled", "the window the sheet hangs from, not the sheet"
    assert snap.blockers == ["sheet “save”"]
    assert [c.label for c in snap.controls[:4]] == ["Save As", "Delete", "Cancel", "Save"] or \
        [c.label for c in snap.controls[:3]] == ["Delete", "Cancel", "Save"]
    assert all(c.in_blocker for c in snap.controls[:4] if c.label in {"Delete", "Cancel", "Save"})
    behind = [c for c in snap.controls if not c.in_blocker]
    assert any(c.ax_role == "AXTextArea" for c in behind), "the window under the sheet is still listed, after it"
    assert listing.splitlines()[1].startswith("Open sheet “save” — deal with it first")


async def test_the_identifiers_of_a_sheets_buttons_come_through_with_their_labels():
    surface, _, _ = _on_save_sheet()
    snap, _ = await surface.read("TextEdit")
    by_label = {c.label: c.identifier for c in snap.controls if c.in_blocker}
    assert by_label["Delete"] == "DontSaveButton"
    assert by_label["Cancel"] == "CancelButton"
    assert by_label["Save"] == "OKButton"


async def test_a_sheet_is_counted_once_whether_or_not_the_window_lists_it_among_its_children():
    for listed in (True, False):
        surface, _, _ = _on_save_sheet(in_window_children=listed)
        snap, _ = await surface.read("TextEdit")
        assert snap.blockers == ["sheet “save”"], f"in_window_children={listed}"
        assert [c.label for c in snap.controls if c.in_blocker].count("Delete") == 1


async def test_pressing_a_button_on_the_sheet_presses_that_button():
    surface, _, parts = _on_save_sheet()
    snap, _ = await surface.read("TextEdit")
    delete = next(c for c in snap.controls if c.identifier == "DontSaveButton")
    await surface.press(delete.handle)
    assert parts["delete"].performed == ["AXPress"] and parts["save"].performed == []


async def test_a_sheet_over_a_sheet_is_read_over_the_window_with_both_listed_first():
    surface, _, _ = _on_save_sheet(nested=True)
    snap, _ = await surface.read("TextEdit")
    assert snap.title == "Untitled"
    assert [b.split(" ")[0] for b in snap.blockers] == ["sheet", "sheet"]
    labels = [c.label for c in snap.controls if c.in_blocker]
    assert {"Delete", "Cancel", "Save", "Replace"} <= set(labels)


async def test_a_sheet_whose_window_cannot_be_found_is_still_read_not_lost():
    surface, _, _ = _on_save_sheet(parent=False)
    snap, _ = await surface.read("TextEdit")
    assert {"Delete", "Cancel", "Save"} <= {c.label for c in snap.controls}


@pytest.mark.parametrize("focus", ["sheet", "window"])
@pytest.mark.parametrize("sheet_frame", ["normal", "missing", "zero", "flat"])
async def test_the_real_save_sheet_is_a_blocker_whichever_window_is_focused_and_whatever_frame_it_reports(
        focus, sheet_frame):
    """AXWindow > AXSheet desc="save" > Delete / Cancel / Save, as a Mac reports it: the sheet
    directly under the window, and either one of them the app's focused window. A sheet that
    reports no area is still a sheet — it used to be skipped as a hidden element."""
    surface, _, _ = _on_save_sheet(focus=focus, sheet_frame=sheet_frame)
    snap, listing = await surface.read("TextEdit")
    assert snap.title == "Untitled"
    assert snap.blockers == ["sheet “save”"]
    blocked = [c for c in snap.controls if c.in_blocker]
    assert {"Delete", "Cancel", "Save"} <= {c.label for c in blocked}
    assert snap.controls[:len(blocked)] == blocked, "the sheet's controls come first"
    assert {c.identifier for c in blocked} >= {"DontSaveButton", "CancelButton", "OKButton"}
    assert listing.splitlines()[1].startswith("Open sheet “save” — deal with it first")


async def test_a_zero_sized_group_is_still_skipped_but_a_zero_sized_sheet_is_not():
    """The hidden-element rule stays for what it is for."""
    surface, _, parts = _on_save_sheet(focus="window", sheet_frame="zero")
    parts["window"].children.append(El("AXButton", "Ghost", frame=(0, 0, 0, 0)))
    snap, _ = await surface.read("TextEdit")
    assert "Ghost" not in {c.label for c in snap.controls}
    assert snap.blockers == ["sheet “save”"]


async def test_a_sheet_that_hangs_from_the_application_not_a_window_is_read_as_what_it_is():
    """Only a window (or another sheet) is something a sheet can be followed up to."""
    ok = El("AXButton", "OK", frame=(20, 80, 60, 24))
    sheet = El("AXSheet", description="alert", actions=(), frame=(100, 100, 300, 150), children=[ok])
    app = El("AXApplication", "Tool", actions=(), AXWindows=[], AXFocusedWindow=sheet,
             AXMenuBar=El("AXMenuBar", actions=()))
    sheet.attrs["AXParent"] = app
    surface = NativeSurface(backend=FakeBackend({"Tool": (404, app)}, front="Tool"), input=RecordingInput(),
                            sleep=lambda _s: None)
    snap, _ = await surface.read("Tool")
    assert [c.label for c in snap.controls] == ["OK"] and snap.frame.w == 300


async def test_a_dialog_window_that_is_the_focused_window_is_still_just_the_window():
    """Only a sheet hangs from another window; a dialog of its own is read as the window it is."""
    ok = El("AXButton", "OK", frame=(20, 80, 60, 24))
    dialog = El("AXWindow", "Alert", subrole="AXDialog", actions=(), frame=(100, 100, 300, 150), children=[ok])
    app = El("AXApplication", "Tool", actions=(), AXWindows=[dialog], AXFocusedWindow=dialog,
             AXMenuBar=El("AXMenuBar", actions=()))
    dialog.attrs["AXParent"] = app
    surface = NativeSurface(backend=FakeBackend({"Tool": (404, app)}, front="Tool"), input=RecordingInput(),
                            sleep=lambda _s: None)
    snap, _ = await surface.read("Tool")
    assert snap.title == "Alert" and snap.blockers == [] and [c.label for c in snap.controls] == ["OK"]


async def test_handles_stay_the_same_for_the_same_control(notes):
    surface, _, _, parts = notes
    first, _ = await surface.read()
    parts["split"].children.append(El("AXButton", "Share", frame=(800, 60, 30, 22)))
    second, listing = await surface.read()
    handle_of = {c.label: c.handle for c in first.controls}
    assert all(handle_of[c.label] == c.handle for c in second.controls if c.label in handle_of)
    share = next(c for c in second.controls if c.label == "Share")
    assert share.handle not in handle_of.values()
    assert "New since the last look: 1 new, 0 gone." in listing


async def test_a_second_look_says_when_nothing_changed(notes):
    surface, _, _, _ = notes
    await surface.read()
    _, listing = await surface.read()
    assert "Nothing changed since the last look." in listing


async def test_a_long_window_is_read_in_pages(notes):
    surface, _, _, parts = notes
    parts["split"].children.extend(El("AXButton", f"Tag {n}", frame=(10 + n, 530, 20, 20))
                                   for n in range(100))
    snap, listing = await surface.read()
    assert len(snap.controls) == axmod.MAX_LISTED and snap.total > axmod.MAX_LISTED
    assert f"Showing {axmod.MAX_LISTED} of {snap.total} controls" in listing
    more, _ = await surface.read(offset=axmod.MAX_LISTED)
    assert len(more.controls) == snap.total - axmod.MAX_LISTED


async def test_a_password_field_is_named_as_one(notes):
    surface, _, _, parts = notes
    parts["split"].children.append(El("AXTextField", subrole="AXSecureTextField", title="Password",
                                      frame=(300, 530, 100, 20)))
    _, listing = await surface.read()
    assert '] password field "Password"' in listing


async def test_without_accessibility_it_asks_once_and_says_how_to_fix_it(notes):
    surface, backend, _, _ = notes
    backend.is_trusted = False
    for _ in range(2):
        with pytest.raises(NativeError) as raised:
            await surface.read()
        assert raised.value.message == PERMISSION_HINT
    assert backend.prompted == 1, "the system prompt is shown once, not on every call"


# ---------------------------------------------------------------------------
# acting
# ---------------------------------------------------------------------------
async def _handle(surface, label: str) -> str:
    snap, _ = await surface.read()
    return next(c.handle for c in snap.controls if c.label == label)


async def test_pressing_uses_the_accessibility_action_not_the_mouse(notes):
    surface, _, recorder, parts = notes
    summary = await surface.press(await _handle(surface, "New Note"))
    assert parts["new_note"].performed == ["AXPress"]
    assert recorder.events == [] and summary == "Pressed “New Note”."


async def test_a_control_without_a_press_action_is_clicked_at_its_centre(notes):
    surface, backend, recorder, parts = notes
    handle = await _handle(surface, "New Note")
    backend.front = "Finder"
    parts["new_note"].actions = []
    await surface.press(handle)
    assert backend.activations == [101], "its app is brought to the front first"
    assert recorder.events == [("click", 655, 71, "left", 1)]


async def test_double_click_and_right_click_are_real_mouse_gestures(notes):
    surface, _, recorder, _ = notes
    handle = await _handle(surface, "Holiday ideas · Lisbon")
    summary = await surface.press(handle, clicks=2)
    assert recorder.events[-1] == ("click", 120, 144, "left", 2) and summary.startswith("Double-clicked")


async def test_a_greyed_out_control_is_reported_not_clicked(notes):
    surface, _, recorder, parts = notes
    parts["new_note"].attrs["AXEnabled"] = False
    with pytest.raises(NativeError, match="greyed out"):
        await surface.press(await _handle(surface, "New Note"))
    assert parts["new_note"].performed == [] and recorder.events == []


async def test_a_stale_handle_says_to_look_again(notes):
    surface, _, _, parts = notes
    handle = await _handle(surface, "New Note")
    parts["new_note"].alive = False
    with pytest.raises(NativeError, match="read_window again"):
        await surface.press(handle)
    with pytest.raises(NativeError, match="no control"):
        await surface.press("ax999")


async def test_a_recycled_row_refuses_to_be_pressed_as_if_unchanged(notes):
    """The handle-drift finding: a virtualised list (Notes' own note list
    here, but the same shape as Mail, Messages, Finder list view, or almost
    any Electron app) can reuse the very same AX element for different
    content as the list scrolls or refreshes. ``alive`` (the test above)
    covers the element vanishing outright; this covers the element
    surviving but silently showing something else — the handle still
    resolves, so only comparing what it shows now against what the model
    read catches it."""
    surface, _, _, parts = notes
    handle = await _handle(surface, "Holiday ideas · Lisbon")
    cell = parts["rows"][1].children[0]
    cell.children[0].attrs["AXValue"] = "Recipes"
    cell.children[1].attrs["AXValue"] = "risotto"
    with pytest.raises(NativeError, match="now shows"):
        await surface.press(handle)


def _swap(parts, old, new) -> None:
    """Put *new* where *old* sits in the tree — a control the app rebuilt."""
    def walk(element) -> bool:
        for index, child in enumerate(element.children):
            if child is old:
                element.children[index] = new
                return True
            if walk(child):
                return True
        return False

    assert walk(parts["window"])


def _set_row(row, title: str, preview: str) -> None:
    cell = row.children[0]
    cell.children[0].attrs["AXValue"] = title
    cell.children[1].attrs["AXValue"] = preview


# These run against the fake accessibility tree: they prove the surface's own
# decisions (when to re-find, when to refuse). That macOS really hands back
# the same role/name for a rebuilt control is only proven on a real Mac.
async def test_a_stale_handle_is_refound_when_the_same_control_is_still_there(notes):
    surface, _, recorder, parts = notes
    handle = await _handle(surface, "New Note")
    rebuilt = El("AXButton", "New Note", frame=(640, 60, 30, 22))
    _swap(parts, parts["new_note"], rebuilt)
    parts["new_note"].alive = False
    assert await surface.press(handle) == "Pressed “New Note”."
    assert rebuilt.performed == ["AXPress"] and parts["new_note"].performed == []
    assert (surface.relocations, surface.relocations_refused) == (1, 0)
    # The handle now means the rebuilt control: a second press, and a fresh
    # look at the window, both agree — and the second needs no re-find.
    await surface.press(handle)
    assert rebuilt.performed == ["AXPress", "AXPress"]
    assert await _handle(surface, "New Note") == handle
    assert surface.relocations == 1


async def test_time_spent_finding_the_element_behind_a_handle_is_counted(notes, monkeypatch):
    surface, backend, _, _ = notes
    handle = await _handle(surface, "New Note")
    assert surface.resolve_seconds == 0.0
    original = surface._resolve_handle

    def slow(handle):
        time.sleep(0.05)
        return original(handle)

    monkeypatch.setattr(surface, "_resolve_handle", slow)
    await surface.describe(handle)
    assert 0.05 <= surface.resolve_seconds < 1.0
    first = surface.resolve_seconds
    await surface.describe(handle)
    assert surface.resolve_seconds >= first + 0.05, "it accumulates"


async def test_time_is_counted_even_when_the_handle_is_refused(notes):
    surface, _, _, _ = notes
    with pytest.raises(NativeError):
        await surface.press("ax999")
    assert surface.resolve_seconds > 0.0


async def test_a_handle_that_never_went_stale_counts_no_relocation(notes):
    surface, _, _, parts = notes
    await surface.press(await _handle(surface, "New Note"))
    assert (surface.relocations, surface.relocations_refused) == (0, 0)


async def test_a_recycled_row_is_refound_where_its_content_went(notes):
    """The virtualised-list case: the row element the model chose now shows
    something else, but the content it chose is still on screen — on a
    different element. Act there, never on what the old element shows now."""
    surface, backend, _, parts = notes
    handle = await _handle(surface, "Holiday ideas · Lisbon")
    _set_row(parts["rows"][1], "Recipes", "risotto")
    _set_row(parts["rows"][2], "Holiday ideas", "Lisbon")
    await surface.press(handle)
    assert backend.sets == [(parts["rows"][2], "AXSelected", True)]


async def test_an_ambiguous_refind_is_refused_rather_than_guessed(notes):
    surface, backend, _, parts = notes
    handle = await _handle(surface, "Holiday ideas · Lisbon")
    _set_row(parts["rows"][1], "Recipes", "risotto")
    _set_row(parts["rows"][2], "Holiday ideas", "Lisbon")
    _set_row(parts["rows"][0], "Holiday ideas", "Lisbon")      # now two of them
    with pytest.raises(NativeError, match="now shows"):
        await surface.press(handle)
    assert backend.sets == []
    assert (surface.relocations, surface.relocations_refused) == (0, 1)


async def test_refinding_one_handle_does_not_vouch_for_the_others(notes):
    """A re-find must not refresh anyone else's fingerprint: a handle whose
    row changed to something found nowhere still has to be refused."""
    surface, backend, _, parts = notes
    kept = await _handle(surface, "Holiday ideas · Lisbon")
    lost = await _handle(surface, "Shopping list · milk, eggs")
    _set_row(parts["rows"][0], "Brand new", "thing")
    _set_row(parts["rows"][1], "Recipes", "risotto")
    _set_row(parts["rows"][2], "Holiday ideas", "Lisbon")
    await surface.press(kept)
    assert backend.sets == [(parts["rows"][2], "AXSelected", True)]
    with pytest.raises(NativeError, match="now shows"):
        await surface.press(lost)
    assert len(backend.sets) == 1


async def test_a_look_alike_with_another_subrole_is_not_refound(notes):
    """A plain text area gone, a password field of the same name in its
    place: not the same control, so not acted on — and nothing is typed."""
    surface, backend, recorder, parts = notes
    handle = await _handle(surface, "Note body")
    impostor = El("AXTextArea", subrole="AXSecureTextField", description="Note body",
                  frame=(240, 100, 500, 400))
    _swap(parts, parts["body"], impostor)
    parts["body"].alive = False
    with pytest.raises(NativeError, match="read_window again"):
        await surface.type_into(handle, "hunter2")
    assert recorder.events == [] and backend.sets == []


async def test_a_same_named_control_in_another_window_is_not_refound(notes):
    surface, backend, _, parts = notes
    handle = await _handle(surface, "New Note")
    elsewhere = El("AXWindow", "Another note", actions=(), frame=(0, 0, 900, 560),
                   children=[El("AXButton", "New Note", frame=(640, 60, 30, 22))])
    backend.apps["Notes"][1].attrs["AXWindows"].append(elsewhere)
    parts["new_note"].alive = False
    with pytest.raises(NativeError, match="read_window again"):
        await surface.press(handle)


async def test_a_refind_that_is_not_on_screen_is_refused(notes):
    surface, _, _, parts = notes
    handle = await _handle(surface, "New Note")
    _swap(parts, parts["new_note"], El("AXButton", "New Note", frame=(5000, 5000, 30, 22)))
    parts["new_note"].alive = False
    with pytest.raises(NativeError, match="read_window again"):
        await surface.press(handle)


async def test_a_window_too_big_to_search_whole_is_not_refound(notes, monkeypatch):
    """If the walk was cut short, "exactly one match" proves nothing."""
    surface, backend, _, parts = notes
    handle = await _handle(surface, "New Note")
    _swap(parts, parts["new_note"], El("AXButton", "New Note", frame=(640, 60, 30, 22)))
    parts["new_note"].alive = False
    # Four nodes reach the rebuilt button and then stop short of the rest of
    # the window — so the one match is there, but nothing proves it's the only one.
    monkeypatch.setattr(axmod, "MAX_VISITED", 4)
    cut = axmod.snapshot(backend, parts["window"])
    assert cut.truncated and any(c.label == "New Note" for c in cut.controls)
    with pytest.raises(NativeError, match="read_window again"):
        await surface.press(handle)


async def test_describe_and_press_each_ask_the_accessibility_server_once(notes):
    """_resolve() reads a control's full attributes to verify it and its
    content are still what the handle promised — describe() (the registry's
    own consequence check, run before every action) and each action
    (press/type_into/choose_option/...) used to re-fetch that identical,
    already-in-hand set of attributes right afterwards, doubling the
    Accessibility-server round trips behind every single native action."""
    surface, backend, _, parts = notes
    handle = await _handle(surface, "New Note")

    calls = {"n": 0}
    original = backend.attributes

    def counting(element, names):
        calls["n"] += 1
        return original(element, names)

    backend.attributes = counting

    info = await surface.describe(handle)
    assert info["text"] == "New Note"
    assert calls["n"] == 1, "describe() must fetch the control's attributes only once"

    calls["n"] = 0
    summary = await surface.press(handle)
    assert parts["new_note"].performed == ["AXPress"] and "Pressed" in summary
    assert calls["n"] == 1, "press() must reuse _resolve()'s own fetch, not repeat it"


def test_poll_returns_as_soon_as_the_condition_is_true():
    """The fixed-sleep replacement: activation and menu-populate waits now
    poll instead of guessing one flat delay — this proves the poll itself
    returns the moment it's ready rather than always waiting the ceiling,
    which is what makes the common (fast) case no slower than before."""
    from jarvis.surfaces.native.surface import NativeSurface

    slept: list[float] = []
    surface = NativeSurface(sleep=slept.append)
    countdown = [2]  # not ready for the first two checks, ready on the third

    def ready() -> bool:
        if countdown[0] > 0:
            countdown[0] -= 1
            return False
        return True

    assert surface._poll(ready, timeout_s=5.0, interval_s=0.01) is True
    assert slept == [0.01, 0.01], "one sleep per failed check, no more"


def test_poll_gives_up_once_the_timeout_elapses():
    from jarvis.surfaces.native.surface import NativeSurface

    surface = NativeSurface(sleep=lambda _s: None)
    assert surface._poll(lambda: False, timeout_s=0.05, interval_s=0.01) is False


async def test_typing_focuses_the_field_selects_it_and_types(notes):
    surface, backend, recorder, parts = notes
    handle = next(c.handle for c in (await surface.read())[0].controls if c.role == "search field")

    def typed(_event):
        parts["search"].attrs["AXValue"] = "eggs"

    recorder_type = recorder.type_text

    def type_and_update(text):
        typed(None)
        return recorder_type(text)

    recorder.type_text = type_and_update
    summary, value = await surface.type_into(handle, "eggs", submit=True)
    assert (parts["search"], "AXFocused", True) in backend.sets
    assert recorder.events == [("key", "cmd+a", 1), ("type", "eggs"), ("key", "return", 1)]
    assert value == "eggs" and "pressed Return" in summary


async def test_a_field_that_ignores_keys_gets_its_value_set_directly(notes):
    surface, backend, _, parts = notes
    handle = await _handle(surface, "Note body")
    _, value = await surface.type_into(handle, "buy cheese")
    assert (parts["body"], "AXValue", "buy cheese") in backend.sets and value == "buy cheese"


async def test_a_password_field_is_never_typed_into(notes):
    surface, _, recorder, parts = notes
    secure = El("AXTextField", subrole="AXSecureTextField", title="Password", frame=(300, 530, 100, 20))
    parts["split"].children.append(secure)
    with pytest.raises(NativeError, match="never type passwords"):
        await surface.type_into(await _handle(surface, "Password"), "hunter2")
    assert recorder.events == []
    app = notes[1].application(101)
    app.attrs["AXFocusedUIElement"] = secure
    with pytest.raises(NativeError, match="never type passwords"):
        await surface.type_text("hunter2")
    assert recorder.events == []


async def test_long_text_is_pasted_and_the_clipboard_put_back(notes):
    surface, _, recorder, _ = notes

    class Board:
        def __init__(self):
            self.contents = [{"public.png": b"picture"}]
            self.log = []

        def save(self):
            return list(self.contents)

        def set_text(self, text):
            self.log.append(("set", len(text)))

        def restore(self, saved):
            self.log.append(("restore", saved))

    board = Board()
    surface._clipboard = board
    await surface.type_text("x" * 500)
    assert ("key", "cmd+v", 1) in recorder.events
    assert board.log == [("set", 500), ("restore", [{"public.png": b"picture"}])]


async def test_a_menu_item_is_chosen_by_its_path(notes):
    surface, _, _, parts = notes
    summary = await surface.choose_menu(["file", "Export as PDF"])
    assert parts["export"].performed == ["AXPress"]
    assert summary == "Chose File › Export as PDF… in Notes.", "the summary names the real items"


async def test_a_wrong_menu_path_says_what_is_there(notes):
    surface, _, _, parts = notes
    with pytest.raises(NativeError) as raised:
        await surface.choose_menu(["File", "Export as Word"])
    assert "New Note, Export as PDF…, Print…" in raised.value.message
    with pytest.raises(NativeError, match="menus are: Notes, File, Edit"):
        await surface.choose_menu(["Filez", "Export"])
    with pytest.raises(NativeError, match="greyed out"):
        await surface.choose_menu(["File", "Print…"])
    assert parts["greyed"].performed == []


async def test_a_menu_that_fills_in_when_opened_is_opened(notes):
    surface, _, _, parts = notes
    menu = parts["file"].children[0]
    items, menu.children = menu.children, []

    def fill(action):
        menu.children = items

    parts["file"].on_perform = fill
    await surface.choose_menu(["File", "Export as PDF…"])
    assert parts["file"].performed == ["AXPress"] and parts["export"].performed == ["AXPress"]


async def test_an_option_is_chosen_from_a_pop_up(notes):
    surface, _, _, parts = notes
    a4 = El("AXMenuItem", "A4")
    popup = El("AXPopUpButton", "Paper size", value="Letter", frame=(300, 530, 100, 20))
    menu = El("AXMenu", actions=(), children=[El("AXMenuItem", "Letter"), a4])
    popup.on_perform = lambda action: popup.children.append(menu)
    parts["split"].children.append(popup)
    summary = await surface.choose_option(await _handle(surface, "Paper size"), "a4")
    assert a4.performed == ["AXPress"] and summary == "Chose “A4” in “Paper size”."
    with pytest.raises(NativeError, match="it has: Letter, A4"):
        await surface.choose_option(await _handle(surface, "Paper size"), "Legal")


async def test_dragging_between_plain_controls_goes_from_centre_to_centre(notes):
    surface, _, recorder, _ = notes
    snap, _ = await surface.read()
    handles = {c.label: c.handle for c in snap.controls}
    await surface.drag(handles["New Note"], handles["Bold"])
    assert recorder.events[-1] == ("drag", (655, 71), (312, 71))


async def test_dragging_a_row_takes_hold_of_its_name_not_the_middle_of_the_row(notes):
    """A row is as wide as its list; its middle is blank space, where a drag begins a rubber band."""
    surface, _, recorder, _ = notes
    snap, _ = await surface.read()
    handles = {c.label: c.handle for c in snap.controls}
    summary = await surface.drag(handles["Recipes · risotto"], handles["New Note"])
    assert recorder.events[-1] == ("drag", (48, 168), (655, 71)), \
        "just inside the left edge of the row's first text (24 + 24, its middle line), not (120, 174)"
    assert summary.startswith("Dragged “Recipes · risotto” onto “New Note”"), \
        "a row is named by the text inside it; its own title is empty"


def _row_with(*children, frame=(20, 100, 400, 22)):
    return El("AXRow", actions=(), frame=frame, children=[El("AXCell", actions=(), frame=frame, children=list(children))])


def _grab(row: El, backend=None):
    backend = backend or FakeBackend({}, front="")
    return axmod.grab_point(backend, row, backend.attributes(row, axmod.ATTRIBUTES))


def test_a_row_with_an_icon_is_taken_by_its_icon():
    row = _row_with(El("AXImage", actions=(), frame=(22, 103, 16, 16)),
                    El("AXStaticText", value="drag-me.txt", actions=(), frame=(42, 102, 200, 18)))
    point = _grab(row)
    assert (point.on, point.x, point.y) == ("icon", 30, 111)


def test_a_row_without_one_is_taken_just_inside_its_name_even_when_the_name_is_wide():
    row = _row_with(El("AXStaticText", value="drag-me.txt", actions=(), frame=(42, 102, 300, 18)))
    point = _grab(row)
    assert (point.on, point.x, point.y) == ("name", 66, 111), "42 + 24, not the middle of the 300-wide frame"
    narrow = _row_with(El("AXStaticText", value="a", actions=(), frame=(42, 102, 10, 18)))
    assert _grab(narrow).x == 47, "a name narrower than that is taken at its own middle"


def test_a_row_is_taken_at_its_centre_only_when_nothing_inside_it_can_be_located():
    nothing = _row_with(El("AXStaticText", value="x", actions=(), frame=(0, 0, 0, 0)),
                        El("AXImage", actions=(), frame=(500, 500, 16, 16)),     # not inside the row
                        El("AXImage", actions=(), frame=(22, 103, 300, 300)))     # a picture, not an icon
    point = _grab(nothing)
    assert (point.on, point.x, point.y) == ("centre", 220, 111)


def test_a_zero_sized_element_has_nowhere_to_grab():
    assert _grab(El("AXButton", "Hidden", frame=(0, 0, 0, 0))) is None
    assert _grab(El("AXButton", "No frame", frame=None)) is None


def test_a_plain_control_is_taken_at_its_centre():
    point = _grab(El("AXButton", "OK", frame=(10, 10, 80, 20)))
    assert (point.on, point.x, point.y) == ("centre", 50, 20)


async def test_a_drag_records_where_it_took_hold_and_let_go(notes):
    surface, _, _, _ = notes
    snap, _ = await surface.read()
    handles = {c.label: c.handle for c in snap.controls}
    await surface.drag(handles["Recipes · risotto"], handles["New Note"])
    drag = surface.last_drag
    assert (drag["from"].on, drag["to"].on) == ("name", "centre")
    assert drag["source"].w == 200 and drag["target"].w == 30


async def test_pressing_a_row_names_it_by_the_text_inside(notes):
    surface, _, _, els = notes
    snap, _ = await surface.read()
    row = next(c for c in snap.controls if c.label == "Recipes · risotto")
    els["rows"][2].actions = []
    assert await surface.press(row.handle) == "Selected “Recipes · risotto”."


# ---------------------------------------------------------------------------
# static text: what the app calls it, and what it shows
# ---------------------------------------------------------------------------
def _read_texts(*children: El) -> axmod.WindowSnapshot:
    window = El("AXWindow", "Calculator", actions=(), frame=(0, 0, 300, 400), children=list(children))
    return axmod.snapshot(FakeBackend({}, front=""), window)


@pytest.mark.parametrize("shown", ["12", 12, 12.0], ids=["string", "int", "float"])
def test_a_static_text_described_by_the_app_still_shows_its_value(shown):
    """Calculator's display: a static text *described* as "Edit field", its value the sum. The
    description used to win outright, so the window read "Edit field" and the 12 was never seen."""
    snap = _read_texts(El("AXStaticText", description="Last Expression", value="7+5", actions=(),
                          frame=(10, 10, 100, 10)),
                       El("AXStaticText", description="Edit field", value=shown, actions=(),
                          frame=(10, 30, 100, 30)))
    assert [(i.label, i.value) for i in snap.text_items] == [("Last Expression", "7+5"), ("Edit field", "12")]
    assert snap.texts == ["Last Expression: 7+5", "Edit field: 12"]
    assert "Text on screen: Last Expression: 7+5 · Edit field: 12" in axmod.render(snap)


def test_a_static_text_that_is_only_a_value_or_only_a_name_is_listed_as_before():
    snap = _read_texts(El("AXStaticText", value="3 notes", actions=(), frame=(0, 0, 50, 10)),
                       El("AXStaticText", description="Spacer", actions=(), frame=(0, 20, 50, 10)),
                       El("AXHeading", "Welcome", value="Welcome", actions=(), frame=(0, 40, 50, 10)))
    assert snap.texts == ["3 notes", "Spacer", "Welcome"], "a name that is also the value isn't said twice"
    assert [i.value for i in snap.text_items] == ["3 notes", "", "Welcome"]


def test_numbers_are_shown_as_numbers_and_a_boolean_is_not_a_number():
    snap = _read_texts(El("AXStaticText", value=0.5, actions=(), frame=(0, 0, 50, 10)),
                       El("AXStaticText", value=True, actions=(), frame=(0, 20, 50, 10)),
                       El("AXStaticText", value=1234567.0, actions=(), frame=(0, 40, 50, 10)),
                       El("AXStaticText", value=float("nan"), actions=(), frame=(0, 60, 50, 10)))
    assert snap.texts == ["0.5", "1234567"]


def test_a_text_field_with_a_numeric_value_lists_it_and_a_checkbox_still_does_not():
    snap = _read_texts(El("AXTextField", "Quantity", value=12, frame=(0, 0, 80, 20)),
                       El("AXCheckBox", "Bold", value=1, frame=(0, 30, 80, 20)),
                       El("AXButton", "Count", value=3, frame=(0, 60, 80, 20)))
    by_label = {c.label: c for c in snap.controls}
    assert by_label["Quantity"].value == "12" and 'value="12"' in by_label["Quantity"].line()
    assert by_label["Bold"].value == "" and by_label["Count"].value == "", "0/1 and badge counts aren't text"


def test_a_static_text_inside_a_listed_row_is_still_the_rows_name_not_a_separate_text():
    row = _row_with(El("AXStaticText", description="Name", value="drag-me.txt", actions=(), frame=(24, 100, 80, 18)))
    snap = _read_texts(row)
    assert snap.texts == [] and snap.text_items == []
    assert [c.label for c in snap.controls] == ["drag-me.txt"], "named by what the text shows, not what it is called"


# ---------------------------------------------------------------------------
# input
# ---------------------------------------------------------------------------
def test_shortcuts_resolve_to_key_codes_and_modifiers():
    assert resolve_key("cmd+shift+s") == Keystroke(0x01, (1 << 20) | (1 << 17), "cmd+shift+s")
    assert resolve_key("F5").keycode == 0x60
    assert resolve_key("forward delete").keycode == 0x75
    assert resolve_key("?").flags == 1 << 17, "a shifted symbol brings its own shift"
    assert resolve_key("s", ["command"]).flags == 1 << 20
    with pytest.raises(ValueError):
        resolve_key("hyper+x")


def test_text_is_split_for_key_events_without_breaking_characters():
    assert text_chunks("a\nb") == ["a", "\n", "b"]
    chunks = text_chunks("😀" * 15)
    assert all(len(c.encode("utf-16-le")) // 2 <= 20 for c in chunks) and "".join(chunks) == "😀" * 15


def test_input_posts_real_events_in_order():
    class Poster:
        def __init__(self):
            self.log = []

        def key(self, code, flags, down):
            self.log.append(("key", code, flags, down))

        def unicode(self, text, down):
            self.log.append(("uni", text, down))

        def mouse(self, kind, x, y, button="left", state=1):
            self.log.append(("mouse", kind, x, y, button, state))

    poster = Poster()
    native = NativeInput(poster, sleep=lambda _s: None)
    native.type_text("hi\nyou")
    native.click(5, 6, clicks=2)
    assert poster.log[:6] == [("uni", "hi", True), ("uni", "hi", False), ("key", 0x24, 0, True),
                              ("key", 0x24, 0, False), ("uni", "you", True), ("uni", "you", False)]
    assert [e[1:6] for e in poster.log[6:]] == [
        ("move", 5, 6, "left", 1), ("down", 5, 6, "left", 1), ("up", 5, 6, "left", 1),
        ("down", 5, 6, "left", 2), ("up", 5, 6, "left", 2)]


class _MoveRecordingPoster:
    def __init__(self):
        self.moves = []

    def mouse(self, kind, x, y, button="left", state=1):
        if kind == "move":
            self.moves.append((x, y))


def test_a_click_far_from_the_last_position_glides_there_instead_of_teleporting():
    poster = _MoveRecordingPoster()
    native = NativeInput(poster, sleep=lambda _s: None)
    native.click(0, 0)  # nothing known yet: a direct move, establishes position
    assert poster.moves == [(0, 0)]

    native.click(100, 0)
    assert len(poster.moves) > 2, "a real distance must produce more than one move event"
    assert poster.moves[-1] == (100, 0), "the glide must still land exactly on the target"
    xs = [p[0] for p in poster.moves[1:]]
    assert xs == sorted(xs), "each step must move strictly toward the target, not overshoot or jitter back"


def test_a_short_move_stays_a_single_direct_jump():
    poster = _MoveRecordingPoster()
    native = NativeInput(poster, sleep=lambda _s: None)
    native.move(0, 0)
    native.move(2, 2)  # well under the glide threshold
    assert poster.moves == [(0, 0), (2, 2)], "a move this small must not be split into steps"


class _Timeline:
    """A poster and the clock it is driven by, so an event sequence can be read with its timing."""

    def __init__(self):
        self.now, self.log = 0.0, []

    def mouse(self, kind, x, y, button="left", state=1):
        self.log.append((self.now, kind, x, y))

    def sleep(self, seconds):
        self.now += seconds

    def kinds(self):
        return [kind for _, kind, _, _ in self.log]


def _drag(start, end, **kw):
    line = _Timeline()
    native = NativeInput(line, sleep=line.sleep)
    native.drag(start, end, **kw)
    return line, native


def test_a_drag_rests_presses_nudges_carries_dwells_and_only_then_lets_go():
    line, _ = _drag((100, 100), (100, 300))
    down = next(i for i, (_, kind, _, _) in enumerate(line.log) if kind == "down")
    up = next(i for i, (_, kind, _, _) in enumerate(line.log) if kind == "up")
    assert line.kinds()[down + 1:up] == ["drag"] * (up - down - 1) and line.kinds()[-1] == "up"
    assert line.log[down][2:] == (100, 100) and line.log[up][2:] == (100, 300)

    # the pointer was seen resting on the item before it was pressed
    before_press = [t for t, kind, x, y in line.log[:down] if (x, y) == (100, 100)]
    assert line.log[down][0] - before_press[-1] >= 0.1

    # the first drag event is a small nudge past the drag threshold, then there is a pause
    first = line.log[down + 1]
    assert 4 < first[3] - 100 <= 12 and first[2] == 100, "past a ~4 pt threshold, not yet far"
    assert line.log[down + 2][0] - first[0] >= 0.15

    # the carry is monotonic toward the target and arrives exactly there
    carried = [y for _, kind, _, y in line.log[down + 1:up] if kind == "drag"]
    assert carried == sorted(carried) and carried[-1] == 300 and len(carried) >= 24

    # ...and the pointer is seen over the target for a while before the button comes up
    arrived = next(t for t, kind, _, y in line.log[down + 1:up] if y == 300)
    assert line.log[up][0] - arrived >= 0.3, "dropping needs the target to have seen the pointer"
    assert sum(1 for _, kind, _, y in line.log[down + 1:up] if y == 300) >= 4


def test_a_drag_shorter_than_the_threshold_never_overshoots_and_zero_distance_is_safe():
    line, _ = _drag((100, 100), (103, 100))
    drags = [x for _, kind, x, _ in line.log if kind == "drag"]
    assert max(drags) <= 103 and drags[-1] == 103
    line, _ = _drag((50, 50), (50, 50))
    assert line.kinds()[-1] == "up" and {(x, y) for _, kind, x, y in line.log if kind == "drag"} == {(50, 50)}


def test_after_a_drag_the_next_move_glides_from_where_it_ended():
    line, native = _drag((0, 0), (200, 0))
    mark = len(line.log)
    native.click(300, 0)
    glided = [x for _, kind, x, _ in line.log[mark:] if kind == "move"]
    assert glided[0] > 200 and glided[-1] == 300, "from the drop point, not back from where the drag began"


def test_glide_points_end_exactly_on_the_target():
    from jarvis.surfaces.native.input import _glide_points

    points = _glide_points(0, 0, 30, 60, steps=6)
    assert len(points) == 6
    assert points[-1] == (30, 60)


# ---------------------------------------------------------------------------
# marks
# ---------------------------------------------------------------------------
def test_screenshot_pixels_become_screen_points_on_a_retina_display():
    window = axmod.Frame(100, 50, 200, 100)
    box = TextBox("Save", x=40, y=20, w=60, h=20)
    assert to_points(box, (400, 200), window) == axmod.Frame(120, 60, 30, 10)
    flipped = from_normalised("Top", 0.9, (0.0, 0.9, 0.5, 0.1), (400, 200))
    assert (flipped.x, flipped.y, flipped.w, flipped.h) == (0, pytest.approx(0), 200, pytest.approx(20))


def _control(label, frame, role="button"):
    return axmod.Control(ref=None, ax_role="AXButton", subrole="", role=role, label=label,
                         frame=axmod.Frame(*frame))


def _snapshot(controls):
    snap = axmod.WindowSnapshot(app="Test", pid=1, title="Window", frame=None)
    snap.controls = controls
    return snap


def test_render_hints_at_mark_screen_when_no_controls_were_found():
    listing = axmod.render(_snapshot([]))
    assert "No usable controls were found here" in listing


def test_render_hints_at_mark_screen_when_every_control_is_unlabelled():
    """A window that returned controls, but none with a name or value, tells
    the model exactly as little as an empty listing would — common in
    Electron/canvas apps that expose bare, unlabelled roles."""
    controls = [_control("", (0, 0, 20, 20)), _control("", (30, 0, 20, 20))]
    listing = axmod.render(_snapshot(controls))
    assert "No usable controls were found here" in listing


def test_render_does_not_hint_when_at_least_one_control_is_identifiable():
    controls = [_control("", (0, 0, 20, 20)), _control("Share", (30, 0, 20, 20))]
    listing = axmod.render(_snapshot(controls))
    assert "No usable controls were found here" not in listing


def test_marks_name_unlabelled_controls_by_their_text_and_number_in_reading_order():
    window = axmod.Frame(0, 0, 200, 100)
    controls = [_control("", (10, 60, 40, 20)), _control("Share", (150, 10, 40, 20))]
    texts = [TextBox("Export", x=24, y=124, w=40, h=20),         # inside the unlabelled button
             TextBox("Total £12.99", x=20, y=20, w=120, h=16),
             TextBox("x", x=0, y=0, w=4, h=4, confidence=0.1)]   # noise
    marks = build_marks(texts, controls, (400, 200), window)
    assert [(m.number, m.kind, m.label) for m in marks] == [
        (1, "text", "Total £12.99"), (2, "button", "Share"), (3, "button", "Export")]
    listing = render_marks(marks, app="Pages", title="Invoice")
    assert '[m3] button "Export"' in listing and listing.count("Export") == 1


def test_the_overlay_is_drawn_for_a_vision_model(tmp_path):
    pytest.importorskip("PIL")
    from PIL import Image

    shot = tmp_path / "window.png"
    Image.new("RGB", (400, 200), "white").save(shot)
    window = axmod.Frame(0, 0, 200, 100)
    marks = build_marks([TextBox("Save", x=40, y=20, w=60, h=20)], [], (400, 200), window)
    out = draw_overlay(shot, marks, window, tmp_path / "marked.png")
    with Image.open(out) as image:
        assert image.size == (400, 200)
        assert image.getpixel((40, 30)) != (255, 255, 255), "the box was drawn where the text is"


def test_a_picked_mark_must_be_a_real_one():
    assert parse_pick("It's number 3.", 5) == 3
    assert parse_pick("0", 5) is None and parse_pick("12", 5) is None and parse_pick("", 5) is None


def test_captions_are_read_per_requested_number():
    reply = "2: gear icon\n3: close button\n99: not asked about"
    assert parse_captions(reply, {2, 3}) == {2: "gear icon", 3: "close button"}


def test_an_unparseable_caption_line_is_dropped_not_guessed_at():
    assert parse_captions("I can see a gear and a close button.", {2, 3}) == {}
    assert parse_captions("", {2, 3}) == {}


async def test_caption_unlabelled_marks_batches_one_vision_call(app, fake_provider, tmp_path):
    """The real value: click_mark/find_on_screen see "gear icon" instead of
    "(unlabelled)" afterward, and it costs one vision call, not one per icon."""
    pytest.importorskip("PIL")
    from PIL import Image

    overlay = tmp_path / "overlay.png"
    Image.new("RGB", (200, 100), "white").save(overlay)
    marks = [
        Mark(number=1, label="Share", kind="button", frame=axmod.Frame(0, 0, 20, 20), source="ax"),
        Mark(number=2, label="", kind="button", frame=axmod.Frame(30, 0, 20, 20), source="ax"),
        Mark(number=3, label="", kind="image", frame=axmod.Frame(60, 0, 20, 20), source="ax"),
    ]
    listing = "Marks on the screen (click_mark with a number clicks it):\n" + "\n".join(
        m.line() for m in marks)
    fake_provider.responses = ["2: gear icon\n3: close button"]

    tool = MarkScreenTool(app.deps)
    new_listing = await tool._caption_unlabelled(marks, overlay, listing)

    assert marks[1].label == "gear icon" and marks[2].label == "close button"
    assert marks[0].label == "Share", "an already-labelled mark is left untouched"
    assert '[m2] button "gear icon"' in new_listing and '[m3] image "close button"' in new_listing
    assert len(fake_provider.calls) == 1


async def test_caption_unlabelled_marks_reuses_a_recent_look_at_the_same_window(app, fake_provider,
                                                                                tmp_path):
    """The speed half of the fix: a vision call is the single most expensive
    step in native desktop control, and a task that looks, acts, then looks
    again at the same window shouldn't pay for it twice when nothing about
    the unlabelled marks has actually moved."""
    pytest.importorskip("PIL")
    from PIL import Image

    overlay = tmp_path / "overlay.png"
    Image.new("RGB", (200, 100), "white").save(overlay)
    marks = [Mark(number=2, label="", kind="button", frame=axmod.Frame(30, 0, 20, 20), source="ax")]
    listing = "Marks on the screen:\n" + marks[0].line()
    fake_provider.responses = ["2: gear icon"]

    tool = MarkScreenTool(app.deps)
    first = await tool._caption_unlabelled(marks, overlay, listing)
    assert marks[0].label == "gear icon" and len(fake_provider.calls) == 1

    marks[0].label = ""  # a fresh, otherwise-identical look at the same window
    second = await tool._caption_unlabelled(marks, overlay, listing)
    assert marks[0].label == "gear icon", "reused the cached caption"
    assert len(fake_provider.calls) == 1, "no second vision call for an unchanged window"
    assert first == second


async def test_caption_unlabelled_marks_recaptions_when_the_marks_actually_changed(app, fake_provider,
                                                                                   tmp_path):
    """The other half: a real change (a mark that moved, or a different
    app) must not be served a stale caption from the cache."""
    pytest.importorskip("PIL")
    from PIL import Image

    overlay = tmp_path / "overlay.png"
    Image.new("RGB", (200, 100), "white").save(overlay)
    listing = "Marks on the screen:\n"
    fake_provider.responses = ["2: gear icon", "2: a different icon"]

    tool = MarkScreenTool(app.deps)
    first_marks = [Mark(number=2, label="", kind="button", frame=axmod.Frame(30, 0, 20, 20), source="ax")]
    await tool._caption_unlabelled(first_marks, overlay, listing)
    assert first_marks[0].label == "gear icon"

    moved_marks = [Mark(number=2, label="", kind="button", frame=axmod.Frame(80, 0, 20, 20), source="ax")]
    await tool._caption_unlabelled(moved_marks, overlay, listing)
    assert moved_marks[0].label == "a different icon"
    assert len(fake_provider.calls) == 2


async def test_caption_unlabelled_marks_is_a_no_op_when_nothing_is_unlabelled(app, fake_provider, tmp_path):
    marks = [Mark(number=1, label="Share", kind="button", frame=axmod.Frame(0, 0, 20, 20), source="ax")]
    listing = "Marks on the screen:\n" + marks[0].line()
    tool = MarkScreenTool(app.deps)
    result = await tool._caption_unlabelled(marks, tmp_path / "overlay.png", listing)
    assert result == listing
    assert fake_provider.calls == []


async def test_caption_unlabelled_marks_leaves_marks_alone_if_the_vision_model_fails(
        app, fake_provider, tmp_path):
    """Captioning is a bonus on top of the baseline listing, never a
    requirement for it — a vision-model outage must not break mark_screen."""
    pytest.importorskip("PIL")
    from PIL import Image

    overlay = tmp_path / "overlay.png"
    Image.new("RGB", (200, 100), "white").save(overlay)
    marks = [Mark(number=1, label="", kind="button", frame=axmod.Frame(0, 0, 20, 20), source="ax")]
    listing = "Marks on the screen:\n" + marks[0].line()
    fake_provider.fail = True

    tool = MarkScreenTool(app.deps)
    result = await tool._caption_unlabelled(marks, overlay, listing)
    assert result == listing and marks[0].label == ""


async def test_caption_unlabelled_marks_ignores_an_unparseable_reply(app, fake_provider, tmp_path):
    pytest.importorskip("PIL")
    from PIL import Image

    overlay = tmp_path / "overlay.png"
    Image.new("RGB", (200, 100), "white").save(overlay)
    marks = [Mark(number=1, label="", kind="button", frame=axmod.Frame(0, 0, 20, 20), source="ax")]
    listing = "Marks on the screen:\n" + marks[0].line()
    fake_provider.responses = ["I can see a gear icon there."]

    tool = MarkScreenTool(app.deps)
    result = await tool._caption_unlabelled(marks, overlay, listing)
    assert result == listing and marks[0].label == ""


async def test_marking_a_window_and_clicking_a_mark(notes, tmp_path):
    pytest.importorskip("PIL")
    from PIL import Image

    surface, _, recorder, parts = notes
    parts["window"].attrs["AXPosition"] = (100, 50)
    parts["window"].attrs["AXSize"] = (900, 560)

    async def capture(pid, number):
        assert (pid, number) == (101, 4242)
        path = tmp_path / "shot.png"
        Image.new("RGB", (1800, 1120), "white").save(path)
        return path

    def recognize(path):
        return [TextBox("Welcome back", x=200, y=1060, w=300, h=30)]

    marks, listing, overlay = await surface.mark(capture, recognize=recognize, overlay_dir=tmp_path)
    assert '"Welcome back"' in listing and overlay is not None and overlay.exists()
    welcome = next(m for m in marks if m.label == "Welcome back")
    clicked = await surface.click_mark(welcome.number)
    assert clicked is welcome and recorder.events[-1] == ("click", 275, 588, "left", 1)
    with pytest.raises(NativeError, match="no mark 999"):
        await surface.click_mark(999)


# ---------------------------------------------------------------------------
# the tools, safety, and the operator
# ---------------------------------------------------------------------------
@pytest.fixture
def mac(app, notes, monkeypatch):
    """JARVIS with the fake Notes app as its native surface."""
    surface, backend, recorder, parts = notes
    # Default autonomy: routine steps run, consequential ones ask.
    app.config_store.update({"security": {"confirmation_timeout_s": 0.2}})
    app.deps.native = surface
    for name in app.deps.registry.names():
        tool = app.deps.registry.get(name)
        if tool.spec.category == "screen":
            monkeypatch.setattr(tool.spec, "requires_macos", False)

    async def frontmost():
        return backend.front

    monkeypatch.setattr(app.deps.controller, "frontmost_app", frontmost)
    return surface, backend, recorder, parts


async def test_read_window_is_a_tool_with_the_listing_as_its_observation(app, mac, ctx):
    result = await app.deps.registry.call("read_window", {}, ctx)
    assert result.ok and '] button "New Note"' in result.observation
    assert result.data["application"] == "Notes"


async def test_click_element_now_finds_controls_at_any_depth(app, mac, ctx):
    _, _, _, parts = mac
    result = await app.deps.registry.call("click_element", {"label": "New Note"}, ctx)
    assert result.ok and parts["new_note"].performed == ["AXPress"]
    many = await app.deps.registry.call("click_element", {"label": "o"}, ctx)
    assert not many.ok and "Call again with an index" in many.summary
    missing = await app.deps.registry.call("click_element", {"label": "Launch rockets"}, ctx)
    assert missing.wrong_tool


async def test_choose_option_reports_what_the_control_actually_shows_afterward(app, mac, ctx):
    """The tool re-reads the target after choosing, so the verifier
    (intelligence/verify.py) has real evidence the pop-up now shows what
    was asked for — not just that the click ran without raising."""
    surface, _, _, parts = mac
    a4 = El("AXMenuItem", "A4")
    popup = El("AXPopUpButton", "Paper size", value="Letter", frame=(300, 530, 100, 20))
    menu = El("AXMenu", actions=(), children=[El("AXMenuItem", "Letter"), a4])
    popup.on_perform = lambda action: popup.children.append(menu)
    # A real pop-up's own displayed value changes once an item is chosen —
    # the fake needs telling to do the same, on the menu item's own press.
    a4.on_perform = lambda action: popup.attrs.update({"AXValue": "A4"})
    parts["split"].children.append(popup)
    handle = await _handle(surface, "Paper size")

    result = await app.deps.registry.call("choose_option", {"handle": handle, "option": "a4"}, ctx)
    assert result.ok
    assert result.data["current_value"] == "A4"


async def test_mark_screen_respects_the_caption_config_flag_end_to_end(app, mac, ctx, fake_provider,
                                                                       monkeypatch):
    """The config flag actually reaches mark_screen's tool layer: on, the
    extra vision call happens; off, it doesn't and nothing else changes.
    (What the call does with an unlabelled mark is already covered by the
    isolated _caption_unlabelled tests above — this just proves the wiring.)"""
    pytest.importorskip("PIL")
    from jarvis.surfaces.native import marks as marks_module
    from jarvis.tools.macos.controller import ShellResult
    from PIL import Image

    _, _, _, parts = mac
    parts["split"].children.append(El("AXButton", "", frame=(800, 500, 20, 20)))
    monkeypatch.setattr(marks_module, "recognize_text", lambda path: [])

    async def fake_screencapture(argv, timeout=20.0):
        Image.new("RGB", (1800, 1120), "white").save(argv[-1])
        return ShellResult(0, "", "")

    monkeypatch.setattr(app.deps.controller, "run", fake_screencapture)

    app.config.capabilities.caption_unlabelled_marks = True
    fake_provider.responses = ["1: gear icon"]
    result_on = await app.deps.registry.call("mark_screen", {}, ctx)
    assert result_on.ok and len(fake_provider.calls) == 1, "the flag must trigger the extra call"

    fake_provider.calls.clear()
    app.config.capabilities.caption_unlabelled_marks = False
    result_off = await app.deps.registry.call("mark_screen", {}, ctx)
    assert result_off.ok and fake_provider.calls == [], "the flag must actually stop the extra call"
    assert "(unlabelled)" in result_off.observation, "the icon is still listed, just not captioned"


async def test_press_key_takes_whole_shortcuts(app, mac, ctx):
    _, _, recorder, _ = mac
    result = await app.deps.registry.call("press_key", {"key": "cmd+shift+n"}, ctx)
    assert result.ok and recorder.events == [("key", "cmd+shift+n", 1)]
    unknown = await app.deps.registry.call("press_key", {"key": "warp"}, ctx)
    assert not unknown.ok and "warp" in unknown.summary


@pytest.mark.parametrize(("path", "asks"), [
    (["Finder", "Empty Trash…"], True),
    (["File", "Move to Trash"], True),
    (["File", "Export as PDF…"], False),
    (["Format", "Bold"], False),
])
def test_menu_items_that_delete_are_always_confirmed(path, asks):
    target = {"role": "menu item", "text": path[-1], "application": "Finder"}
    spec = type("Spec", (), {"always_confirm_individually": False})()
    assert consequence.classify("choose_menu_item", {"path": path}, spec, target) is asks


async def test_a_control_is_judged_by_its_own_label_not_the_models(app, mac, ctx):
    """The model calls the "Move to Trash" button "Tidy up": the check reads
    the real button, so it still asks."""
    surface, _, _, parts = mac
    trash = El("AXButton", "Move to Trash", frame=(300, 530, 100, 20))
    parts["split"].children.append(trash)
    handle = await _handle(surface, "Move to Trash")
    asked: list[str] = []

    async def decline():
        while not app.permissions.pending():
            await asyncio.sleep(0.01)
        asked.append(app.permissions.pending()[0]["summary"])
        app.permissions.resolve(app.permissions.pending()[0]["id"], False)

    asyncio.create_task(decline())
    result = await app.deps.registry.call("click_control", {"handle": handle, "label": "Tidy up"}, ctx)
    assert not result.ok and trash.performed == []
    assert asked == ["Click “Move to Trash”?"]


async def test_the_operator_reads_the_window_again_after_acting(app, mac, fake_provider):
    surface, _, _, parts = mac
    handle = await _handle(surface, "New Note")
    prompts: list[str] = []
    replies = [json.dumps({"tool": "click_control", "arguments": {"handle": handle}}),
               json.dumps({"tool": "give_up", "arguments": {"reason": "enough"}})]

    def reply(messages, kwargs):
        text = "\n".join(m.content for m in messages)
        if "You operate this Mac for the user" in text:
            prompts.append(text)
            return replies.pop(0)
        return None

    fake_provider.router = reply
    parts["split"].children.append(El("AXTextField", "Title", value="", frame=(300, 530, 100, 20)))
    result = await Operator(app.deps).run(
        "start a note", app.deps.tool_context(), tools=["read_window", "click_control", "type_into"],
        budget=Budget(steps=5, wall_s=30, model_calls=5))
    assert result.steps == 1, "looking again costs no step"
    assert "The window now:" in prompts[1] and '] field "Title"' in prompts[1]


def test_native_tools_are_offered_compactly(app):
    defs = app.deps.registry.tool_defs(["read_window", "click_control", "choose_menu_item"])
    assert [d.name for d in defs] == ["read_window", "click_control", "choose_menu_item"]
    assert all('"default"' not in json.dumps(d.parameters) for d in defs)


def test_a_result_is_never_reported_for_a_tool_that_is_not_there(app):
    assert app.deps.registry.get("read_window") is not None
    assert isinstance(ToolResult(summary="x"), ToolResult)



# ---------------------------------------------------------------------------
# reading text off a picture (Vision), against stand-in modules
# ---------------------------------------------------------------------------
class _CocoaDict:
    """What +[NSDictionary dictionary] gives: a real Cocoa dictionary."""

    def removeObjectForKey_(self, key):
        return None


class _Observation:
    def __init__(self, text, confidence, box):
        self._text, self._confidence, self._box = text, confidence, box

    def topCandidates_(self, n):
        return [SimpleNamespace(string=lambda: self._text, confidence=lambda: self._confidence)]

    def boundingBox(self):
        x, y, w, h = self._box
        return SimpleNamespace(origin=SimpleNamespace(x=x, y=y), size=SimpleNamespace(width=w, height=h))


def _vision_stand_ins(monkeypatch, seen):
    """Vision, as PyObjC presents it on a Mac: handing a *Python* dict to Cocoa
    wraps it as OC_PythonDictionary, whose -removeObjectForKey: raises for an
    absent key, and Vision removes keys it may not find. (Models that behaviour,
    from PyObjC's own source; it is not Vision.)"""
    observations = [_Observation("Hello", 0.9, (0.1, 0.5, 0.2, 0.1)), _Observation("noise", 0.1, (0, 0, 0.1, 0.1))]

    class Handler:
        @classmethod
        def alloc(cls):
            return cls()

        def initWithURL_options_(self, url, options):
            seen["options"] = options
            if type(options) is dict:
                raise ValueError("NSInvalidArgumentException - key does not exist")
            options.removeObjectForKey_("VNImageOptionNotThere")
            return self

        def performRequests_error_(self, requests, error):
            return True, None

    class Request:
        @classmethod
        def alloc(cls):
            return cls()

        def init(self):
            return self

        def setRecognitionLevel_(self, level):
            seen["level"] = level

        def setUsesLanguageCorrection_(self, on):
            seen["correction"] = on

        def results(self):
            return observations

    vision = SimpleNamespace(VNImageRequestHandler=Handler, VNRecognizeTextRequest=Request)
    foundation = SimpleNamespace(NSURL=SimpleNamespace(fileURLWithPath_=lambda path: ("url", path)),
                                 NSDictionary=SimpleNamespace(dictionary=lambda: _CocoaDict()))
    monkeypatch.setitem(sys.modules, "Vision", vision)
    monkeypatch.setitem(sys.modules, "Foundation", foundation)


async def test_vision_is_handed_a_cocoa_dictionary_not_a_python_one(monkeypatch, tmp_path):
    """On a Mac the OCR step failed with "NSInvalidArgumentException - key does
    not exist": the ``{}`` passed as Vision's options was a Python dict."""
    pytest.importorskip("PIL")
    from jarvis.surfaces.native.marks import recognize_text
    from PIL import Image

    picture = tmp_path / "shot.png"
    Image.new("RGB", (200, 100), "white").save(picture)
    seen: dict = {}
    _vision_stand_ins(monkeypatch, seen)
    boxes = recognize_text(picture)
    assert type(seen["options"]) is not dict
    assert [b.text for b in boxes] == ["Hello", "noise"]
    assert (boxes[0].x, boxes[0].y, boxes[0].w, boxes[0].h) == pytest.approx((20, 40, 40, 10))
    assert boxes[0].confidence == 0.9
    assert seen["level"] == 0 and seen["correction"] is True


async def test_the_fast_setting_changes_the_recognition_level(monkeypatch, tmp_path):
    pytest.importorskip("PIL")
    from jarvis.surfaces.native.marks import recognize_text
    from PIL import Image

    picture = tmp_path / "shot.png"
    Image.new("RGB", (10, 10), "white").save(picture)
    seen: dict = {}
    _vision_stand_ins(monkeypatch, seen)
    recognize_text(picture, fast=True)
    assert seen["level"] == 1 and seen["correction"] is False


async def test_the_stand_in_really_does_fail_on_a_python_dict(monkeypatch):
    """So the test above could not pass by accident: given ``{}`` the stand-in raises."""
    seen: dict = {}
    _vision_stand_ins(monkeypatch, seen)
    with pytest.raises(ValueError, match="key does not exist"):
        sys.modules["Vision"].VNImageRequestHandler.alloc().initWithURL_options_("u", {})
