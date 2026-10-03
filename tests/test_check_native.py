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

import ast
import asyncio
import importlib.util
import json
import os
import re
import threading
from pathlib import Path

import pytest
from jarvis.surfaces.native import NativeError, NativeSurface
from jarvis.surfaces.native.ax import TextItem
from jarvis.surfaces.native.marks import TextBox
from jarvis.surfaces.native.surface import ACTIVATED, MENU_OPENED
from test_backend_cf import Array, AXRef, NullDereference, strict_backend
from test_native import El, FakeBackend, RecordingInput
from test_observer import FakeDriver

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
    monkeypatch.setattr(module, "real_pause", module.pause, raising=False)
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

    def __init__(self, names: dict[str, str] | None = None, display_as: str = "plain",
                 ax_frame: tuple | None = None):
        """*display_as* is how the display is exposed: ``plain`` (a static text whose value is the
        number — the shape every earlier version of this fake had, and the one real Calculator is
        not), ``described`` (the real shape, as far as a Mac's own dump has shown it: a static text
        *described* as "Edit field", its value the number as a string, under a "Last Expression"
        line), or ``number`` (the same, with the value an NSNumber)."""
        self.display, self.left, self.fresh = "0", 0, True
        self.display_as = display_as
        called = {"clear": "All Clear", "7": "7", "add": "add", "5": "5", "equals": "equals",
                  "9": "9", **(names or {})}
        self.screen = El("AXStaticText", value="0", actions=(), frame=(110, 110, 180, 30),
                         description="" if display_as == "plain" else "Edit field")
        self.expression = El("AXStaticText", description="Last Expression", value="", actions=(),
                             frame=(110, 104, 180, 10))
        self._show()
        self.buttons = {key: El("AXButton", called[key], frame=(x, y, 40, 40))
                        for key, (x, y) in self.LAYOUT.items()}
        for key, button in self.buttons.items():
            button.on_perform = lambda _action, key=key: self.press(key)
        window = El("AXWindow", "Calculator", actions=(), frame=ax_frame or self.WINDOW,
                    children=[*([] if display_as == "plain" else [self.expression]), self.screen,
                              *self.buttons.values()])
        menus = El("AXMenuBar", actions=(), children=[El("AXMenuBarItem", "Apple"), El("AXMenuBarItem", "Calculator"),
                                                      El("AXMenuBarItem", "View")])
        self.app = El("AXApplication", "Calculator", actions=(), AXWindows=[window],
                      AXFocusedWindow=window, AXMenuBar=menus)

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
        self._show()

    def _show(self) -> None:
        self.screen.attrs["AXValue"] = int(self.display) if self.display_as == "number" else self.display

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


@pytest.mark.parametrize("display_as", ["plain", "described", "number"])
async def test_read_window_shows_the_result_however_the_display_is_exposed(cn, display_as, capsys):
    """The display on a real Mac is a static text the app *describes* ("Edit field"), whose value is
    the sum. Reading it by name alone gives "Edit field" and never "12"."""
    calculator = Calculator(display_as=display_as)
    surface = surface_for({"Calculator": (101, calculator.app)}, "Calculator", ClickingInput(calculator))
    cn.read_clipboard = lambda: calculator.display
    assert await cn.calculator_click(surface) is True
    out = capsys.readouterr().out
    assert "✓ read_window shows the result" in out and "✗" not in out
    if display_as != "plain":
        assert "Edit field → 12" in out, "the check prints the name and the shown value apart"


async def test_a_display_that_never_shows_the_sum_fails_and_dumps_the_raw_text_attributes(cn, capsys):
    calculator = Calculator(display_as="number")
    surface = surface_for({"Calculator": (101, calculator.app)}, "Calculator", ClickingInput(calculator))
    cn.read_clipboard = lambda: "12"                       # the app copies 12; read_window can't see it
    calculator.press = lambda key: None                    # ...because the display never changes
    assert await cn.calculator_click(surface) is False
    out = capsys.readouterr().out
    assert "✗ read_window shows the result" in out
    assert "AXStaticText Description='Edit field'<str> Value=0<int>" in out, \
        "the raw attribute and the type it came back as, so a number is not mistaken for a string"
    assert "AXStaticText Description='Last Expression'<str>" in out


class ProbeMac(FakeBackend):
    """A backend that can say what attributes an element has and which values are elements, as the
    real one can — for the search that finds where an app keeps a piece of text."""

    def attribute_names(self, element):
        return [*element.attrs, "AXChildren"]

    def linked_elements(self, value):
        if isinstance(value, El):
            return [value]
        if isinstance(value, list) and value and all(isinstance(v, El) for v in value):
            return value
        return None


def _locate(cn, *children, snap_text="12", **window_extra):
    window = El("AXWindow", "Calculator", actions=(), frame=(0, 0, 300, 400), children=list(children),
                **window_extra)
    app = El("AXApplication", "Calculator", actions=(), AXWindows=[window], AXFocusedWindow=window)
    backend = ProbeMac({"Calculator": (101, app)}, front="Calculator")
    from jarvis.surfaces.native import ax

    return cn.locate_text(backend, 101, snap_text, ax.snapshot(backend, window)), backend


def test_the_search_finds_a_value_the_listing_shows_and_says_so(cn):
    lines, _ = _locate(cn, El("AXStaticText", description="Edit field", value=12, actions=(), frame=(10, 10, 100, 30)))
    assert "read_window lists it" in lines[0]
    assert any("AXValue<int>='12' on AXStaticText description='Edit field'" in line and line.endswith("— listed")
               for line in lines), lines


def test_a_value_held_in_another_attribute_is_found_and_the_rule_that_misses_it_named(cn):
    lines, _ = _locate(cn, El("AXStaticText", description="Edit field", value="", actions=(), frame=(10, 10, 100, 30),
                              AXValueDescription="12"))
    assert "does not list it" in lines[0]
    assert any("AXValueDescription<str>='12'" in line
               and "it is in AXValueDescription, and the listing reads a static text's AXValue" in line
               for line in lines), lines


def test_a_value_on_a_role_the_listing_does_not_represent_is_named_as_such(cn):
    lines, _ = _locate(cn, El("AXGenericElement", value="12", actions=(), frame=(10, 10, 100, 30)))
    assert any("AXGenericElement is a role the listing does not represent" in line for line in lines), lines


def test_an_element_reached_only_through_contents_is_found_and_flagged(cn):
    hidden = El("AXStaticText", value="12", actions=(), frame=(10, 10, 100, 30))
    lines, _ = _locate(cn, El("AXScrollArea", actions=(), frame=(0, 0, 200, 200), AXContents=[hidden]))
    assert any("reached only through AXContents, which the traversal does not follow" in line for line in lines), lines


def test_a_value_with_no_area_or_inside_a_skipped_container_is_explained(cn):
    lines, _ = _locate(cn, El("AXStaticText", value="12", actions=(), frame=(10, 10, 0, 0)),
                       El("AXScrollBar", actions=(), frame=(0, 0, 20, 200), children=[
                           El("AXStaticText", value="12", actions=(), frame=(1, 1, 10, 10))]))
    text = "\n".join(lines)
    assert "its frame has no area, so it is skipped as hidden" in text
    assert "it is inside a container the listing skips" in text


def test_when_the_app_exposes_the_value_nowhere_the_search_says_only_the_screenshot_has_it(cn):
    lines, _ = _locate(cn, El("AXStaticText", description="Edit field", value="", actions=(), frame=(10, 10, 100, 30)))
    assert "is in no attribute of anything reachable from the window" in lines[1]
    assert "only the screenshot has it" in lines[1]


def test_the_search_stays_inside_the_window_and_does_not_climb_to_its_parent(cn):
    """AXParent leads up to the application and from there to every other window and the menu bar."""
    stray = El("AXMenuItem", "12", actions=())
    app = El("AXApplication", "Calculator", actions=(), AXMenuBar=stray)
    window = El("AXWindow", "Calculator", actions=(), frame=(0, 0, 300, 400), children=[
        El("AXStaticText", value="0", actions=(), frame=(10, 10, 100, 30))], AXParent=app)
    app.attrs["AXWindows"] = [window]
    app.attrs["AXFocusedWindow"] = window
    backend = ProbeMac({"Calculator": (101, app)}, front="Calculator")
    from jarvis.surfaces.native import ax

    lines = cn.locate_text(backend, 101, "12", ax.snapshot(backend, window))
    assert lines[0].startswith("searched 2 elements") and "is in no attribute" in lines[1]


def test_the_search_stops_at_its_budget_and_visits_each_element_once(cn):
    shared = El("AXStaticText", value="x", actions=(), frame=(1, 1, 5, 5))
    many = [El("AXGroup", actions=(), frame=(0, 0, 10, 10), children=[shared], AXContents=[shared])
            for _ in range(30)]
    window = El("AXWindow", "W", actions=(), frame=(0, 0, 300, 400), children=many)
    app = El("AXApplication", "W", actions=(), AXWindows=[window], AXFocusedWindow=window)
    backend = ProbeMac({"W": (1, app)}, front="W")
    from jarvis.surfaces.native import ax

    snap = ax.snapshot(backend, window)
    assert cn.locate_text(backend, 1, "zzz", snap)[0].startswith("searched 32 elements")
    assert cn.locate_text(backend, 1, "zzz", snap, budget=5)[0].startswith("searched 5 elements")


async def test_a_failing_calculator_read_prints_the_search_before_the_raw_dump(cn, capsys, monkeypatch):
    calculator = Calculator(display_as="number")
    surface = surface_for({"Calculator": (101, calculator.app)}, "Calculator", ClickingInput(calculator))
    cn.read_clipboard = lambda: "12"
    calculator.press = lambda key: None
    assert await cn.calculator_click(surface) is False
    out = capsys.readouterr().out
    assert out.index("searched ") < out.index("AXStaticText Description='Edit field'<str> Value=0<int>")
    assert "“12” is in no attribute of anything reachable from the window" in out, \
        "the plain fake backend can't list attributes, so the default set is searched"


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


class BoundsMac(FakeBackend):
    """A Mac that can also say where the window server puts the window being photographed."""

    def __init__(self, *args, bounds=None, others=(), **kw):
        super().__init__(*args, **kw)
        self.bounds, self.others = bounds, list(others)

    def window_bounds(self, number):
        return self.bounds

    def window_list(self, pid):
        return list(self.others)

    def screens(self):
        return [{"frame": (0.0, 0.0, 1512.0, 982.0), "scale": 2.0}]


def _marks_surface(calculator, bounds, others=()):
    backend = BoundsMac({"Calculator": (101, calculator.app)}, front="Calculator", bounds=bounds, others=others)
    return NativeSurface(backend=backend, input=ClickingInput(calculator), sleep=lambda _s: None), backend


#: The Accessibility frame of an inner rectangle, not of the window drawn: what an app can report when
#: its window is hosted by something larger. Under it the reported Mac numbers are reproduced exactly —
#: its keypad's pixels convert to a layout 0.48 as wide and 0.78 as tall as its buttons'.
INNER = (110.0, 140.0, 96.0, 231.0)


@pytest.fixture
def shots(cn, monkeypatch, tmp_path):
    """Screenshots of Calculator's real window at two pixels to the point, and OCR over them."""
    pytest.importorskip("PIL")
    from PIL import Image

    async def capture(pid, number):
        path = tmp_path / "window.png"
        Image.new("RGB", (Calculator.WINDOW[2] * 2, Calculator.WINDOW[3] * 2), "white").save(path)
        return path

    monkeypatch.setattr(cn, "capture_window", capture)


async def test_text_lands_on_its_controls_when_converted_against_the_window_server_rectangle(
        cn, shots, monkeypatch, capsys):
    """The picture is of the window the window server photographed. Accessibility's frame for the
    window is something else (INNER); converting the pixels against it puts every digit in the wrong
    place, converting against the window server's rectangle does not."""
    calculator = Calculator(ax_frame=INNER)
    monkeypatch.setattr(cn, "recognize_text", lambda path: calculator.ocr(2))
    monkeypatch.setattr(cn, "read_clipboard", lambda: calculator.display)
    surface, _ = _marks_surface(calculator, bounds=Calculator.WINDOW)
    assert await cn.calculator_marks(surface) is True
    out = capsys.readouterr().out
    assert "3 of 3 agree" in out and "✗" not in out
    assert calculator.display == "9", "and a click on the 9 mark pressed the 9 button"
    assert surface.last_capture.source == "window server"
    assert surface.last_capture.disagreement.startswith("Accessibility says the window is (110, 140, 96×231)")


async def test_without_the_window_servers_rectangle_the_wrong_frame_fails_and_the_report_says_what_fits(
        cn, shots, monkeypatch, capsys):
    calculator = Calculator(ax_frame=INNER)
    monkeypatch.setattr(cn, "recognize_text", lambda path: calculator.ocr(2))
    surface, _ = _marks_surface(calculator, bounds=None)
    assert await cn.calculator_marks(surface) is False
    out = capsys.readouterr().out
    assert re.search(r"✗ text read off the screenshot lands on the controls it names — [0-2] of 3 agree", out)
    assert "converted against the accessibility's" in out
    assert "the controls fit neither window rectangle" in out
    assert "(100, 100, 200×300)" in out, "the rectangle the controls do fit: the window actually photographed"


