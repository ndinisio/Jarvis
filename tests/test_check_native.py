"""scripts/check_native.py's ``--controls`` checks, run against a fake Mac.

That script is what gets run on a real Mac, where it has never yet run; these
tests don't stand in for that. What they pin is the script's own logic — which
button it looks for under which names, what it counts as passing, what it
prints when a step fails (the labels the window really showed, so a pasted
result is enough to fix the surface from), and that it cleans up only what it
made. The Mac is faked at the same seam test_native.py uses (an accessibility
tree built from plain objects, and a recording input device), plus a handful of
fakes for the script's own shell-outs (open, pbpaste, screencapture, OCR).
Whether Calculator, TextEdit's Save sheet and Finder really answer to those
calls is exactly what the real run is for.
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest
from jarvis.surfaces.native import NativeSurface
from jarvis.surfaces.native.marks import TextBox
from test_native import El, FakeBackend, RecordingInput

pytestmark = pytest.mark.asyncio

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "check_native.py"


@pytest.fixture
def cn(monkeypatch):
    """The script as a module, with everything that would touch a real Mac
    replaced by a recorder. ``cn.calls`` is what it tried to do outside the
    accessibility tree."""
    spec = importlib.util.spec_from_file_location("check_native_script", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    calls: list[tuple] = []

    async def no_pause(_seconds):
        return None

    monkeypatch.setattr(module, "calls", calls, raising=False)
    monkeypatch.setattr(module, "pause", no_pause)
    monkeypatch.setattr(module, "launch", lambda app: calls.append(("launch", app)))
    monkeypatch.setattr(module, "open_path", lambda path: calls.append(("open", Path(path).name)))
    monkeypatch.setattr(module, "quit_app", lambda app: calls.append(("quit", app)))
    monkeypatch.setattr(module, "is_running", lambda app: False)
    monkeypatch.setattr(module, "read_clipboard", lambda: "")
    monkeypatch.setattr(module, "write_clipboard", lambda text: calls.append(("clipboard", text)))
    return module


def surface_for(apps: dict[str, tuple[int, El]], front: str, input_device=None) -> NativeSurface:
    return NativeSurface(backend=FakeBackend(apps, front=front), input=input_device or RecordingInput(),
                         sleep=lambda _s: None)


# ---------------------------------------------------------------------------
# Calculator
# ---------------------------------------------------------------------------
class Calculator:
    """A Calculator window that does arithmetic when a button is pressed —
    through an accessibility press or a click that lands on it."""

    WINDOW = (100, 100, 200, 300)
    LAYOUT = {"clear": (110, 150), "7": (110, 200), "add": (160, 200),
              "5": (110, 250), "equals": (160, 250), "9": (210, 200)}
    #: What is printed on each button — what a screenshot's OCR reads, which
    #: for the symbols is not what accessibility calls them.
    PRINTED = {"clear": "AC", "7": "7", "add": "+", "5": "5", "equals": "=", "9": "9"}

    def __init__(self, names: dict[str, str] | None = None):
        self.display, self.left, self.fresh = "0", 0, True
        called = {"clear": "All Clear", "7": "7", "add": "add", "5": "5", "equals": "equals",
                  "9": "9", **(names or {})}
        self.screen = El("AXStaticText", value="0", actions=(), frame=(110, 110, 180, 30))
        self.buttons = {key: El("AXButton", called[key], frame=(x, y, 40, 40))
                        for key, (x, y) in self.LAYOUT.items()}
        for key, button in self.buttons.items():
            button.on_perform = lambda _action, key=key: self.press(key)
        window = El("AXWindow", "Calculator", actions=(), frame=self.WINDOW,
                    children=[self.screen, *self.buttons.values()])
        self.app = El("AXApplication", "Calculator", actions=(), AXWindows=[window],
                      AXFocusedWindow=window, AXMenuBar=El("AXMenuBar", actions=()))

    def press(self, key: str) -> None:
        if key == "clear":
            self.display, self.left, self.fresh = "0", 0, True
        elif key == "add":
            self.left, self.fresh = int(self.display), True
        elif key == "equals":
            self.display, self.fresh = str(self.left + int(self.display)), True
        else:
            self.display = key if self.fresh else self.display + key
            self.fresh = False
        self.screen.attrs["AXValue"] = self.display

    def click_at(self, x: float, y: float) -> None:
        for key, (bx, by) in self.LAYOUT.items():
            if bx <= x <= bx + 40 and by <= y <= by + 40:
                self.press(key)

    def ocr(self, scale: int, *, reads_at: int | None = None) -> list[TextBox]:
        """The text on the buttons as OCR would box it in a screenshot taken at
        *scale* pixels to the point — or placed as if at *reads_at*, to
        stand in for an OCR/scale mismatch."""
        s = reads_at or scale
        wx, wy = self.WINDOW[:2]
        return [TextBox(self.PRINTED[key], x=(bx + 14 - wx) * s, y=(by + 14 - wy) * s, w=12 * s, h=12 * s)
                for key, (bx, by) in self.LAYOUT.items()]


class ClickingInput(RecordingInput):
    """Input whose clicks press whatever Calculator button they land on."""

    def __init__(self, calculator: Calculator):
        super().__init__()
        self.calculator = calculator

    def click(self, x, y, *, button="left", clicks=1):
        super().click(x, y, button=button, clicks=clicks)
        self.calculator.click_at(x, y)


@pytest.fixture
def calc(cn, monkeypatch, tmp_path):
    """A working Calculator, with the screenshot and OCR faked at 2 pixels to the point."""
    pytest.importorskip("PIL")
    from PIL import Image

    calculator = Calculator()

    async def capture(pid, number):
        path = tmp_path / "window.png"
        Image.new("RGB", (Calculator.WINDOW[2] * 2, Calculator.WINDOW[3] * 2), "white").save(path)
        return path

    monkeypatch.setattr(cn, "capture_window", capture)
    monkeypatch.setattr(cn, "recognize_text", lambda path: calculator.ocr(2))
    monkeypatch.setattr(cn, "read_clipboard", lambda: calculator.display)
    surface = surface_for({"Calculator": (101, calculator.app)}, "Calculator", ClickingInput(calculator))
    return calculator, surface


async def test_calculator_checks_pass_against_a_working_calculator(cn, calc, capsys):
    calculator, surface = calc
    assert await cn.calculator(surface) is True
    out = capsys.readouterr().out
    assert "✗" not in out
    assert "the display copied as '12'" in out
    assert "3 of 3 agree" in out, "the digit buttons' text is read where the buttons are"
    assert calculator.display == "9", "the click on mark 9 pressed the 9 button"
    assert ("launch", "Calculator") in cn.calls
    assert ("quit", "Calculator") in cn.calls, "it was launched by the check, so it's quit by it"
    assert ("clipboard", "0") in cn.calls, "the clipboard it borrowed is put back"


async def test_a_calculator_that_was_already_open_is_left_open(cn, calc, monkeypatch):
    _, surface = calc
    monkeypatch.setattr(cn, "is_running", lambda app: True)
    assert await cn.calculator(surface) is True
    assert ("quit", "Calculator") not in cn.calls


async def test_a_missing_button_is_reported_with_the_labels_that_were_there(cn, capsys):
    calculator = Calculator(names={"add": "plus sign"})
    surface = surface_for({"Calculator": (101, calculator.app)}, "Calculator", ClickingInput(calculator))
    assert await cn.calculator_click(surface) is False
    out = capsys.readouterr().out
    assert "✗ found the add button" in out
    assert 'button "plus sign"' in out, "the output says what the window did call it"


async def test_a_clear_button_under_another_name_is_not_a_failure(cn, capsys):
    calculator = Calculator(names={"clear": "Wipe"})
    surface = surface_for({"Calculator": (101, calculator.app)}, "Calculator", ClickingInput(calculator))
    cn.read_clipboard = lambda: calculator.display
    assert await cn.calculator_click(surface) is True
    assert "✗" not in capsys.readouterr().out


async def test_a_sum_that_did_not_happen_is_a_failure_that_shows_the_display(cn, capsys):
    calculator = Calculator()
    surface = surface_for({"Calculator": (101, calculator.app)}, "Calculator", ClickingInput(calculator))
    cn.read_clipboard = lambda: "7"             # the buttons were pressed but the display never moved on
    assert await cn.calculator_click(surface) is False
    assert "the display copied as '7'" in capsys.readouterr().out


async def test_text_read_at_the_wrong_scale_is_caught(cn, calc, monkeypatch, capsys):
    """If the screenshot's pixels-per-point were wrong, a click on text would
    land beside it; the agreement check is what would show that."""
    calculator, surface = calc
    monkeypatch.setattr(cn, "recognize_text", lambda path: calculator.ocr(2, reads_at=1))
    assert await cn.calculator_marks(surface) is False
    out = capsys.readouterr().out
    assert "✗ text read off the screenshot lands on the controls it names" in out
    assert "0 of 3 agree" in out


async def test_a_mark_click_that_pressed_nothing_is_a_failure(cn, calc, capsys):
    calculator, _ = calc
    surface = surface_for({"Calculator": (101, calculator.app)}, "Calculator", RecordingInput())
    calculator.press("5")
    assert await cn.calculator_marks(surface) is False
    assert "✗ clicking the mark pressed the button" in capsys.readouterr().out


async def test_without_pillow_the_missing_overlay_is_reported_not_crashed_on(cn, calc, monkeypatch, capsys):
    _, surface = calc
    monkeypatch.setattr("jarvis.surfaces.native.surface.draw_overlay", lambda *a, **k: None)
    assert await cn.calculator_marks(surface) is False
    assert "need Pillow" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# TextEdit's Save sheet
# ---------------------------------------------------------------------------
class TextEdit:
    """TextEdit with a File menu whose items are logged, and a Save sheet
    (Where pop-up, Cancel) that appears when File ▸ Save… is chosen."""

    def __init__(self, *, desktop: bool = True, new: bool = True):
        self.log: list[str] = []
        self.chosen = ""
        self.popup_value = "Documents"
        documents, desktop_item = El("AXMenuItem", "Documents"), El("AXMenuItem", "Desktop")
        documents.on_perform = lambda _a: self._choose("Documents")
        desktop_item.on_perform = lambda _a: self._choose("Desktop")
        self.popup = El("AXPopUpButton", "Where", value="Documents", frame=(120, 300, 200, 22),
                        children=[El("AXMenu", actions=(), children=[documents, *([desktop_item] if desktop else [])])])
        cancel = El("AXButton", "Cancel", frame=(300, 380, 80, 24))
        cancel.on_perform = lambda _a: self._cancel()
        self.sheet = El("AXSheet", actions=(), frame=(60, 200, 480, 220), children=[
            El("AXTextField", description="Save As", value="Untitled", frame=(120, 220, 300, 22)),
            self.popup, cancel, El("AXButton", "Save", frame=(400, 380, 80, 24))])
        self.window = El("AXWindow", "Untitled", actions=(), frame=(50, 50, 500, 400), children=[
            El("AXTextArea", description="document", value="", frame=(60, 80, 480, 300))])
        items = [self._item("New") if new else None, self._item("Save…", self._open_sheet), self._item("Close")]
        menubar = El("AXMenuBar", actions=(), children=[
            El("AXMenuBarItem", "File", children=[El("AXMenu", actions=(), children=[i for i in items if i])])])
        self.app = El("AXApplication", "TextEdit", actions=(), AXWindows=[self.window],
                      AXFocusedWindow=self.window, AXMenuBar=menubar)

    def _item(self, title, effect=None):
        item = El("AXMenuItem", title)
        item.on_perform = lambda _a: (self.log.append(title), effect() if effect else None)
        return item

    def _open_sheet(self):
        self.window.children.append(self.sheet)

    def _choose(self, name):
        self.popup.attrs["AXValue"] = name

    def _cancel(self):
        self.log.append("Cancel")
        self.window.children.remove(self.sheet)


async def test_the_save_sheets_popup_is_set_to_desktop_then_cancelled_and_closed(cn, capsys):
    textedit = TextEdit()
    surface = surface_for({"TextEdit": (303, textedit.app)}, "TextEdit")
    assert await cn.guarded("x", cn.textedit_dropdown(surface)) is True
    out = capsys.readouterr().out
    assert "✓ the pop-up now says Desktop" in out
    assert textedit.popup.attrs["AXValue"] == "Desktop"
    assert textedit.log == ["New", "Save…", "Cancel", "Close"], "nothing saved, one document closed"
    assert ("quit", "TextEdit") in cn.calls


async def test_a_popup_without_the_option_lists_the_options_it_has_and_still_cleans_up(cn, capsys):
    textedit = TextEdit(desktop=False)
    surface = surface_for({"TextEdit": (303, textedit.app)}, "TextEdit")
    assert await cn.guarded("choose_option in TextEdit", cn.textedit_dropdown(surface)) is False
    out = capsys.readouterr().out
    assert "has no option “Desktop”" in out and "Documents" in out
    assert textedit.log[-2:] == ["Cancel", "Close"]
    assert textedit.log.count("Close") == 1


async def test_nothing_is_closed_if_the_check_never_made_a_document(cn):
    """A failed File ▸ New must not be followed by a File ▸ Close, which would
    close whatever document was in front — possibly the person's own."""
    textedit = TextEdit(new=False)
    surface = surface_for({"TextEdit": (303, textedit.app)}, "TextEdit")
    assert await cn.guarded("x", cn.textedit_dropdown(surface)) is False
    assert "Close" not in textedit.log


