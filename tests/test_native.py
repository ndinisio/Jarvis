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
from typing import Any

import pytest
from jarvis.intelligence.operator import Budget, Operator
from jarvis.security import consequence
from jarvis.surfaces.native import PERMISSION_HINT, NativeError, NativeSurface
from jarvis.surfaces.native import ax as axmod
from jarvis.surfaces.native.input import Keystroke, NativeInput, resolve_key, text_chunks
from jarvis.surfaces.native.marks import (
    TextBox,
    build_marks,
    draw_overlay,
    from_normalised,
    parse_pick,
    render_marks,
    to_points,
)
from jarvis.tools.base import ToolResult

pytestmark = pytest.mark.asyncio


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


async def test_dragging_goes_from_centre_to_centre(notes):
    surface, _, recorder, _ = notes
    snap, _ = await surface.read()
    handles = {c.label: c.handle for c in snap.controls}
    await surface.drag(handles["Recipes · risotto"], handles["New Note"])
    assert recorder.events[-1] == ("drag", (120, 174), (655, 71))


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