async def test_the_failure_lists_the_windows_and_displays_that_could_explain_it(cn, shots, monkeypatch, capsys):
    calculator = Calculator(ax_frame=INNER)
    monkeypatch.setattr(cn, "recognize_text", lambda path: calculator.ocr(2))
    other = {"number": 7, "name": "History", "layer": 0, "on_screen": True, "bounds": (400.0, 100.0, 120.0, 200.0)}
    surface, _ = _marks_surface(calculator, bounds=None, others=[other])
    await cn.calculator_marks(surface)
    out = capsys.readouterr().out
    assert "window 7 “History” layer 0 on screen (400, 100, 120×200)" in out
    assert "displays: (0, 0, 1512×982) at 2×" in out


async def test_a_plain_mac_whose_two_descriptions_agree_is_unchanged(cn, shots, monkeypatch, capsys):
    calculator = Calculator()
    monkeypatch.setattr(cn, "recognize_text", lambda path: calculator.ocr(2))
    monkeypatch.setattr(cn, "read_clipboard", lambda: calculator.display)
    surface, _ = _marks_surface(calculator, bounds=Calculator.WINDOW)
    assert await cn.calculator_marks(surface) is True
    assert surface.last_capture.disagreement == "" and surface.last_capture.frame.w == 200


def test_the_scale_the_controls_fit_is_measured_not_assumed(cn):
    pairs = [((60.0, 40.0), (130.0, 120.0)), ((160.0, 40.0), (180.0, 120.0)), ((60.0, 240.0), (130.0, 220.0))]
    across, down = cn.fit_axis([(p[0][0], p[1][0]) for p in pairs]), cn.fit_axis([(p[0][1], p[1][1]) for p in pairs])
    assert across == pytest.approx((100.0, 0.5, 0.0)) and down == pytest.approx((100.0, 0.5, 0.0))
    assert cn.fit_axis([(5.0, 1.0), (5.0, 9.0)]) is None, "one column says nothing about the scale across"
    assert cn.fit_axis([(5.0, 1.0)]) is None
    noisy = cn.fit_axis([(0.0, 0.0), (100.0, 50.0), (200.0, 102.0)])
    assert noisy[1] == pytest.approx(0.51, abs=0.01) and 0 < noisy[2] < 2


def _capture(ax, bounds, size=(400, 600)):
    from jarvis.surfaces.native.ax import Frame
    from jarvis.surfaces.native.marks import Capture

    frame = Frame(*(bounds or ax))
    return Capture(frame, "window server" if bounds else "accessibility", Frame(*ax) if ax else None,
                   Frame(*bounds) if bounds else None, size)


def test_the_report_names_the_rectangle_the_controls_fit(cn):
    window = (100.0, 100.0, 200.0, 300.0)
    pairs = [((60.0, 40.0), (130.0, 120.0)), ((160.0, 40.0), (180.0, 120.0)), ((60.0, 240.0), (130.0, 220.0))]
    lines = cn.coordinate_report(_capture((110.0, 140.0, 96.0, 231.0), window), pairs)
    text = "\n".join(lines)
    assert "fitted to 3 pairs: 2.00 px/pt across, 2.00 down" in text
    assert "put the picture at (100, 100, 200×300)" in text and "the controls fit the window server's rectangle" in text
    assert "the two disagree: Accessibility says the window is (110, 140, 96×231)" in text
    agree = cn.coordinate_report(_capture(window, None), pairs)
    assert "the controls fit Accessibility's rectangle" in "\n".join(agree)


def test_controls_laid_out_on_a_different_scale_than_the_picture_are_called_that(cn):
    """Buttons are square, so a picture whose text spacing is 0.48 as wide and 0.78 as tall as the
    buttons' cannot be explained by any window rectangle."""
    window = (100.0, 100.0, 200.0, 300.0)
    pairs = [((60.0, 40.0), (130.0, 120.0)), ((160.0, 40.0), (130 + 100 / 0.481, 120.0)),
             ((60.0, 240.0), (130.0, 120 + 200 / 0.778))]
    text = "\n".join(cn.coordinate_report(_capture(window, window), pairs))
    assert "the scale across and down differ" in text and "no one window rectangle explains the controls" in text


def test_too_few_pairs_to_fit_is_said_not_guessed_at(cn):
    window = (100.0, 100.0, 200.0, 300.0)
    one_row = [((60.0, 40.0), (130.0, 120.0)), ((160.0, 40.0), (180.0, 120.0))]
    assert "too few, or all in one row or column" in "\n".join(cn.coordinate_report(_capture(window, window), one_row))
    assert "0 text/control pair(s)" in "\n".join(cn.coordinate_report(_capture(window, window), []))


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

    #: The "Where:" menu as a Mac listed it: names inside invisible bidi isolates, qualified iCloud
    #: folders, greyed-out headings, and "Desktop — iCloud" under two of them.
    ICLOUD = [("iCloud Library", False), ("\u2068Desktop\u2069 — iCloud", True), ("iCloud Drive", True),
              ("\u2068TextEdit\u2069 — iCloud", True), ("Locations", False), ("Macintosh HD", True),
              ("iCloud Drive", True), ("ndinisio", True), ("Favourites", False),
              ("\u2068Desktop\u2069 — iCloud", True), ("\u2068Documents\u2069 — iCloud", True),
              ("Downloads", True)]

    def __init__(self, *, desktop: bool = True, new: bool = True, where: str = "simple",
                 menu: list[tuple[str, bool]] | None = None, identifiers: dict | None = None):
        self.log: list[str] = []
        self.chosen = ""
        self.popup_value = "Documents"
        self.pressed: list[str] = []
        if where == "icloud":
            titles = [(t, e) for t, e in (menu or self.ICLOUD) if desktop or "Desktop" not in t]
            entries = []
            for number, (title, enabled) in enumerate(titles, 1):
                item = El("AXMenuItem", title, enabled=enabled, frame=(300, 100 + 20 * number, 200, 20),
                          **({"AXIdentifier": identifiers[number]} if identifiers and number in identifiers else {}))
                item.on_perform = lambda _a, n=number, t=title: (self.pressed.append(f"{n}:{t}"), self._choose(t))
                entries.append(item)
            self.popup = El("AXPopUpButton", "Where:", value="\u2068TextEdit\u2069 — iCloud",
                            frame=(120, 300, 200, 22))
            menu = El("AXMenu", actions=("AXCancel",), children=entries)
            for entry in entries:
                entry.attrs["AXParent"] = menu
            self.popup.on_perform = lambda _a: self.popup.children.append(menu) if not self.popup.children else None
        else:
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


async def test_a_desktop_listed_twice_is_refused_then_chosen_by_number_and_the_popup_must_say_desktop(cn, capsys):
    """The Where: pop-up on a Mac with iCloud Desktop: "Desktop — iCloud" under two headings."""
    textedit = TextEdit(where="icloud")
    surface = surface_for({"TextEdit": (303, textedit.app)}, "TextEdit")
    assert await cn.guarded("x", cn.textedit_dropdown(surface)) is True
    out = capsys.readouterr().out
    assert "✓ an option the menu lists twice is refused, not guessed" in out
    assert "1. “Desktop — iCloud” (under “iCloud Library”); 2. “Desktop — iCloud” (under “Favourites”)" in out
    assert "✓ the pop-up now says Desktop" in out and "✗" not in out
    assert textedit.pressed == ["2:\u2068Desktop\u2069 — iCloud"], "exactly one item was pressed: the first match"
    assert textedit.log == ["New", "Save…", "Cancel", "Close"]


async def test_the_refusal_shows_what_tells_the_two_candidates_apart(cn, capsys):
    surface = surface_for({"TextEdit": (303, (textedit := TextEdit(where="icloud")).app)}, "TextEdit")
    assert await cn.guarded("x", cn.textedit_dropdown(surface)) is True
    out = capsys.readouterr().out
    assert "candidate 1:" in out and "candidate 2:" in out
    assert "parent='AXMenu with 12 children'" in out
    assert "place in the menu='item 2, under “iCloud Library”'" in out and "item 10, under “Favourites”" in out
    assert "they differ in: AXPosition, place in the menu — only where they sit; nothing about what they are or do tells them apart" in out
    assert "\u2068" not in out and "Title='Desktop — iCloud'" in out
    assert textedit.pressed == ["2:\u2068Desktop\u2069 — iCloud"]


async def test_a_difference_in_identifier_is_what_the_evidence_points_at(cn, capsys):
    textedit = TextEdit(where="icloud", identifiers={2: "icloud-desktop", 10: "favourite-desktop"})
    surface = surface_for({"TextEdit": (303, textedit.app)}, "TextEdit")
    assert await cn.guarded("x", cn.textedit_dropdown(surface)) is True
    out = capsys.readouterr().out
    assert "they differ in: AXIdentifier, AXPosition, place in the menu" in out
    assert "only where they sit" not in out
    assert "Identifier='icloud-desktop'" in out and "Identifier='favourite-desktop'" in out


# ---------------------------------------------------------------------------
# the harness's native diagnostics, on a C API where handing it nothing is fatal
# ---------------------------------------------------------------------------
def _menu_items_on_the_strict_api():
    """Two menu items as the real backend class reads them: attributes an element lacks come back
    as None, children arrive as an NSArray proxy rather than a list, and the parent is an element."""
    menu = AXRef(AXRole="AXMenu")
    items = []
    for number, section in ((2, "iCloud Library"), (10, "Favourites")):
        items.append(types_namespace(element=AXRef(
            AXRole="AXMenuItem", AXTitle="Desktop — iCloud", AXEnabled=True, AXHelp=None, AXIdentifier=None,
            AXDescription=None, AXMenuItemMarkChar=None, AXPosition=(300.0, 100.0 + 20 * number),
            AXSize=(200.0, 20.0), AXParent=menu, AXChildren=Array()), index=number, section=section))
    menu.attrs["AXChildren"] = Array([i.element for i in items])
    return items


def types_namespace(**kw):
    import types

    return types.SimpleNamespace(**kw)


def test_the_candidate_facts_are_read_without_ever_handing_nothing_to_the_c_api(cn):
    """The v8.54 crash: an attribute the element lacks (here AXHelp, AXIdentifier, AXDescription,
    AXMenuItemMarkChar) reached CFGetTypeID as NULL, and the process died with exit 139."""
    backend, accessibility, cf = strict_backend()
    first, _ = _menu_items_on_the_strict_api()
    facts = cn.option_facts(backend, first)
    assert facts["AXTitle"] == "Desktop — iCloud" and facts["AXEnabled"] == "True"
    assert facts["AXPosition"] == "300,140" and facts["parent"] == "AXMenu with 2 children"
    assert facts["place in the menu"] == "item 2, under “iCloud Library”"
    assert not {"AXHelp", "AXIdentifier", "AXChildren", "AXParent"} & set(facts), "missing, links and arrays aren't facts"
    assert all(kind != "NoneType" for _, kind in cf.calls + accessibility.calls)


def test_the_evidence_for_two_candidates_survives_the_strict_c_api_end_to_end(cn):
    backend, _, cf = strict_backend()
    lines = cn.option_evidence(backend, _menu_items_on_the_strict_api())
    assert lines[0].startswith("candidate 1: Role='AXMenuItem' Title='Desktop — iCloud'")
    assert lines[-1] == ("they differ in: AXPosition, place in the menu — only where they sit; "
                         "nothing about what they are or do tells them apart")
    assert all(kind != "NoneType" for _, kind in cf.calls)


def test_the_text_search_follows_nsarray_children_that_are_not_lists_and_never_passes_nothing(cn):
    """The search must walk a real tree: AXChildren is an NSArray proxy, and most attributes of most
    elements are None. Under v8.54 it would have crashed on the first of them, and, had it not, never
    followed a child."""
    from jarvis.surfaces.native import ax

    backend, accessibility, cf = strict_backend()
    display = AXRef(AXRole="AXStaticText", AXDescription="Edit field", AXValue=12, AXTitle=None,
                    AXPosition=(10.0, 30.0), AXSize=(100.0, 30.0), AXChildren=Array())
    group = AXRef(AXRole="AXGroup", AXTitle=None, AXValue=None, AXChildren=Array([display]),
                  AXPosition=(0.0, 0.0), AXSize=(300.0, 400.0))
    window = AXRef(AXRole="AXWindow", AXTitle="Calculator", AXChildren=Array([group]),
                   AXPosition=(0.0, 0.0), AXSize=(300.0, 400.0), AXContents=None)
    application = AXRef(AXFocusedWindow=window)
    backend.application = lambda pid: application
    backend.front_window = lambda app: window
    snap = ax.WindowSnapshot(app="Calculator", pid=1, title="Calculator", frame=None)
    lines = cn.locate_text(backend, 1, "12", snap)
    assert lines[0].startswith("searched 3 elements")
    assert any("AXValue<int>='12' on AXStaticText description='Edit field'" in line for line in lines), lines
    assert all(kind != "NoneType" for _, kind in cf.calls + accessibility.calls)


def test_the_stand_in_would_have_caught_v8_54s_ordering(cn):
    """The old test of an attribute was `is_element(value) or value in (None, "")`: the element check
    first. Put back, against the strict C API, it dies."""
    backend, _, _ = strict_backend()
    first, _ = _menu_items_on_the_strict_api()
    values = backend.attributes(first.element, ("AXTitle", "AXHelp"))
    assert values["AXHelp"] is None
    with pytest.raises(NullDereference):
        backend.CF.CFGetTypeID(values["AXHelp"])         # what handing it the None did


# -- where the native diagnostics run -------------------------------------------------------------
#: The functions that call into Accessibility / CoreFoundation for the harness's evidence.
NATIVE_DIAGNOSTICS = {"option_evidence", "option_facts", "locate_text", "text_structure", "ax_structure",
                      "drag_evidence", "under_pointer"}