# ---------------------------------------------------------------------------
# Finder
# ---------------------------------------------------------------------------
class Finder:
    """A Finder list-view window for the check's throwaway folder."""

    def __init__(self, home: Path, *, title: str | None = None):
        self.base = home / "Desktop" / f"JARVIS-check-{os.getpid()}"
        self.log: list[str] = []

        def row(name, y):
            return El("AXRow", actions=(), frame=(20, y, 400, 22), children=[
                El("AXCell", actions=(), frame=(20, y, 400, 22), children=[
                    El("AXStaticText", value=name, actions=(), frame=(24, y, 200, 18)),
                    El("AXStaticText", value="--", actions=(), frame=(240, y, 60, 18))])])

        self.file_row, self.folder_row = row("drag-me.txt", 100), row("target", 130)
        self.window = El("AXWindow", title or self.base.name, actions=(), frame=(0, 0, 500, 300),
                         children=[El("AXOutline", actions=(), frame=(10, 90, 420, 200),
                                      children=[self.file_row, self.folder_row])])

        def item(title, menu):
            element = El("AXMenuItem", title)
            element.on_perform = lambda _a: self.log.append(title)
            return element

        menubar = El("AXMenuBar", actions=(), children=[
            El("AXMenuBarItem", "File", children=[El("AXMenu", actions=(), children=[item("Close Window", "File")])]),
            El("AXMenuBarItem", "View", children=[El("AXMenu", actions=(), children=[item("as List", "View")])])])
        self.app = El("AXApplication", "Finder", actions=(), AXWindows=[self.window],
                      AXFocusedWindow=self.window, AXMenuBar=menubar)