def calls_made_on_the_event_loop(source: str) -> list[str]:
    """Calls to a native diagnostic made directly (not as the function handed to ``asyncio.to_thread``)
    from an ``async def`` - that is, on the event loop's own thread."""
    found = []

    class Visitor(ast.NodeVisitor):
        def __init__(self):
            self.in_async = []

        def visit_AsyncFunctionDef(self, node):
            self.in_async.append(node.name)
            self.generic_visit(node)
            self.in_async.pop()

        def visit_FunctionDef(self, node):
            self.in_async.append(None)         # a plain def nested in an async one runs wherever it is called
            self.generic_visit(node)
            self.in_async.pop()

        def visit_Call(self, node):
            if isinstance(node.func, ast.Name) and node.func.id in NATIVE_DIAGNOSTICS and self.in_async \
                    and self.in_async[-1]:
                found.append(f"{self.in_async[-1]} calls {node.func.id} on the event loop (line {node.lineno})")
            self.generic_visit(node)

    Visitor().visit(ast.parse(source))
    return found


def test_no_async_check_calls_a_native_diagnostic_on_the_event_loop_thread():
    assert calls_made_on_the_event_loop(SCRIPT.read_text()) == []


def test_the_structural_check_does_catch_the_pattern_it_guards_against():
    """`option_evidence(surface.backend, exc.candidates)` from a coroutine - the v8.54 edit that moved
    the diagnostics onto the main thread - must be seen."""
    source = (
        "async def check(surface):\n"
        "    for line in option_evidence(surface.backend, exc.candidates):\n"
        "        print(line)\n"
        "async def fine(surface):\n"
        "    for line in await asyncio.to_thread(option_evidence, surface.backend, exc.candidates):\n"
        "        print(line)\n"
        "async def nested(surface):\n"
        "    def sync():\n"
        "        return locate_text(surface.backend, 1, 'x', None)\n"
        "    return await asyncio.to_thread(sync)\n")
    assert calls_made_on_the_event_loop(source) == ["check calls option_evidence on the event loop (line 2)"]


async def test_the_diagnostics_run_on_a_worker_thread_when_the_checks_call_them(cn, monkeypatch, tmp_path, capsys):
    """Behaviour, not just text: each one is entered from a thread other than the event loop's."""
    pytest.importorskip("PIL")
    main = threading.main_thread()
    seen: dict[str, threading.Thread] = {}

    def spy(name, result):
        def run(*args, **kwargs):
            seen[name] = threading.current_thread()
            return result
        return run

    monkeypatch.setattr(cn, "option_evidence", spy("option_evidence", []))
    textedit = TextEdit(where="icloud")
    assert await cn.guarded("x", cn.textedit_dropdown(surface_for({"TextEdit": (303, textedit.app)}, "TextEdit")))

    monkeypatch.setattr(cn, "locate_text", spy("locate_text", ["l"]))
    monkeypatch.setattr(cn, "text_structure", spy("text_structure", ["t"]))
    calculator = Calculator(display_as="number")
    cn.read_clipboard = lambda: "12"
    calculator.press = lambda key: None
    await cn.calculator_click(surface_for({"Calculator": (101, calculator.app)}, "Calculator", ClickingInput(calculator)))

    monkeypatch.setattr(cn, "drag_evidence", spy("drag_evidence", "evidence"))
    monkeypatch.setenv("HOME", str(tmp_path))
    finder = Finder(tmp_path)
    await cn.finder_drag(finder_surface(finder, finder_input(finder, accepts=False)))

    assert set(seen) == {"option_evidence", "locate_text", "text_structure", "drag_evidence"}
    assert all(thread is not main for thread in seen.values()), {k: v.name for k, v in seen.items()}


async def test_a_unique_qualified_desktop_is_chosen_without_asking(cn, capsys):
    only_one = [entry for index, entry in enumerate(TextEdit.ICLOUD) if index != 9]   # the second Desktop is gone
    textedit = TextEdit(where="icloud", menu=only_one)
    surface = surface_for({"TextEdit": (303, textedit.app)}, "TextEdit")
    assert await cn.guarded("x", cn.textedit_dropdown(surface)) is True
    out = capsys.readouterr().out
    assert "refused" not in out and "✓ the pop-up now says Desktop" in out
    assert textedit.pressed == ["2:\u2068Desktop\u2069 — iCloud"]


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
    """A Finder list-view window for the check's throwaway folder.

    Each row is an icon, a name and a date column, as a list view lays them out — the row spans the
    whole width, the name's text is only as wide as its letters although its frame is the column's.
    With ``expose_icon=False`` the icon is drawn (and draggable) but Accessibility doesn't list it."""

    ROW_X, ROW_W, ICON, NAME_X, NAME_W, LETTER = 20, 400, (22, 16, 16), 42, 200, 7

    def __init__(self, home: Path, *, title: str | None = None, expose_icon: bool = True):
        self.base = home / "Desktop" / f"JARVIS-check-{os.getpid()}"
        self.log: list[str] = []
        self.rows = {"drag-me.txt": 100, "target": 130}

        def row(name, y):
            icon = [El("AXImage", actions=(), frame=(self.ICON[0], y + 3, 16, 16))] if expose_icon else []
            return El("AXRow", actions=(), frame=(self.ROW_X, y, self.ROW_W, 22), children=[
                El("AXCell", actions=(), frame=(self.ROW_X, y, self.ROW_W, 22), children=[
                    *icon,
                    El("AXStaticText", value=name, actions=(), frame=(self.NAME_X, y + 2, self.NAME_W, 18)),
                    El("AXStaticText", value="--", actions=(), frame=(260, y, 60, 18))])])

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
        self._link(self.window)

    def _link(self, element: El) -> None:
        for child in element.children:
            child.attrs["AXParent"] = element
            self._link(child)

    def hit(self, x: float, y: float) -> str | None:
        """What a list view treats as the item at a point: its icon or its name's letters — not the
        blank rest of the row. ("file" / "folder" / None.)"""
        for name, top in self.rows.items():
            if not top <= y < top + 22:
                continue
            on_icon = self.ICON[0] <= x <= self.ICON[0] + self.ICON[1]
            on_name = self.NAME_X <= x <= self.NAME_X + self.LETTER * len(name)
            if on_icon or on_name:
                return "file" if name == "drag-me.txt" else "folder"
        return None

    def over_folder_row(self, x: float, y: float) -> bool:
        return self.ROW_X <= x <= self.ROW_X + self.ROW_W and self.rows["target"] <= y < self.rows["target"] + 22


class FinderMac(FakeBackend):
    """Accessibility's answer to "what is at this point", from the window's own frames."""

    def element_at(self, x, y):
        found = None
        stack = [self.apps["Finder"][1].attrs["AXFocusedWindow"]]
        while stack:
            element = stack.pop()
            px, py = element.attrs["AXPosition"] or (0, 0)
            w, h = element.attrs["AXSize"] or (0, 0)
            if px <= x <= px + w and py <= y <= py + h and w and h:
                found = element
                stack.extend(element.children)
        return found


class FinderPoster:
    """What Finder's list view does with the mouse events it is sent — as far as this repository
    can model it, which is *a model, not a measurement*. A press takes hold of the file only on
    its icon or its name. The drag begins once the pointer has moved past a threshold and had a
    moment to. The drop is taken if the pointer has been seen over the folder for a moment before
    the button comes up. (The real answer is what ``check_native.py --controls`` finds.)"""

    THRESHOLD, BEGIN_S, DWELL_S = 4.0, 0.1, 0.25

    def __init__(self, finder: Finder, clock: list[float], *, accepts: bool = True):
        self.finder, self.clock, self.accepts = finder, clock, accepts
        self.held = False
        self.pressed_at = (0.0, 0.0)
        self.pressed_when = 0.0
        self.began: float | None = None
        self.over_since: float | None = None

    def mouse(self, kind, x, y, button="left", state=1):
        now = self.clock[0]
        if kind == "down":
            self.held = self.finder.hit(x, y) == "file"
            self.pressed_at, self.pressed_when, self.began, self.over_since = (x, y), now, None, None
        elif kind == "drag" and self.held:
            moved = ((x - self.pressed_at[0]) ** 2 + (y - self.pressed_at[1]) ** 2) ** 0.5
            if self.began is None and moved >= self.THRESHOLD and now - self.pressed_when >= self.BEGIN_S:
                self.began = now
            over = self.began is not None and self.finder.over_folder_row(x, y)
            self.over_since = (self.over_since if self.over_since is not None else now) if over else None
        elif kind == "up":
            dropped = (self.accepts and self.held and self.began is not None
                       and self.over_since is not None and now - self.over_since >= self.DWELL_S
                       and self.finder.over_folder_row(x, y))
            self.held = False
            if dropped:
                (self.finder.base / "drag-me.txt").rename(self.finder.base / "target" / "drag-me.txt")


def finder_input(finder: Finder, *, accepts: bool = True):
    """The real input device, posting to a Finder model on a clock that only its own sleeps advance."""
    from jarvis.surfaces.native.input import NativeInput

    clock = [0.0]

    def sleep(seconds):
        clock[0] += seconds

    return NativeInput(FinderPoster(finder, clock, accepts=accepts), sleep=sleep)


@pytest.fixture
def home(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


def finder_surface(finder: Finder, input_device=None) -> NativeSurface:
    return NativeSurface(backend=FinderMac({"Finder": (202, finder.app)}, front="Finder"),
                         input=input_device or finder_input(finder), sleep=lambda _s: None)


@pytest.mark.parametrize("expose_icon", [True, False], ids=["icon listed", "icon not listed"])
async def test_a_file_dragged_onto_a_folder_is_checked_on_disk_and_cleaned_up(cn, home, capsys, expose_icon):
    finder = Finder(home, expose_icon=expose_icon)
    surface = finder_surface(finder)
    assert await cn.guarded("x", cn.finder_drag(surface)) is True
    assert "✓ the file is inside the folder" in capsys.readouterr().out
    assert surface.last_drag["from"].on == ("icon" if expose_icon else "name")
    assert finder.log == ["as List", "Close Window"]
    assert not finder.base.exists(), "the throwaway folder is removed"


def test_the_middle_of_a_row_is_not_the_file_so_a_drag_from_there_moves_nothing(home):
    """The failure on a real Mac: the drag started at the centre of the row — blank space in the list
    view — and Finder treated it as a rubber band. The model of Finder here must say the same."""
    finder = Finder(home)
    row = finder.file_row.attrs
    middle = (row["AXPosition"][0] + row["AXSize"][0] / 2, row["AXPosition"][1] + row["AXSize"][1] / 2)
    assert finder.hit(*middle) is None, "the model: the row's middle is blank"
    assert finder.hit(30, 111) == "file" and finder.hit(60, 111) == "file"
    finder.base.mkdir(parents=True)
    (finder.base / "target").mkdir()
    (finder.base / "drag-me.txt").write_text("x")
    finder_input(finder).drag(middle, (60, 141))
    assert (finder.base / "drag-me.txt").exists() and not (finder.base / "target" / "drag-me.txt").exists()


def test_a_gesture_without_the_pauses_is_not_taken_either(home):
    from jarvis.surfaces.native.input import NativeInput

    finder = Finder(home)
    clock = [0.0]
    poster = FinderPoster(finder, clock)
    finder.base.mkdir(parents=True)
    (finder.base / "target").mkdir()
    (finder.base / "drag-me.txt").write_text("x")
    quick = NativeInput(poster, sleep=lambda _s: None)           # press, a jump, release: no time passes
    quick.drag((30, 111), (60, 141))
    assert (finder.base / "drag-me.txt").exists(), "no time to begin the drag, no time over the target"


async def test_a_move_that_lands_a_moment_after_the_drop_is_a_pass_and_one_that_never_does_is_not(cn, home, monkeypatch):
    finder = Finder(home)
    surface = finder_surface(finder, finder_input(finder, accepts=False))
    polls = []

    async def slow(seconds):
        if surface.last_drag is not None:
            polls.append(seconds)
            if len(polls) == 6:
                (finder.base / "drag-me.txt").rename(finder.base / "target" / "drag-me.txt")

    monkeypatch.setattr(cn, "pause", slow)
    assert await cn.guarded("x", cn.finder_drag(surface)) is True
    assert len(polls) == 6, "it stopped looking as soon as the postcondition held"

    again = Finder(home)
    never = finder_surface(again, finder_input(again, accepts=False))
    polls.clear()

    async def nothing(seconds):
        if never.last_drag is not None:
            polls.append(seconds)

    monkeypatch.setattr(cn, "pause", nothing)
    assert await cn.guarded("x", cn.finder_drag(never)) is False
    assert len(polls) == 16, "...and the wait is bounded: four seconds of quarter-second looks"


async def test_a_drag_that_moved_nothing_is_a_failure_that_says_where_it_pressed_and_what_was_there(cn, home, capsys):
    finder = Finder(home)
    surface = finder_surface(finder, finder_input(finder, accepts=False))          # Finder ignores the drop
    assert await cn.guarded("x", cn.finder_drag(surface)) is False
    out = capsys.readouterr().out
    assert "✗ the file is inside the folder" in out
    assert "Dragged “drag-me.txt · --” onto “target · --”" in out, \
        "rows are named as the listing names them, by the text inside — not the empty “” of before"
    assert "on disk: ['drag-me.txt', 'target']" in out
    assert f"{finder.base / 'drag-me.txt'} still exists; {finder.base / 'target' / 'drag-me.txt'} does not exist" in out
    assert "afterwards the file's row is not selected, the folder's row is not selected" in out
    assert "pressed at (30, 111) on its icon in the row at (20, 100, 400×22)" in out
    assert "released at (30, 141) on its icon in the row at (20, 130, 400×22)" in out
    assert "under that point: AXImage ‹ AXCell ‹ AXRow ‹ AXOutline" in out
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
    def __init__(self, available=True, **kwargs):
        self._available = available
        self.kwargs = kwargs
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

    monkeypatch.setattr(cn, "NativeSurface", lambda **kwargs: _Stub(**kwargs))
    monkeypatch.setattr(cn, "look", look)
    monkeypatch.setattr(cn, "controls", controls)
    monkeypatch.setattr("sys.argv", ["check_native.py", *flag])
    assert await cn.main() == 0
    assert ran == expected


def test_the_chain_of_parent_processes_is_followed_to_the_top_and_cannot_loop(cn):
    parents = {900: 800, 800: 700, 700: 1}

    def ps(command, **kwargs):
        pid = int(command[-1])
        return type("Done", (), {"stdout": f"{parents.get(pid, 'junk')}\n"})()

    assert cn.ancestors(900, ps) == [800, 700]
    assert cn.ancestors(555, ps) == [], "garbage from ps ends the walk"
    loop = {10: 20, 20: 10}
    answer = lambda c, **k: type("D", (), {"stdout": f"{loop[int(c[-1])]}\n"})()   # noqa: E731
    assert cn.ancestors(10, answer) == [20, 10], "a cycle is walked once, not for ever"
    itself = lambda c, **k: type("D", (), {"stdout": "5\n"})()                     # noqa: E731
    assert cn.ancestors(5, itself) == [5]


class HostMac(FakeBackend):
    """Terminal (this script's home, with a window) and TextEdit (in front, every window closed)."""

    def app_for_pid(self, pid):
        return {50: (50, "Terminal"), 303: (303, "TextEdit")}.get(pid)


def _host_world():
    shell = El("AXTextArea", description="shell", value="hello world from the terminal", frame=(10, 30, 380, 200))
    window = El("AXWindow", "Shell", actions=(), frame=(0, 0, 400, 300), children=[shell])
    menubar = El("AXMenuBar", actions=(), children=[El("AXMenuBarItem", "Apple"), El("AXMenuBarItem", "Terminal")])
    terminal = El("AXApplication", "Terminal", actions=(), AXWindows=[window], AXFocusedWindow=window, AXMenuBar=menubar)
    textedit = El("AXApplication", "TextEdit", actions=(), AXWindows=[], AXFocusedWindow=None)
    backend = HostMac({"Terminal": (50, terminal), "TextEdit": (303, textedit)}, front="TextEdit")
    return backend, NativeSurface(backend=backend, input=RecordingInput(), sleep=lambda _s: None)


def test_the_app_this_ran_from_is_the_nearest_ancestor_that_is_an_application(cn):
    backend, _ = _host_world()
    assert cn.host_app(backend, [7777, 6666, 50, 1]) == "Terminal", "a shell and a login process are not apps"
    assert cn.host_app(backend, [7777, 6666]) == ""
    assert cn.host_app(object(), [50]) == "", "a backend that can't say leaves the frontmost app as the default"


async def test_the_opening_look_reads_the_app_it_was_run_from_not_whatever_an_earlier_check_left_in_front(
        cn, monkeypatch, tmp_path, capsys):
    """--controls straight after --act: TextEdit, with every window closed, is the app in front."""
    pytest.importorskip("PIL")
    from PIL import Image

    async def capture(pid, number):
        path = tmp_path / "window.png"
        Image.new("RGB", (800, 600), "white").save(path)
        return path

    monkeypatch.setattr(cn, "capture_window", capture)
    monkeypatch.setattr(cn, "recognize_text", lambda path: [TextBox("hello world from the terminal", x=40, y=80, w=300, h=24)])
    backend, surface = _host_world()
    assert await cn.look(surface, "") is False, "as it was: the frontmost app, TextEdit, has nothing to read"
    assert "✗ read the window — TextEdit has no window open" in capsys.readouterr().out
    assert await cn.look(surface, cn.host_app(backend, [50])) is True
    assert "✓ read the window" in capsys.readouterr().out


class _HostStub(_Stub):
    def app_for_pid(self, pid):
        return (pid, "Terminal") if pid == 50 else None


@pytest.mark.parametrize("argv, expected", [([], "Terminal"), (["--app", "Notes"], "Notes")])
async def test_main_starts_at_the_host_app_unless_told_otherwise(cn, monkeypatch, capsys, argv, expected):
    seen = []

    async def look(surface, app):
        seen.append(app)
        return True

    monkeypatch.setattr(cn, "NativeSurface", lambda **kwargs: _HostStub(**kwargs))
    monkeypatch.setattr(cn, "ancestors", lambda pid: [7, 50])
    monkeypatch.setattr(cn, "look", look)
    monkeypatch.setattr("sys.argv", ["check_native.py", *argv])
    assert await cn.main() == 0
    assert seen == [expected]
    assert ("Starting point: Terminal" in capsys.readouterr().out) is (not argv)


async def test_without_the_native_extras_it_says_so_and_does_nothing(cn, monkeypatch, capsys):
    monkeypatch.setattr(cn, "NativeSurface", lambda **kwargs: _Stub(available=False, **kwargs))
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
        text_items = [TextItem(label="", value="‎12")]
        controls = [C("main display", "5")]

    assert cn.shows(Snap, "12"), "direction marks around the number don't hide it"
    assert cn.shows(Snap, "5") and not cn.shows(Snap, "1"), "whole texts only, never part of one"

    class Named:
        texts = ["Edit field: 12"]
        text_items = [TextItem(label="Edit field", value="12")]
        controls: list = []

    assert cn.shows(Named, "12"), "a text's shown value counts even when the app names it something else"
    assert not cn.shows(Named, "Edit"), "...but never a part of its name"
    assert cn.names([C("OK"), C("")], limit=1).endswith("… (2 in all)")


# ---------------------------------------------------------------------------
# --observe
# ---------------------------------------------------------------------------
class ObservingMac(FakeBackend):
    """Calculator and Finder, where bringing an app to the front and opening
    Calculator's View menu each make the app post its notification — unless
    told to stay silent, or (``drop_every``) to forget every Nth one."""

    def __init__(self, driver: FakeDriver, *, silent: bool = False, can_observe: bool = True,
                 drop_every: int = 0, prefilled: bool = False):
        self.driver, self.silent, self.can_observe = driver, silent, can_observe
        self.drop_every, self.posts = drop_every, 0
        zoom_menu = El("AXMenu", actions=(), children=[El("AXMenuItem", "Zoom In")] if prefilled else [])
        view = El("AXMenuBarItem", "View", children=[zoom_menu])

        def opened(_action):
            if not zoom_menu.children:
                zoom_menu.children.append(El("AXMenuItem", "Zoom In"))
            self._post(101, MENU_OPENED)

        view.on_perform = opened
        calculator = El("AXApplication", "Calculator", actions=(), AXWindows=[],
                        AXMenuBar=El("AXMenuBar", actions=(), children=[El("AXMenuBarItem", "Calculator"), view]))
        finder = El("AXApplication", "Finder", actions=(), AXWindows=[], AXMenuBar=El("AXMenuBar", actions=()))
        super().__init__({"Calculator": (101, calculator), "Finder": (202, finder)}, front="Finder")

    def _post(self, pid, notification):
        self.posts += 1
        if self.silent or (self.drop_every and self.posts % self.drop_every == 0):
            return
        self.driver.post(pid, notification)

    def activate(self, pid):
        result = super().activate(pid)
        threading.Timer(0.02, self._post, args=(pid, ACTIVATED)).start()
        return result

    def __getattr__(self, name):
        if name == "observer_driver" and self.can_observe:
            return lambda: self.driver
        raise AttributeError(name)


def _observed_surfaces(**backend_kwargs):
    driver = FakeDriver()
    backend = ObservingMac(driver, **backend_kwargs)
    make = lambda observe: NativeSurface(backend=backend, input=RecordingInput(),   # noqa: E731
                                         sleep=lambda _s: None, observe=observe)
    return make(True), make(False), driver


@pytest.fixture
def quick_loop(cn, monkeypatch):
    """The observer check without its real seconds: three rounds of each wait,
    no breaths between, a short CPU sample, and the event-loop stall numbers
    whatever the test says (baseline first)."""
    readings = [0.002, 0.003]

    async def lag(_seconds):
        return readings.pop(0)

    monkeypatch.setattr(cn, "event_loop_lag", lag)
    monkeypatch.setattr(cn, "ROUNDS", 3)
    monkeypatch.setattr(cn, "ROUND_GAP_S", 0.0)
    monkeypatch.setattr(cn, "CPU_SECONDS", 0.2)
    return readings


async def test_the_observer_check_passes_when_the_notifications_arrive(cn, quick_loop, capsys):
    on, off, driver = _observed_surfaces()
    assert await cn.observe(on, off) is True
    out = capsys.readouterr().out
    assert "✗" not in out and "⚠" not in out
    assert "✓ AXApplicationActivated arrives — median" in out
    assert "✓ AXMenuOpened arrives — median" in out
    assert "✓ no notification was missed — 0 of 6 waits got no notification" in out
    assert "✓ no callback raised" in out and "✓ the observer costs next to no CPU" in out
    assert "✓ no thread is left behind" in out and "✓ waits still work with the observer stopped" in out
    assert "25 of 25 subscribed" in out
    assert ("quit", "Calculator") in cn.calls
    assert ("key", "escape", 1) in on.input.events, "the menu the check opened is closed again"
    assert not driver.observed, "everything subscribed was unsubscribed"


async def test_a_menu_that_is_already_filled_in_has_nothing_to_wait_for_and_is_not_counted_as_a_saving(cn, quick_loop, capsys):
    on, off, _ = _observed_surfaces(prefilled=True)
    assert await cn.observe(on, off) is True
    out = capsys.readouterr().out
    assert "✓ AXMenuOpened arrives — median" in out and "polling had nothing to wait for" in out
    assert cn.RUN.extra["observer_usefulness"]["waits"] == 3, "only the three activations, not the menus"


async def test_what_the_observer_is_worth_is_reported_as_a_finding_not_a_pass_or_fail(cn, quick_loop, capsys):
    on, off, _ = _observed_surfaces()
    await cn.observe(on, off)
    out = capsys.readouterr().out
    assert "ℹ usefulness: a 50 ms poll would have ended these waits a median" in out
    finding = cn.RUN.extra["observer_usefulness"]
    assert finding["waits"] >= 1 and set(finding) >= {"median_saving_ms", "best_ms", "worst_ms", "verdict"}
    assert not any(step["label"].startswith("usefulness") for step in cn.RUN.steps)


async def test_notifications_that_never_arrive_are_a_failure_the_output_shows(cn, quick_loop, capsys):
    on, off, _ = _observed_surfaces(silent=True)
    assert await cn.observe(on, off) is False
    out = capsys.readouterr().out
    assert "✗ AXApplicationActivated arrives — median never; polling saw the app in front at" in out
    assert "✗ AXMenuOpened arrives — median never" in out
    assert "✗ no notification was missed — 6 of 6 waits got no notification" in out
    assert "no wait produced both a notification and a poll to compare" in out


async def test_a_notification_missed_now_and_then_is_caught_even_though_most_arrive(cn, quick_loop, capsys):
    on, off, _ = _observed_surfaces(drop_every=4)
    assert await cn.observe(on, off) is False
    out = capsys.readouterr().out
    assert "✓ AXApplicationActivated arrives" in out
    assert re.search(r"✗ no notification was missed — [1-9]\d* of 6 waits got no notification", out)


async def test_an_observer_that_cannot_subscribe_is_reported_not_crashed_on(cn, quick_loop, capsys):
    on, off, _ = _observed_surfaces(can_observe=False)
    assert await cn.observe(on, off) is False
    out = capsys.readouterr().out
    assert "✗ AXApplicationActivated arrives — no subscription was made" in out
    assert "✗ AXMenuOpened arrives — no subscription was made" in out
    assert "✗ no callback raised — there is no observer thread" in out


async def test_a_callback_that_raised_is_a_failure(cn, quick_loop, capsys):
    on, off, driver = _observed_surfaces()
    driver.errors = 2
    assert await cn.observe(on, off) is False
    assert "✗ no callback raised — observer thread stats:" in capsys.readouterr().out


@pytest.mark.parametrize("cpu, expected", [(0.30, "30.0% of one core while waiting"), (None, "no subscription, so nothing")])
async def test_an_observer_that_burns_cpu_or_cannot_be_measured_is_a_failure(cn, quick_loop, monkeypatch, capsys, cpu, expected):
    on, off, _ = _observed_surfaces()

    async def sampled(surface, pid, seconds=None):
        return cpu

    monkeypatch.setattr(cn, "observer_cpu", sampled)
    assert await cn.observe(on, off) is False
    assert f"✗ the observer costs next to no CPU — {expected}" in capsys.readouterr().out


async def test_a_thread_that_stalls_the_event_loop_is_a_failure(cn, quick_loop, capsys):
    on, off, _ = _observed_surfaces()
    quick_loop[:] = [0.003, 0.2]                       # 200 ms stalls while it spins, 3 ms without
    assert await cn.observe(on, off) is False
    assert "✗ the event loop stays free while the observer spins — worst stall 200 ms with it, 3 ms without" \
        in capsys.readouterr().out


async def test_an_observer_that_makes_the_wait_slower_is_a_failure(cn, quick_loop, monkeypatch, capsys):
    on, off, _ = _observed_surfaces()
    monkeypatch.setattr(cn, "average_front",
                        lambda surface, away, to, count=5: (0.3, count) if surface is on else (0.1, count))
    assert await cn.observe(on, off) is False
    assert "✗ bringing an app to the front is no slower with it — 300 ms with, 100 ms without" \
        in capsys.readouterr().out


async def test_an_app_that_never_comes_to_the_front_with_the_observer_is_a_failure(cn, quick_loop, monkeypatch, capsys):
    on, off, _ = _observed_surfaces()
    monkeypatch.setattr(cn, "average_front",
                        lambda surface, away, to, count=5: (0.1, 0 if surface is on else count))
    assert await cn.observe(on, off) is False
    assert "✗ bringing an app to the front is no slower with it" in capsys.readouterr().out


async def test_waits_that_stop_working_once_the_observer_is_stopped_are_a_failure(cn, quick_loop, monkeypatch, capsys):
    on, off, _ = _observed_surfaces()
    real = cn.average_front
    calls = []

    def average(surface, away, to, count=5):
        calls.append(count)
        return real(surface, away, to, count) if count != 3 else (0.1, 0)

    monkeypatch.setattr(cn, "average_front", average)
    assert await cn.observe(on, off) is False
    assert "✗ waits still work with the observer stopped — the app was in front afterwards 0/3 times" \
        in capsys.readouterr().out


async def test_a_thread_still_running_after_the_surface_is_closed_is_a_failure(cn, quick_loop, capsys):
    on, off, _ = _observed_surfaces()
    real_close = on.close
    on.close = lambda: None                            # a close that doesn't stop the thread
    try:
        assert await cn.observe(on, off) is False
        assert "✗ no thread is left behind — still running: ['jarvis-ax-observer']" in capsys.readouterr().out
    finally:
        real_close()


async def test_the_saving_over_a_poll_is_what_the_next_50ms_tick_would_have_cost(cn):
    saving = cn.estimated_saving
    assert saving({"watched": True, "fired": 0.020, "seen": 0.012}) == pytest.approx(0.030)
    assert saving({"watched": True, "fired": 0.005, "seen": 0.012}) == pytest.approx(0.038), \
        "a notification before the change is seen can't help before the change"
    assert saving({"watched": True, "fired": 0.200, "seen": 0.010}) == pytest.approx(-0.150), \
        "a late notification is slower than the poll"
    assert saving({"watched": True, "fired": None, "seen": 0.010}) is None
    assert saving({"watched": False}) is None


async def test_a_blocking_call_that_never_returns_is_reported_as_a_deadlock_not_waited_on_forever(cn):
    import time as real_time

    def stuck():
        real_time.sleep(0.5)

    with pytest.raises(RuntimeError, match="stuck didn't finish within 0 s — a deadlock"):
        await cn.bounded(stuck, timeout_s=0.05)


async def test_the_event_loop_lag_measure_reports_a_real_stall(cn):
    import time

    async def stalls():
        await cn.asyncio.sleep(0.02)
        time.sleep(0.15)                              # something blocking the loop

    task = cn.asyncio.ensure_future(stalls())
    assert await cn.event_loop_lag(0.3) >= 0.1
    await task


@pytest.mark.parametrize("flag, expected", [([], []), (["--observe"], [{"observe": True}, {"observe": False}])])
async def test_the_observe_flag_runs_the_observer_check_with_one_surface_each_way(cn, monkeypatch, flag, expected):
    made = []

    class Recording(_Stub):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            made.append(kwargs)

    async def look(surface, app):
        return True

    async def observe(on, off):
        return True

    monkeypatch.setattr(cn, "NativeSurface", Recording)
    monkeypatch.setattr(cn, "look", look)
    monkeypatch.setattr(cn, "observe", observe)
    monkeypatch.setattr("sys.argv", ["check_native.py", *flag])
    assert await cn.main() == 0
    assert made == [{}, *expected]


# ---------------------------------------------------------------------------
# --stale
# ---------------------------------------------------------------------------
@pytest.fixture
def fx():
    spec = importlib.util.spec_from_file_location("ax_fixture_script", SCRIPT.parent / "ax_fixture.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeFixtureView:
    """ax_fixture.Fixture's view, drawn on the fake accessibility tree. With
    *keep_references*, a removed control stays valid — as if macOS handed back
    the same reference after a rebuild."""

    def __init__(self, backend: FakeBackend, log: list[str], *, keep_references: bool = False,
                 process_name: str = "JARVIS Fixture", steals_focus: bool = False, axpress: str = "works",
                 enabled: bool = True):
        """*axpress*: ``works``; ``absent`` (the button offers no AXPress); ``silent`` (the app accepts
        AXPress and records nothing). Refusing AXPress is the backend's doing (``StaleMac``)."""
        self.backend, self.log, self.keep, self.steals_focus = backend, log, keep_references, steals_focus
        self.axpress, self.enabled = axpress, enabled
        self.window = El("AXWindow", "JARVIS Fixture", actions=(), frame=(0, 0, 420, 300))
        self.other = El("AXWindow", "JARVIS Fixture (other)", actions=(), frame=(500, 0, 420, 300))
        self.app = El("AXApplication", "JARVIS Fixture", actions=(), AXWindows=[self.window],
                      AXFocusedWindow=self.window, AXMenuBar=El("AXMenuBar", actions=()))
        backend.apps[process_name] = (303, self.app)
        self.saves: list[El] = []

    def _control(self, title, identifier, kind, born, x):
        control = El("AXButton" if kind == "button" else "AXCheckBox", title, frame=(x, 240, 90, 28),
                     AXIdentifier=identifier, actions=() if self.axpress == "absent" else ("AXPress",),
                     enabled=self.enabled)
        verb = "click" if kind == "button" else "toggle"
        def performed(action):
            if self.axpress == "silent" and action == "AXPress":
                return
            self.log.append(f"{verb}:{control.attrs['AXTitle']}:{identifier}:born={born}")
            if self.steals_focus:
                self.backend.activate(303)

        control.on_perform = performed
        return control

    def _drop(self, controls, window):
        for control in controls:
            if control in window.children:
                window.children.remove(control)
            control.alive = self.keep

    def build(self, generation, *, saves, kind, title, distinct_ids=False):
        self._drop(self.saves, self.window)
        self.app.attrs["AXWindows"] = [self.window]
        self.saves = [self._control(title, f"save{i + 1}" if distinct_ids and i else "save", kind, generation,
                                    20 + 100 * i) for i in range(saves)]
        self.window.children.extend(self.saves)

    def rename(self, title):
        for control in self.saves:
            control.attrs["AXTitle"] = title

    def move(self, generation):
        self._drop(self.saves, self.window)
        moved = self._control("Save", "save", "button", generation, 20)
        self.other.children[:] = [moved]
        self.app.attrs["AXWindows"] = [self.window, self.other]
        self.saves = [moved]

    def quit(self):
        pass

    def state(self):
        return {"saves": len(self.saves)}


class FakeFixtureProcess:
    """check_native.FixtureProcess, in place of a second process: the real
    command dispatcher, over the fake tree."""

    def __init__(self, view: FakeFixtureView, log: list[str], fx):
        self.fixture, self.log = fx.Fixture(view), log

    def send(self, command, timeout_s=5.0):
        name, _, argument = command.partition(" ")
        return self.fixture.apply(name, argument)

    def events(self):
        return list(self.log)


class StaleMac(FakeBackend):
    """The fixture's Mac, where the app can refuse AXPress and where bringing an app forward can fail."""

    refuse_axpress = False
    activation_works = True

    def perform(self, element, action):
        if action == "AXPress" and self.refuse_axpress:
            return False
        return super().perform(element, action)

    def activate(self, pid):
        if pid == 303 and not self.activation_works:      # the fixture refuses to come forward
            self.activations.append(pid)
            return False
        return super().activate(pid)


class PressingInput(RecordingInput):
    """Input whose click presses the fixture control it lands on, as the real app would see it."""

    view = None

    def click(self, x, y, *, button="left", clicks=1):
        super().click(x, y, button=button, clicks=clicks)
        for control in list(self.view.window.children):
            frame = control.attrs["AXPosition"], control.attrs["AXSize"]
            (px, py), (w, h) = frame
            if px <= x <= px + w and py <= y <= py + h and control.on_perform:
                control.on_perform("click")


def _stale_setup(fx, *, keep_references=False, steals_focus=False, axpress="works", refuse_axpress=False,
                 activation_works=True, enabled=True):
    backend = StaleMac({"Finder": (202, El("AXApplication", "Finder", actions=(), AXWindows=[],
                                           AXMenuBar=El("AXMenuBar", actions=()))),
                        "Notes": (101, El("AXApplication", "Notes", actions=(), AXWindows=[],
                                          AXMenuBar=El("AXMenuBar", actions=()))), }, front="Notes")
    backend.refuse_axpress, backend.activation_works = refuse_axpress, activation_works
    log: list[str] = []
    view = FakeFixtureView(backend, log, keep_references=keep_references, steals_focus=steals_focus,
                           axpress=axpress, enabled=enabled)
    device = PressingInput()
    device.view = view
    surface = NativeSurface(backend=backend, input=device, sleep=lambda _s: None)
    return surface, FakeFixtureProcess(view, log, fx), view, log


async def test_every_stale_scenario_goes_as_it_must_on_a_surface_that_behaves(cn, fx, capsys):
    surface, fixture, _, log = _stale_setup(fx)
    assert await cn.stale_checks(surface, fixture) is True
    out = capsys.readouterr().out
    assert "✗" not in out and "⚠" not in out
    for label in ("a press works with another app in front", "a rebuilt button is re-found and pressed",
                  "two buttons told apart by identifier: the right one is pressed",
                  "two identical buttons: none is guessed", "a look-alike of another kind is not pressed",
                  "a button renamed in place is not pressed",
                  "the same name in another window is not pressed"):
        assert f"✓ {label}" in out
    assert (surface.relocations, surface.relocations_refused) == (2, 4)
    assert len(log) == 3, "the background press, the re-found rebuild and the identifier-matched twin — nothing else"


async def test_a_surface_that_guesses_between_identical_buttons_is_caught_pressing_the_wrong_one(cn, fx, capsys):
    from jarvis.surfaces.native import ax as axmod

    surface, fixture, view, log = _stale_setup(fx)
    original = surface._relocate

    def guesses(handle):
        # A "relocate" that takes the first Save when there are several.
        saves = [c for c in view.window.children if c.attrs["AXTitle"] == "Save"]
        if len(saves) > 1:
            return saves[0], surface.backend.attributes(saves[0], axmod.ATTRIBUTES)
        return original(handle)

    surface._relocate = guesses
    assert await cn.stale_checks(surface, fixture) is False
    out = capsys.readouterr().out
    assert "✗ two identical buttons: none is guessed — PRESSED click “Save” (save, build" in out
    assert any(line.startswith("click:Save:save:") for line in log)


async def test_references_macos_keeps_valid_make_the_rebuild_checks_inconclusive_not_passes(cn, fx, capsys):
    surface, fixture, _, _ = _stale_setup(fx, keep_references=True)
    assert await cn.stale_checks(surface, fixture) is False
    out = capsys.readouterr().out
    assert "⚠ a rebuilt button is re-found and pressed — macOS kept the old reference valid" in out
    assert "⚠ two identical buttons: none is guessed" in out
    assert "✗ a rebuilt button" not in out


@pytest.mark.parametrize("setup, ok, expected", [
    ({}, True, ["✓ a press works with another app in front — AXPress succeeded, with no click and no activation",
                "the surface returned 'Pressed “Save”.'", "the window recorded 1 press(es); Finder stayed in front"]),
    ({"steals_focus": True}, False,
     ["✗ a press works with another app in front — AXPress succeeded, JARVIS posted no click and activated nothing, "
      "and the app still came forward: AXPress (or the app's handling of it) activated the target",
      "the surface returned 'Pressed “Save”.'", "the window recorded 1 press(es); Finder was no longer in front"]),
    ({"refuse_axpress": True}, False,
     ["✗ a press works with another app in front — AXPress did not do the press: AXPress was refused by the app "
      "(perform returned False), so the surface fell back to a coordinate click; the fallback click brought the app "
      "forward", "the surface returned 'Clicked “Save”.'", "the window recorded 1 press(es); Finder was no longer in front"]),
    ({"axpress": "absent"}, False,
     ["✗ a press works with another app in front — AXPress did not do the press: the button does not offer AXPress, "
      "so the surface fell back to a coordinate click; the fallback click brought the app forward",
      "the surface returned 'Clicked “Save”.'"]),
    ({"refuse_axpress": True, "activation_works": False}, False,
     ["fell back to a coordinate click; the fallback click ran, the app did not come forward",
      "the window recorded 1 press(es); Finder stayed in front"]),
    ({"axpress": "silent"}, False,
     ["✗ a press works with another app in front — AXPress succeeded but the app recorded no press",
      "the surface returned 'Pressed “Save”.'", "the window recorded 0 press(es); Finder stayed in front"]),
    ({"enabled": False}, False,
     ["✗ a press works with another app in front — the press raised instead of acting: “Save” is greyed out right now.",
      "the window recorded 0 press(es); Finder stayed in front"]),
], ids=["background-axpress", "axpress-activates", "axpress-refused", "axpress-absent", "fallback-fails-to-activate",
        "axpress-unrecorded", "raises"])
async def test_the_background_press_names_the_mechanism_that_did_or_did_not_work(cn, fx, capsys, setup, ok, expected):
    surface, fixture, _, _ = _stale_setup(fx, **setup)
    assert await cn.background_press(surface, fixture) is ok
    out = capsys.readouterr().out
    for fragment in expected:
        assert fragment in out, (fragment, out)
    assert ("⚠" in out) is False, "there is no 'couldn't tell' here: activation and fallback are failures, not doubts"


async def test_a_press_that_activates_the_app_stays_a_failure_in_the_whole_stale_run(cn, fx, capsys):
    surface, fixture, _, _ = _stale_setup(fx, steals_focus=True)
    assert await cn.stale_checks(surface, fixture) is False
    out = capsys.readouterr().out
    assert "✗ a press works with another app in front" in out and "⚠ a press works" not in out


async def test_a_fallback_click_is_a_failure_even_when_it_does_the_press_and_nothing_comes_forward(cn, fx, capsys):
    """The contract is a semantic AXPress. A click that happened to work is not that."""
    surface, fixture, _, log = _stale_setup(fx, refuse_axpress=True, activation_works=False)
    assert await cn.background_press(surface, fixture) is False
    out = capsys.readouterr().out
    assert "AXPress did not do the press" in out and "the window recorded 1 press(es); Finder stayed in front" in out
    assert len(log) == 1, "the click did land"


@pytest.mark.parametrize("performed, offered, clicks, summary, error, pressed, behind, ok, expected", [
    ([("AXPress", True)], [["AXPress"]], [], "Pressed “Save”.", "", 1, True, True, "AXPress succeeded, with no click"),
    ([("AXPress", True)], [["AXPress"]], [], "Pressed “Save”.", "", 1, False, False, "activated the target"),
    ([("AXPress", True)], [["AXPress"]], [], "Pressed “Save”.", "", 0, True, False, "app recorded no press"),
    ([("AXPress", False)], [["AXPress"]], [((1, 2), {})], "Clicked “Save”.", "", 1, False, False, "was refused by the app"),
    ([], [[]], [((1, 2), {})], "Clicked “Save”.", "", 1, False, False, "does not offer AXPress"),
    ([], [], [((1, 2), {})], "Clicked “Save”.", "", 1, False, False, "(no action list was read)"),
    ([("AXShowMenu", True)], [["AXShowMenu"]], [], "Opened the menu for “Save”.", "", 1, True, False,
     "not an AXPress: the button does not offer AXPress; it was done by AXShowMenu"),
    ([], [["AXPress"]], [], "", "", 0, True, False, "AXPress was offered but never tried; and no click was posted either"),
    ([("AXPress", True)], [["AXPress"]], [((1, 2), {})], "Pressed “Save”.", "", 1, True, False,
     "AXPress succeeded and a coordinate click was posted as well"),
    ([], [], [], "", "“Save” is greyed out right now.", 0, True, False,
     "the press raised instead of acting: “Save” is greyed out right now.; the window recorded"),
])
def test_the_verdict_for_each_way_a_press_can_go(cn, performed, offered, clicks, summary, error, pressed, behind, ok,
                                                 expected):
    trace = cn.PressTrace.__new__(cn.PressTrace)
    trace.performed, trace.offered, trace.clicks = list(performed), list(offered), list(clicks)
    verdict, text = cn.press_verdict(trace, summary, error, pressed, behind, "Finder")
    assert verdict is ok and expected in text, text


async def test_the_trace_records_the_seams_a_press_acts_through_and_puts_them_back(cn, fx):
    surface, fixture, view, _ = _stale_setup(fx, refuse_axpress=True)
    await asyncio.to_thread(fixture.send, "restore")
    handle, _ = await cn.fixture_handle(surface)
    backend, device = surface.backend, surface.input
    assert "perform" not in vars(backend) and "click" not in vars(device)
    with cn.PressTrace(surface) as trace:
        summary = await surface.press(handle)
    assert summary == "Clicked “Save”."
    assert trace.performed == [("AXPress", False)] and trace.offered_axpress and trace.axpress is False
    assert len(trace.clicks) == 1
    assert not {"actions", "perform"} & set(vars(backend)) and "click" not in vars(device), "the originals are back"


async def test_the_trace_puts_the_seams_back_when_the_press_raises(cn, fx):
    surface, fixture, view, _ = _stale_setup(fx, enabled=False)
    await asyncio.to_thread(fixture.send, "restore")
    handle, _ = await cn.fixture_handle(surface)
    with pytest.raises(NativeError), cn.PressTrace(surface):
        await surface.press(handle)
    assert not {"actions", "perform"} & set(vars(surface.backend)) and "click" not in vars(surface.input)


async def test_a_trace_that_found_a_wrapper_already_in_place_restores_that_wrapper(cn, fx):
    surface, _, _, _ = _stale_setup(fx)
    marker = surface.backend.perform
    surface.backend.perform = marker              # an instance attribute, as another tool might have put there
    with cn.PressTrace(surface):
        assert surface.backend.perform is not marker
    assert surface.backend.perform is marker


async def test_a_twin_pressed_by_the_wrong_identifier_is_a_failure(cn):
    outcome = dict(error=None, original=1, changed=2, relocated=1, refused=0,
                   events=[{"kind": "click", "name": "Save", "identifier": "save2", "born": 2}])
    assert cn.judge("twin", outcome)[0] == "fail"
    outcome["events"][0]["identifier"] = "save"
    assert cn.judge("twin", outcome)[0] == "ok"


async def test_a_page_with_no_save_button_says_what_it_did_show(cn, fx, capsys):
    surface, fixture, view, _ = _stale_setup(fx)
    original = fixture.send

    def send(command, timeout_s=5.0):
        state = original(command)
        if command == "restore":
            view.window.children.clear()
        return state

    fixture.send = send
    assert await cn.stale_checks(surface, fixture) is False
    assert "no Save button to take a handle to; the window showed:" in capsys.readouterr().out


@pytest.mark.parametrize("scenario, outcome, expected", [
    ("rebuild", dict(error=None, events=[("click", 2)], original=1, changed=2, relocated=1, refused=0), "ok"),
    ("rebuild", dict(error=None, events=[("click", 1)], original=1, changed=2, relocated=0, refused=0), "inconclusive"),
    ("rebuild", dict(error="gone", events=[], original=1, changed=2, relocated=0, refused=1), "fail"),
    ("rebuild", dict(error=None, events=[("click", 2)], original=1, changed=2, relocated=0, refused=0), "fail"),
    ("twin", dict(error=None, events=[("click", 2)], original=1, changed=2, relocated=1, refused=0), "ok"),
    ("duplicate", dict(error="gone", events=[], original=1, changed=2, relocated=0, refused=1), "ok"),
    ("duplicate", dict(error="other", events=[], original=1, changed=2, relocated=0, refused=0), "inconclusive"),
    ("duplicate", dict(error=None, events=[], original=1, changed=2, relocated=0, refused=0), "fail"),
    ("duplicate", dict(error=None, events=[("click", 2)], original=1, changed=2, relocated=0, refused=0), "fail"),
    ("duplicate", dict(error=None, events=[("click", 1)], original=1, changed=2, relocated=0, refused=0), "inconclusive"),
    ("impostor", dict(error=None, events=[("toggle", 2)], original=1, changed=2, relocated=0, refused=0), "fail"),
])
async def test_the_verdict_for_each_outcome(cn, scenario, outcome, expected):
    outcome = {**outcome, "events": [{"kind": kind, "name": "Save", "identifier": "save", "born": born}
                                     for kind, born in outcome["events"]]}
    assert cn.judge(scenario, outcome)[0] == expected


async def test_events_are_read_back_from_the_fixtures_own_log(cn):
    assert cn.parse_event("click:Save:save:born=3") == {"kind": "click", "name": "Save",
                                                         "identifier": "save", "born": 3}
    assert cn.parse_event("toggle:Save:save2:born=12")["kind"] == "toggle"


# --- the client's file protocol, against the real command handling ----------------------------
class _Child:
    def __init__(self, exits_with=None):
        self.exits_with = exits_with
        self.terminated = False

    def poll(self):
        return self.exits_with

    def wait(self, timeout=None):
        return 0

    def terminate(self):
        self.terminated = True


async def test_the_client_starts_the_window_sends_commands_and_reads_the_presses(cn, fx, tmp_path, monkeypatch):
    import threading

    monkeypatch.setattr(cn.subprocess, "Popen", lambda *a, **k: _Child())
    process = cn.FixtureProcess(tmp_path)
    log = fx.Log(process.log_path)
    log("ready:4242")

    class Plain:                                   # a view that only needs to answer the dispatcher
        def build(self, generation, **kw): pass
        def rename(self, title): pass
        def move(self, generation): pass
        def quit(self): pass
        def state(self): return {"saves": 1}

    commands = fx.CommandFile(process.commands_path, fx.Fixture(Plain()), log)
    stop = threading.Event()

    def serve():
        while not stop.is_set():
            commands.poll()
            stop.wait(0.01)

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        assert process.start() == 4242
        assert process.send("rebuild") == {"generation": 1, "saves": 1}
        assert process.send("rename Delete") == {"generation": 2, "saves": 1}
        log("click:Save:save:born=1")
        log("something else")
        assert process.events() == ["click:Save:save:born=1"]
        process.stop()
    finally:
        stop.set()
        thread.join()


async def test_a_fixture_that_dies_on_start_says_why(cn, tmp_path, monkeypatch):
    def dies(*args, **kwargs):
        kwargs["stderr"].write("ModuleNotFoundError: No module named 'AppKit'")
        kwargs["stderr"].flush()
        return _Child(exits_with=1)

    monkeypatch.setattr(cn.subprocess, "Popen", dies)
    with pytest.raises(RuntimeError, match="exited.*AppKit"):
        cn.FixtureProcess(tmp_path).start(timeout_s=2.0)


async def test_a_command_nobody_answers_is_an_error_not_a_hang(cn, tmp_path):
    process = cn.FixtureProcess(tmp_path)
    with pytest.raises(RuntimeError, match="didn't answer"):
        process.send("rebuild", timeout_s=0.1)


async def test_a_command_the_window_reports_failing_is_an_error(cn, fx, tmp_path):
    process = cn.FixtureProcess(tmp_path)
    fx.Log(process.log_path)("error:1:RuntimeError: no such control")
    with pytest.raises(RuntimeError, match="failed: RuntimeError: no such control"):
        process.send("rename X", timeout_s=0.5)


async def test_the_fixture_is_found_by_its_process_id_when_its_name_is_just_python(cn, fx, monkeypatch, capsys):
    backend = FakeBackend({"Finder": (202, El("AXApplication", "Finder", actions=(), AXWindows=[],
                                              AXMenuBar=El("AXMenuBar", actions=())))}, front="Finder")
    log: list[str] = []
    view = FakeFixtureView(backend, log, process_name="Python")       # not "JARVIS Fixture"
    surface = NativeSurface(backend=backend, input=RecordingInput(), sleep=lambda _s: None)
    process = FakeFixtureProcess(view, log, fx)
    process.start = lambda: 303
    process.stop = lambda: None
    monkeypatch.setattr(cn, "FixtureProcess", lambda directory: process)
    original = backend.find_app
    assert backend.find_app("JARVIS Fixture") is None
    assert await cn.stale(surface) is True
    assert "✗" not in capsys.readouterr().out
    assert backend.find_app == original, "the lookup is put back"
    assert backend.find_app("JARVIS Fixture") is None


async def test_a_window_that_cannot_be_opened_fails_the_stale_check_with_the_reason(cn, monkeypatch, capsys):
    def start(self, timeout_s=15.0):
        raise RuntimeError("the fixture exited: No module named 'AppKit'")

    monkeypatch.setattr(cn.FixtureProcess, "start", start)
    surface = surface_for({"Finder": (202, El("AXApplication", "Finder", actions=(), AXWindows=[],
                                              AXMenuBar=El("AXMenuBar", actions=())))}, "Finder")
    assert await cn.stale(surface) is False
    assert "✗ the fixture window opened — the fixture exited: No module named 'AppKit'" in capsys.readouterr().out


@pytest.mark.parametrize("flag, expected", [([], []), (["--stale"], ["stale"])])
async def test_the_stale_flag_runs_the_stale_checks_and_only_then(cn, monkeypatch, flag, expected):
    ran = []

    async def look(surface, app):
        return True

    async def stale(surface):
        ran.append("stale")
        return True

    monkeypatch.setattr(cn, "NativeSurface", lambda **kwargs: _Stub(**kwargs))
    monkeypatch.setattr(cn, "look", look)
    monkeypatch.setattr(cn, "stale", stale)
    monkeypatch.setattr("sys.argv", ["check_native.py", *flag])
    assert await cn.main() == 0
    assert ran == expected


async def test_an_inconclusive_step_is_not_a_pass(cn, capsys):
    assert cn.step("a thing", True, "it could not tell", inconclusive=True) is False
    assert cn.step("another", True) is True
    out = capsys.readouterr().out
    assert "⚠ a thing" in out and "✓ another" in out


# ---------------------------------------------------------------------------
# timing, repeating, reporting
# ---------------------------------------------------------------------------
async def test_a_measured_check_times_its_calls_by_phase_and_its_verification(cn, calc):
    calculator, surface = calc
    assert await cn.timed("calculator click", cn.calculator_click, surface) is True
    (run,) = cn.RUN.runs
    seconds = run["timings"]["seconds"]
    assert run["check"] == "calculator click" and run["passed"] is True
    assert seconds["action"] > 0 and seconds["verification"] > 0, "the clipboard reading is verification"
    assert seconds["observation"] > 0 and seconds["refresh"] > 0, "reads before, and after, the first press"
    assert run["timings"]["calls"]["action"] == 6, "clear, 7, +, 5, = and then ⌘C"
    assert {step["check"] for step in cn.RUN.steps} == {"calculator click"}


async def test_the_checks_own_waiting_is_counted_apart_from_jarvis_time(cn):
    class Idle:
        resolve_seconds = 0.0

    with cn.RUN.measure("waiting", Idle()):
        await cn.real_pause(0.05)
    seconds = cn.RUN.runs[0]["timings"]["seconds"]
    assert seconds["settle"] >= 0.05 and seconds["action"] == 0.0


async def test_a_check_that_blows_up_is_a_failed_run_and_the_next_check_still_runs(cn, monkeypatch, capsys):
    async def boom(surface):
        raise RuntimeError("it broke")

    async def fine(surface):
        return cn.step("a good thing", True)

    assert await cn.guarded("first", cn.timed("boom", boom, object())) is False
    assert await cn.guarded("second", cn.timed("fine", fine, object())) is True
    assert [(run["check"], run["passed"]) for run in cn.RUN.runs] == [("boom", False), ("fine", True)]


async def test_every_stale_scenario_is_a_measured_run_with_its_own_verdict(cn, fx):
    surface, fixture, _, _ = _stale_setup(fx)
    await cn.stale_checks(surface, fixture)
    verdicts = {run["check"]: run["passed"] for run in cn.RUN.runs}
    assert verdicts == {"stale: press without focus": True, "stale: rebuild": True, "stale: twin": True,
                        "stale: duplicate": True, "stale: impostor": True, "stale: rename": True,
                        "stale: move": True}


async def test_an_inconclusive_stale_scenario_is_not_a_passing_run(cn, fx):
    surface, fixture, _, _ = _stale_setup(fx, keep_references=True)
    await cn.stale_checks(surface, fixture)
    assert {run["check"]: run["passed"] for run in cn.RUN.runs}["stale: rebuild"] is False


class _MainStub(_Stub):
    pass


def _patch_main(cn, monkeypatch, *, look_ok=True, argv=()):
    ran = {"look": 0}

    async def look(surface, app):
        ran["look"] += 1
        return cn.step("read the window", look_ok, "") if True else True

    async def noop(surface):
        return True

    async def observe(on, off):
        return True

    monkeypatch.setattr(cn, "NativeSurface", lambda **kwargs: _Stub(**kwargs))
    monkeypatch.setattr(cn, "look", look)
    monkeypatch.setattr(cn, "act", noop)
    monkeypatch.setattr(cn, "controls", noop)
    monkeypatch.setattr(cn, "stale", noop)
    monkeypatch.setattr(cn, "observe", observe)
    monkeypatch.setattr("sys.argv", ["check_native.py", *argv])
    return ran


async def test_repeat_runs_everything_n_times_and_numbers_the_runs(cn, monkeypatch, capsys):
    ran = _patch_main(cn, monkeypatch, argv=["--repeat", "3"])
    assert await cn.main() == 0
    out = capsys.readouterr().out
    assert ran["look"] == 3
    assert "=== run 1 of 3 ===" in out and "=== run 3 of 3 ===" in out
    assert [step["iteration"] for step in cn.RUN.steps if step["label"] == "read the window"] == [1, 2, 3]
    assert "Timings in milliseconds — median of 3 run(s), worst in brackets" in out
    assert "3/3" in out


async def test_a_single_plain_run_prints_no_table_and_no_criteria(cn, monkeypatch, capsys):
    _patch_main(cn, monkeypatch)
    assert await cn.main() == 0
    out = capsys.readouterr().out
    assert "Timings in milliseconds" not in out and "Exit criteria" not in out and "=== run" not in out


async def test_all_runs_every_section_prints_the_criteria_and_writes_the_results(cn, monkeypatch, capsys, tmp_path):
    monkeypatch.chdir(tmp_path)
    ran = {}

    def section(name):
        async def run(*args):
            ran[name] = True
            return True
        return run

    _patch_main(cn, monkeypatch, argv=["--all", "--repeat", "3"])
    for name in ("act", "controls", "stale"):
        monkeypatch.setattr(cn, name, section(name))
    monkeypatch.setattr(cn, "observe", section("observe"))
    assert await cn.main() == 0
    assert set(ran) == {"act", "controls", "stale", "observe"}
    out = capsys.readouterr().out
    assert "Exit criteria for real-Mac validation" in out and "☐ Permissions" in out
    (written,) = tmp_path.glob("native-validation-*.json")
    report = json.loads(written.read_text())
    assert report["schema"] == 1 and report["repeat"] == 3 and report["ok"] is True
    assert report["argv"] == ["--all", "--repeat", "3"]
    assert {row["area"] for row in report["criteria"]} >= {"Stale handles", "Observer", "Performance"}
    assert str(written.name) in out


async def test_json_goes_where_it_is_told_and_a_failed_step_fails_the_run(cn, monkeypatch, capsys, tmp_path):
    target = tmp_path / "out" / "results.json"
    target.parent.mkdir()
    _patch_main(cn, monkeypatch, look_ok=False, argv=["--json", str(target)])
    assert await cn.main() == 1
    report = json.loads(target.read_text())
    assert report["ok"] is False
    assert any(step["status"] == "fail" for step in report["steps"])
    assert "Something didn't work" in capsys.readouterr().out


async def test_repeat_below_one_is_one(cn, monkeypatch):
    ran = _patch_main(cn, monkeypatch, argv=["--repeat", "0"])
    assert await cn.main() == 0 and ran["look"] == 1


# ---------------------------------------------------------------------------
# step 1: looking, and "text on a screenshot"
# ---------------------------------------------------------------------------
@pytest.fixture
def looking(cn, monkeypatch, tmp_path):
    """Calculator's window, a screenshot of it, and OCR that returns whatever
    the test sets in ``ocr`` (a list of TextBox, or an exception to raise)."""
    pytest.importorskip("PIL")
    from PIL import Image

    calculator = Calculator()
    state = {"ocr": calculator.ocr(2), "picture": "busy"}

    async def capture(pid, number):
        path = tmp_path / "window.png"
        image = Image.new("RGB", (400, 600), "white")
        if state["picture"] == "busy":
            for x in range(0, 400, 8):
                image.putpixel((x, x), (0, 0, 0))
        image.save(path)
        return path

    def recognize(path):
        if isinstance(state["ocr"], Exception):
            raise state["ocr"]
        return state["ocr"]

    monkeypatch.setattr(cn, "capture_window", capture)
    monkeypatch.setattr(cn, "recognize_text", recognize)
    surface = surface_for({"Calculator": (101, calculator.app)}, "Calculator", ClickingInput(calculator))
    return cn, surface, state, calculator


async def test_text_that_sits_inside_a_control_still_counts_as_text_read_off_the_screenshot(looking, capsys):
    """A terminal's text is all inside one big text area, so none of it becomes a
    mark of its own; OCR reading it is still what the step is about. Here every
    piece of text is inside a button, so there are no marks from text at all."""
    cn, surface, state, calculator = looking
    state["ocr"] = calculator.ocr(2) + [TextBox("Clear", x=48, y=128, w=24, h=24)]
    assert await cn.look(surface, "Calculator") is True
    out = capsys.readouterr().out
    assert "✓ text on a screenshot — 7 pieces of text read (0 outside any control)" in out
    assert "✓ screenshot text matches the window's own text — both contain: clear" in out


async def test_text_outside_every_control_is_counted_apart_from_text_inside_them(looking, capsys):
    cn, surface, state, calculator = looking
    state["ocr"] = calculator.ocr(2) + [TextBox("Calculator", x=300, y=10, w=140, h=24)]
    assert await cn.look(surface, "Calculator") is True
    out = capsys.readouterr().out
    assert "✓ text on a screenshot — 7 pieces of text read (1 outside any control)" in out
    assert "✓ screenshot text matches the window's own text — both contain: calculator" in out


async def test_a_screenshot_with_no_readable_text_is_still_a_failure(looking, capsys):
    cn, surface, state, _ = looking
    state["ocr"] = []
    assert await cn.look(surface, "Calculator") is False
    out = capsys.readouterr().out
    assert "✗ text on a screenshot — 0 pieces of text read" in out
    assert "diagnosis: OCR on that picture: 0 pieces of text" in out
    assert "one flat colour" not in out, "this picture has content, so it isn't a permission problem"


async def test_text_below_the_confidence_floor_is_not_text_read(looking, capsys):
    cn, surface, state, _ = looking
    state["ocr"] = [TextBox("smudge", x=10, y=10, w=50, h=12, confidence=0.1)]
    assert await cn.look(surface, "Calculator") is False
    assert "✗ text on a screenshot — 0 pieces of text read" in capsys.readouterr().out


async def test_a_blank_screenshot_points_at_screen_recording(looking, capsys):
    cn, surface, state, _ = looking
    state["ocr"], state["picture"] = [], "blank"
    assert await cn.look(surface, "Calculator") is False
    out = capsys.readouterr().out
    assert "✗ text on a screenshot" in out
    assert "one flat colour: Screen Recording is probably not allowed for this terminal" in out


async def test_the_failure_seen_on_a_mac_says_what_raised_where_and_which_stage_it_was(looking, capsys):
    """The step used to print just the exception text — "NSInvalidArgumentException
    - key does not exist" — with nothing to say it came from the OCR, or from where."""
    cn, surface, state, _ = looking
    state["ocr"] = ValueError("NSInvalidArgumentException - key does not exist")
    assert await cn.look(surface, "Calculator") is False
    out = capsys.readouterr().out
    assert "✗ text on a screenshot — ValueError: NSInvalidArgumentException - key does not exist — at test_check_native.py" in out \
        or "✗ text on a screenshot — ValueError: NSInvalidArgumentException - key does not exist — at check_native.py" in out
    assert "diagnosis: window id: 4242" in out
    assert "diagnosis: screenshot: 400×600 px" in out
    assert "diagnosis: OCR on that picture failed: ValueError: NSInvalidArgumentException - key does not exist" in out


async def test_a_screenshot_whose_text_shares_nothing_with_the_window_is_inconclusive_not_a_pass(looking, capsys):
    cn, surface, state, _ = looking
    state["ocr"] = [TextBox("Completely unrelated sentence", x=10, y=10, w=200, h=14)]
    assert await cn.look(surface, "Calculator") is False
    out = capsys.readouterr().out
    assert "✓ text on a screenshot — 1 pieces of text read" in out
    assert "⚠ screenshot text matches the window's own text — no word in common. Screenshot: Completely unrelated sentence" in out


async def test_no_window_to_photograph_is_reported_by_the_screenshot_stage(looking, monkeypatch, capsys):
    cn, surface, state, _ = looking
    surface.backend.window_number = lambda pid, title="": None
    assert await cn.look(surface, "Calculator") is False
    out = capsys.readouterr().out
    assert "✗ text on a screenshot — I couldn't find Calculator's window on screen to look at." in out
    assert "diagnosis: window id: none found for this window on screen" in out


async def test_matching_words_needs_four_letters_and_ignores_case_and_punctuation(cn):
    assert cn.matching_words(["Zsh — Documents"], ["documents", "Terminal"]) == ["documents"]
    assert cn.matching_words(["abc de"], ["abc de"]) == []
    assert cn.matching_words(["TERMINAL-window"], ["terminal"]) == ["terminal"]
    assert cn.matching_words([], ["terminal"]) == []


async def test_a_failure_is_described_by_its_type_and_the_innermost_line_of_this_project(cn):
    def deep():
        raise KeyError("missing")

    try:
        deep()
    except KeyError as exc:
        text = cn.describe_failure(exc)
    assert text.startswith("KeyError: 'missing' — at test_check_native.py:") and text.endswith(" in deep")
    assert cn.describe_failure(cn.NativeError("plain words")) == "plain words"


# ---------------------------------------------------------------------------
# step 2: the save sheet on closing an edited document
# ---------------------------------------------------------------------------
class ClosingTextEdit:
    """TextEdit with an edited, unsaved document, whose File ▸ Close does what
    *behaviour* says: "sheet" (a sheet with *buttons*), "late" (the same, a moment
    after), "silent" (closes without asking), "nothing" (stays, no sheet), "dialog"
    (a separate dialog window, not a sheet)."""

    def __init__(self, behaviour: str = "sheet", buttons=("Delete", "Cancel", "Save…"), *,
                 discard_closes: bool = True, identifiers: bool = True, ids: dict | None = None,
                 focus: str = "sheet", sheet_frame: str = "normal"):
        """*focus*: what the app calls its focused window while the sheet is up (``sheet``, as macOS 27
        does, or ``window``). *sheet_frame*: the sheet's geometry as it reports it: ``normal``,
        ``zero`` (0×0), ``flat`` (full width, no height) or ``missing`` (no position or size)."""
        self.behaviour, self.log, self.discard_closes = behaviour, [], discard_closes
        self.focus, self.sheet_frame = focus, sheet_frame
        self.identifiers, self.ids, self.pressed = identifiers, ids or {}, []
        self.area = El("AXTextArea", description="document", value="", frame=(60, 80, 480, 300))
        self.window = El("AXWindow", "Untitled", actions=(), frame=(50, 50, 500, 400), children=[self.area])
        self.dialog = None
        bold = El("AXMenuItem", "Bold")
        bold.on_perform = lambda _a: self.log.append("Bold")
        font = El("AXMenuItem", "Font", children=[El("AXMenu", actions=(), children=[bold])])
        close, new = El("AXMenuItem", "Close"), El("AXMenuItem", "New")
        close.on_perform = lambda _a: self._close()
        new.on_perform = lambda _a: self.log.append("New")
        menubar = El("AXMenuBar", actions=(), children=[
            El("AXMenuBarItem", "Apple"), El("AXMenuBarItem", "TextEdit"),
            El("AXMenuBarItem", "File", children=[El("AXMenu", actions=(), children=[new, close])]),
            El("AXMenuBarItem", "Format", children=[El("AXMenu", actions=(), children=[font])])])
        self.app = El("AXApplication", "TextEdit", actions=(), AXWindows=[self.window],
                      AXFocusedWindow=self.window, AXMenuBar=menubar)
        self.buttons = buttons

    #: AppKit's identifiers for a save sheet's buttons, by what the button does.
    IDS = {"delete": "DontSaveButton", "don't save": "DontSaveButton", "cancel": "CancelButton",
           "save": "OKButton", "save…": "OKButton"}

    def _sheet(self):
        controls = []
        for title in self.buttons:
            kind = title.lower().replace("’", "'")
            ident = self.ids[title] if title in self.ids else (self.IDS.get(kind) if self.identifiers else None)
            button = El("AXButton", title, frame=(300, 380, 80, 24), **({"AXIdentifier": ident} if ident else {}))
            discards = ident == "DontSaveButton" if ident is not None else \
                kind in {"delete", "don't save", "throw away"}
            button.on_perform = (lambda _a, t=title: self._discard(t)) if discards else \
                (lambda _a, t=title: self._back_out(t))
            controls.append(button)
        frame = {"normal": (60, 200, 480, 200), "zero": (60, 200, 0, 0), "flat": (60, 200, 480, 0),
                 "missing": None}[self.sheet_frame]
        sheet = El("AXSheet", description="save", AXIdentifier="save-panel", actions=(), frame=frame,
                   children=[El("AXStaticText", value="Do you want to keep this new document?", actions=()),
                             *controls])
        sheet.attrs["AXParent"] = self.window
        return sheet

    def _raise_sheet(self, sheet):
        """As on a Mac: the sheet hangs from the window, and is what the app calls its focused window."""
        self.window.children.append(sheet)
        self.app.attrs["AXFocusedWindow"] = sheet if self.focus == "sheet" else self.window

    def _lower_sheet(self):
        self.window.children = [c for c in self.window.children if c.attrs["AXRole"] != "AXSheet"]
        self.app.attrs["AXFocusedWindow"] = self.window

    def _close(self):
        self.log.append("Close")
        if self.behaviour == "sheet":
            self._raise_sheet(self._sheet())
        elif self.behaviour == "late":
            threading.Timer(0.15, lambda: self._raise_sheet(self._sheet())).start()
        elif self.behaviour == "silent":
            self.app.attrs["AXWindows"] = []
            self.app.attrs["AXFocusedWindow"] = None
        elif self.behaviour == "dialog":
            self.dialog = El("AXWindow", "Save", subrole="AXDialog", actions=(), frame=(100, 100, 300, 150),
                             children=[El("AXButton", "OK", frame=(200, 200, 60, 24))])
            self.app.attrs["AXWindows"] = [self.window, self.dialog]

    def _discard(self, title=""):
        self.log.append("discard")
        self.pressed.append(title)
        if self.discard_closes:
            self.app.attrs["AXWindows"], self.app.attrs["AXFocusedWindow"] = [], None
        else:
            self._lower_sheet()

    def _back_out(self, title):
        self.log.append(title)
        self.pressed.append(title)
        self._lower_sheet()


def _closing(behaviour="sheet", **kwargs):
    textedit = ClosingTextEdit(behaviour, **kwargs)
    textedit.area.attrs["AXValue"] = "Hello from JARVIS — café, £5, 😀"
    surface = surface_for({"TextEdit": (303, textedit.app)}, "TextEdit")
    return textedit, surface


@pytest.fixture
def quick_sheet(cn, monkeypatch):
    monkeypatch.setattr(cn, "SHEET_WAIT_S", 0.3)


@pytest.mark.parametrize("identifiers", [True, False])
@pytest.mark.parametrize("buttons", [("Delete", "Cancel", "Save…"), ("Don’t Save", "Cancel", "Save"),
                                     ("Don't Save", "Cancel", "Save")])
async def test_whichever_way_a_macos_release_words_the_sheet_it_is_found_and_discarded(cn, quick_sheet, capsys,
                                                                                    buttons, identifiers):
    """With AppKit's identifiers on the buttons (as on macOS 27) or, for an app that has none, by title."""
    textedit, surface = _closing(buttons=buttons, identifiers=identifiers)
    await surface.choose_menu(["File", "Close"], "TextEdit")
    assert await cn.save_sheet(surface, typed="Hello from JARVIS") is True
    out = capsys.readouterr().out
    assert "✓ the save sheet appeared, listed first — blockers: ['sheet “save”']" in out
    assert ("(id DontSaveButton)" in out) is identifiers and ("(title “" in out) is not identifiers
    assert "✓ discarding closed the document without saving — TextEdit has no window left" in out
    assert textedit.log == ["Close", "discard"], "the document was discarded, and nothing else was pressed"


@pytest.mark.parametrize("sheet_frame", ["normal", "zero", "flat", "missing"])
@pytest.mark.parametrize("focus", ["sheet", "window"])
async def test_the_exact_structure_seen_on_a_mac_is_found_whatever_is_focused_and_whatever_size_it_reports(
        cn, quick_sheet, capsys, focus, sheet_frame):
    """AXWindow → AXSheet desc='save' id='save-panel' → Delete (DontSaveButton) / Cancel (CancelButton) /
    Save (OKButton): the sheet directly under the window, with the window or the sheet focused, and
    with the sheet reporting an ordinary frame, none, or one with no area."""
    textedit, surface = _closing(focus=focus, sheet_frame=sheet_frame)
    await surface.choose_menu(["File", "Close"], "TextEdit")
    assert any(c.attrs["AXRole"] == "AXSheet" for c in textedit.window.children)
    assert await cn.save_sheet(surface, typed="Hello from JARVIS") is True
    out = capsys.readouterr().out
    assert "blockers: none" not in out and "no sheet is open" not in out
    assert "discard = “Delete” (id DontSaveButton)" in out
    assert textedit.pressed == ["Delete"]


async def test_a_sheet_that_slides_down_a_moment_later_is_waited_for(cn, monkeypatch, capsys):
    monkeypatch.setattr(cn, "SHEET_WAIT_S", 2.0)
    textedit, surface = _closing("late")
    await surface.choose_menu(["File", "Close"], "TextEdit")
    assert await cn.save_sheet(surface, typed="Hello from JARVIS") is True


async def test_no_sheet_at_all_is_a_failure_and_the_tree_is_printed_as_the_evidence(cn, quick_sheet, capsys):
    textedit, surface = _closing("nothing")
    await surface.choose_menu(["File", "Close"], "TextEdit")
    assert await cn.save_sheet(surface, typed="Hello from JARVIS") is False
    out = capsys.readouterr().out
    assert "✗ the save sheet appeared, listed first — no sheet is open; blockers: none" in out
    assert "evidence: 1 AX window(s): AXWindow title='Untitled'" in out
    assert "evidence: AXFocusedWindow: AXWindow title='Untitled'" in out and "(one of AXWindows)" in out
    assert "frame=(50,50 500x400)" in out, "the dump shows geometry, which is what an element is skipped for"
    assert "evidence:     AXTextArea desc='document' value='Hello from JARVIS — café, £5, 😀'" in out
    assert "discard" not in textedit.log, "nothing is discarded when the sheet isn't understood"


async def test_a_document_closed_without_asking_is_called_that(cn, quick_sheet, capsys):
    textedit, surface = _closing("silent")
    await surface.choose_menu(["File", "Close"], "TextEdit")
    assert await cn.save_sheet(surface, typed="Hello from JARVIS") is False
    assert "has no window to read after File ▸ Close, so it closed the document without asking" \
        in capsys.readouterr().out


async def test_labels_that_offer_no_way_to_discard_are_a_failure_that_lists_what_was_there(cn, quick_sheet, capsys):
    textedit, surface = _closing(buttons=("Throw away", "Cancel", "Keep"))
    await surface.choose_menu(["File", "Close"], "TextEdit")
    assert await cn.save_sheet(surface, typed="Hello from JARVIS") is False
    out = capsys.readouterr().out
    assert "the sheet has no discard / save button" in out
    assert "button “Throw away”, button “Cancel” [CancelButton], button “Keep”" in out
    assert textedit.log == ["Close", "Cancel"], "it backs out of the sheet rather than guess which button discards"
    assert "close it and choose Delete" in out


async def test_a_buttons_identifier_outranks_its_title(cn, quick_sheet, capsys):
    """The discard button is the one AppKit calls DontSaveButton, whatever it says on it — and a
    button that merely says "Delete" is not taken for it."""
    textedit, surface = _closing(buttons=("Delete", "Wipe", "Cancel", "Save…"),
                                 ids={"Delete": "SomethingElse", "Wipe": "DontSaveButton"})
    await surface.choose_menu(["File", "Close"], "TextEdit")
    assert await cn.save_sheet(surface, typed="Hello from JARVIS") is True
    out = capsys.readouterr().out
    assert "discard = “Wipe” (id DontSaveButton)" in out
    assert textedit.pressed == ["Wipe"], "the button that says Delete was left alone"


async def test_a_sheet_the_app_reports_as_its_focused_window_is_found_by_the_check(cn, quick_sheet, capsys):
    """The failure seen on a Mac: with the sheet focused, the surface once saw an ordinary window,
    reported no sheet and listed none of its controls."""
    textedit, surface = _closing()
    await surface.choose_menu(["File", "Close"], "TextEdit")
    assert textedit.app.attrs["AXFocusedWindow"] is not textedit.window, "the fake is as a Mac is"
    assert await cn.save_sheet(surface, typed="Hello from JARVIS") is True
    assert "blockers: none" not in capsys.readouterr().out


async def test_a_dialog_window_is_not_mistaken_for_a_sheet(cn, quick_sheet, capsys):
    textedit, surface = _closing("dialog")
    await surface.choose_menu(["File", "Close"], "TextEdit")
    assert await cn.save_sheet(surface, typed="Hello from JARVIS") is False
    out = capsys.readouterr().out
    assert "no sheet is open (only a dialog or popover)" in out
    assert "evidence: 2 AX window(s)" in out and "AXWindow/AXDialog" in out


async def test_a_discard_that_leaves_the_document_open_is_a_failure(cn, quick_sheet, capsys):
    textedit, surface = _closing(discard_closes=False)
    await surface.choose_menu(["File", "Close"], "TextEdit")
    assert await cn.save_sheet(surface, typed="Hello from JARVIS") is False
    out = capsys.readouterr().out
    assert "✓ the save sheet appeared, listed first" in out
    assert "✗ discarding closed the document without saving — still open:" in out


async def test_controls_listed_behind_the_windows_own_are_not_listed_first(cn, quick_sheet, monkeypatch, capsys):
    textedit, surface = _closing()
    await surface.choose_menu(["File", "Close"], "TextEdit")
    real = surface.read

    async def reversed_read(app="", *, offset=0):
        snap, listing = await real(app, offset=offset)
        snap.controls.reverse()              # the window's controls ahead of the sheet's
        return snap, listing

    monkeypatch.setattr(surface, "read", reversed_read)
    assert await cn.save_sheet(surface, typed="Hello from JARVIS") is False
    assert "the sheet's controls are not listed ahead of the window's" in capsys.readouterr().out


async def test_the_whole_round_trip_passes_with_the_modern_sheet(cn, quick_sheet, capsys):
    textedit, surface = _closing()
    textedit.area.attrs["AXValue"] = ""
    assert await cn.act(surface) is True
    out = capsys.readouterr().out
    for line in ("✓ found the document's text area", "✓ typed Unicode text", "✓ chose Format › Font › Bold",
                 "✓ the save sheet appeared, listed first", "✓ discarding closed the document without saving"):
        assert line in out
    assert "✗" not in out


async def test_labels_are_compared_without_case_ellipsis_or_curly_apostrophes(cn):
    assert cn.plain("Save…") == "save" and cn.plain("Don’t Save") == "don't save"
    assert cn.plain("Cancel ") == "cancel" and cn.plain("Save...") == "save"


async def test_the_dump_says_when_the_focused_window_is_a_sheet_that_is_not_one_of_the_windows(cn):
    textedit, surface = _closing()
    await surface.choose_menu(["File", "Close"], "TextEdit")
    lines = cn.ax_structure(surface.backend, 303)
    focused = next(line for line in lines if line.startswith("AXFocusedWindow:"))
    assert "AXSheet" in focused and "NOT one of AXWindows" in focused
    assert "hangs from: AXWindow/" not in focused and "hangs from: AXWindow title='Untitled'" in focused
    assert lines[0].startswith("1 AX window(s): AXWindow title='Untitled'")


async def test_the_tree_dump_is_bounded_and_says_when_there_is_no_window(cn):
    textedit, surface = _closing()
    for index in range(200):
        textedit.window.children.append(El("AXButton", f"b{index}"))
    lines = cn.ax_structure(surface.backend, 303, limit=20)
    assert lines[0].startswith("1 AX window(s)") and len(lines) <= 23
    textedit.app.attrs["AXWindows"], textedit.app.attrs["AXFocusedWindow"] = [], None
    assert cn.ax_structure(surface.backend, 303) == ["0 AX window(s): ", "no front window"]