class DroppingInput(RecordingInput):
    """Input whose drag moves the file when it ends over the folder row."""

    def __init__(self, finder: Finder):
        super().__init__()
        self.finder = finder

    def drag(self, start, end, steps: int = 12) -> None:
        super().drag(start, end, steps)
        if 130 <= end[1] <= 152:
            (self.finder.base / "drag-me.txt").rename(self.finder.base / "target" / "drag-me.txt")


@pytest.fixture
def home(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


async def test_a_file_dragged_onto_a_folder_is_checked_on_disk_and_cleaned_up(cn, home, capsys):
    finder = Finder(home)
    surface = surface_for({"Finder": (202, finder.app)}, "Finder", DroppingInput(finder))
    assert await cn.guarded("x", cn.finder_drag(surface)) is True
    assert "✓ the file is inside the folder" in capsys.readouterr().out
    assert finder.log == ["as List", "Close Window"]
    assert not finder.base.exists(), "the throwaway folder is removed"


async def test_a_drag_that_moved_nothing_is_a_failure_that_says_what_is_on_disk(cn, home, capsys):
    finder = Finder(home)
    surface = surface_for({"Finder": (202, finder.app)}, "Finder")       # a drag that does nothing
    assert await cn.guarded("x", cn.finder_drag(surface)) is False
    out = capsys.readouterr().out
    assert "✗ the file is inside the folder" in out and "drag-me.txt" in out
    assert not finder.base.exists()


async def test_a_finder_window_the_check_did_not_open_is_not_closed(cn, home):
    finder = Finder(home, title="Documents")                   # the person's own window is in front
    surface = surface_for({"Finder": (202, finder.app)}, "Finder")
    assert await cn.guarded("x", cn.finder_drag(surface)) is False
    assert "Close Window" not in finder.log
    assert not finder.base.exists()


# ---------------------------------------------------------------------------
# the script as a whole
# ---------------------------------------------------------------------------
async def test_one_check_crashing_does_not_stop_the_others(cn, monkeypatch, capsys):
    ran = []

    async def fine(_surface):
        ran.append("calculator")
        return True

    async def crashes(_surface):
        raise RuntimeError("boom")

    async def last(_surface):
        ran.append("finder")
        return True

    monkeypatch.setattr(cn, "calculator", fine)
    monkeypatch.setattr(cn, "textedit_dropdown", crashes)
    monkeypatch.setattr(cn, "finder_drag", last)
    assert await cn.controls(object()) is False
    assert ran == ["calculator", "finder"]
    assert "RuntimeError: boom" in capsys.readouterr().out


class _Stub:
    def __init__(self, available=True):
        self._available = available
        self.backend = self

    def available(self):
        return self._available

    def trusted(self, prompt=False):
        return True


@pytest.mark.parametrize("flag, expected", [([], []), (["--controls"], ["controls"])])
async def test_the_controls_flag_runs_the_controls_checks_and_only_then(cn, monkeypatch, flag, expected):
    ran = []

    async def look(surface, app):
        return True

    async def controls(surface):
        ran.append("controls")
        return True

    monkeypatch.setattr(cn, "NativeSurface", lambda: _Stub())
    monkeypatch.setattr(cn, "look", look)
    monkeypatch.setattr(cn, "controls", controls)
    monkeypatch.setattr("sys.argv", ["check_native.py", *flag])
    assert await cn.main() == 0
    assert ran == expected


async def test_without_the_native_extras_it_says_so_and_does_nothing(cn, monkeypatch, capsys):
    monkeypatch.setattr(cn, "NativeSurface", lambda: _Stub(available=False))
    monkeypatch.setattr("sys.argv", ["check_native.py", "--controls"])
    assert await cn.main() == 1
    assert "native extras aren't installed" in capsys.readouterr().out


async def test_picking_and_reading_helpers(cn):
    class C:
        def __init__(self, label, value="", role="button"):
            self.label, self.value, self.role = label, value, role

    controls = [C("Add"), C("plus"), C("7")]
    assert cn.pick(controls, "add", "+").label == "Add", "case aside, earliest wish first"
    assert cn.pick(controls, "+", "plus").label == "plus"
    assert cn.pick(controls, "minus") is None, "a near name isn't a match"

    class Snap:
        texts = ["‎12"]
        controls = [C("main display", "5")]

    assert cn.shows(Snap, "12"), "direction marks around the number don't hide it"
    assert cn.shows(Snap, "5") and not cn.shows(Snap, "1"), "whole texts only, never part of one"
    assert cn.names([C("OK"), C("")], limit=1).endswith("… (2 in all)")
